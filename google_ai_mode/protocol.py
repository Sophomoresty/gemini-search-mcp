"""Pure-protocol Google AI Mode client — no browser, no JS engine.

Flow:
1. GET https://www.google.com/search?q=<q>&udm=50 with cookies → 360KB HTML
2. Extract tokens (srtst, xsrf_folif, xsrf_folwr, garc, lro_token, ei) from data-* attributes
3. GET /async/folwr?<tokens>&q=<question> → streaming HTML response with AI answer
4. Parse text from HTML chunks
"""
import re
import ssl
import time
import urllib.request
import urllib.parse
import urllib.error
from html.parser import HTMLParser


_SEARCH_URL = "https://www.google.com.hk/search?q={q}&hl=en&gl=us&udm=50&aep=1&ntc=1"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"

_TOKEN_ATTRS = [
    "data-srtst",
    "data-xsrf-folif-token",
    "data-xsrf-folwr-token",
    "data-garc",
    "data-lro-token",
    "data-lro-signature",
    "data-ei",
    "data-stkp",
]


def extract_sca_esv(html):
    """Extract sca_esv hash from page."""
    m = re.search(r'sca_esv=([a-f0-9]+)', html) or re.search(r'"sca_esv":"([a-f0-9]+)"', html)
    return m.group(1) if m else ""


def extract_ved(html):
    """Extract ved from the AI Mode tab.

    The active AI Mode tab is <a aria-current="page" ... data-ved="...">.
    vet is derived as: vet = "1" + ved + "..i"
    """
    # Primary: AI Mode tab has aria-current="page" and data-ved
    m = re.search(r'aria-current="page"[^>]*data-ved="([^"]+)"', html)
    if m:
        return m.group(1)
    m = re.search(r'data-ved="([^"]+)"[^>]*aria-current="page"', html)
    if m:
        return m.group(1)
    # Fallback: data-ved nearest before "AI Mode" text
    idx = html.find(">AI Mode<")
    if idx < 0:
        idx = html.find("AI Mode")
    if idx > -1:
        chunk = html[max(0, idx - 500):idx]
        matches = re.findall(r'data-ved="([^"]+)"', chunk)
        if matches:
            return matches[-1]
    return ""


def _ssl_ctx():
    return ssl.create_default_context()


def _update_cookies(existing, set_cookie_headers):
    """Merge Set-Cookie headers into existing cookie string."""
    if not set_cookie_headers:
        return existing
    cookie_map = {}
    for pair in existing.split("; "):
        if "=" in pair:
            k, v = pair.split("=", 1)
            cookie_map[k.strip()] = v
    for header in set_cookie_headers:
        pair = header.split(";")[0]
        if "=" in pair:
            k, v = pair.split("=", 1)
            cookie_map[k.strip()] = v.strip()
    return "; ".join(f"{k}={v}" for k, v in cookie_map.items())


def _fetch(url, cookies, referer=None, max_redirects=5, cookie_sink=None):
    """Fetch a URL with cookies, following redirects manually.

    cookie_sink: optional dict with 'cookies' key to receive updated cookies.
    """
    headers = {
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        'Sec-CH-UA': '"Chromium";v="148", "Not?A_Brand";v="24", "Google Chrome";v="148"',
        "Sec-CH-UA-Mobile": "?0",
        'Sec-CH-UA-Platform': '"Windows"',
        "Upgrade-Insecure-Requests": "1",
        "Cookie": cookies,
    }
    if referer:
        headers["Referer"] = referer

    ctx = _ssl_ctx()
    body = ""
    resp = None
    for _ in range(max_redirects):
        req = urllib.request.Request(url, headers=headers, method="GET")
        resp = urllib.request.urlopen(req, context=ctx, timeout=30)
        # Capture Set-Cookie to refresh tokens
        set_cookies = resp.headers.get_all("Set-Cookie") or []
        if set_cookies and cookie_sink is not None:
            cookie_sink["cookies"] = _update_cookies(cookie_sink.get("cookies", cookies), set_cookies)
            headers["Cookie"] = cookie_sink["cookies"]
        body = resp.read().decode("utf-8", errors="replace")
        if resp.status in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location")
            if not loc:
                break
            url = urllib.parse.urljoin(url, loc)
            continue
        return body, resp
    return body, resp


def get_cookies(seed_url="https://www.google.com/"):
    """Bootstrap cookies by visiting Google homepage (returns cookie string)."""
    req = urllib.request.Request(seed_url, headers={"User-Agent": _UA})
    ctx = _ssl_ctx()
    resp = urllib.request.urlopen(req, context=ctx, timeout=15)
    cookies = []
    for header in resp.headers.get_all("Set-Cookie") or []:
        pair = header.split(";")[0]
        if "=" in pair:
            cookies.append(pair.strip())
    return "; ".join(cookies)


def extract_tokens(html):
    """Extract AI Mode tokens from the search page HTML.

    Tokens live on two elements:
    - A div with data-srtst/data-garc/data-lro-*/data-xsrf-*/data-ei
    - A separate div with data-stkp
    """
    tokens = {}

    # Main token element
    token_el_match = re.search(
        r'<div([^>]*data-srtst="[^"]*"[^>]*)>', html
    )
    if not token_el_match:
        raise RuntimeError("Token element not found — cookies may be insufficient")

    attrs_str = token_el_match.group(1)
    for attr in _TOKEN_ATTRS:
        m = re.search(re.escape(attr) + r'="([^"]+)"', attrs_str)
        if m:
            tokens[attr] = m.group(1)

    # data-stkp is on a separate div
    stkp_match = re.search(r'data-stkp="([^"]+)"', html)
    if stkp_match:
        tokens["data-stkp"] = stkp_match.group(1)

    if "data-srtst" not in tokens:
        raise RuntimeError("data-srtst not found in token element")

    return tokens


def build_folwr_url(tokens, question, sca_esv="", ved="", base="https://www.google.com.hk"):
    """Build the /async/folwr streaming request URL."""
    srtst = tokens["data-srtst"]
    xsrf = tokens["data-xsrf-folwr-token"]
    garc = tokens["data-garc"]
    lro = tokens.get("data-lro-token", "")
    mlros = tokens.get("data-lro-signature", "")
    stkp = tokens.get("data-stkp", "")
    ei = tokens["data-ei"]
    vet = f"1{ved}..i" if ved else ""

    params = {
        "srtst": srtst,
        "garc": garc,
        "mlro": lro,
        "mlros": mlros,
        "ei": ei,
        "q": question,
        "yv": "3",
        "vet": vet,
        "ved": ved,
        "aep": "1",
        "gl": "us",
        "hl": "en",
        "sca_esv": sca_esv,
        "udm": "50",
        "stkp": stkp,
        "cs": "0",
        "async": f"_fmt:adl,_xsrf:{xsrf}",
    }
    params = {k: v for k, v in params.items() if v}
    query = urllib.parse.urlencode(params)
    return f"{base}/async/folwr?{query}"


class _TextExtractor(HTMLParser):
    """Extract AI answer text from folwr HTML.

    The answer body lives in <div class="n6owBd ..."> (answer component)
    containing <div class="pTRUV" dir="ltr"> (formatted answer text).
    Search citations and UI controls use different containers.
    """

    def __init__(self):
        super().__init__()
        self.text_parts = []
        self._div_stack = []
        self._skip_stack = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "button"):
            self._skip_stack.append(tag)
            return
        if tag == "div":
            self._div_stack.append(dict(attrs).get("class", ""))

    def handle_endtag(self, tag):
        if tag in ("script", "style", "button"):
            if self._skip_stack:
                self._skip_stack.pop()
            return
        if tag == "div" and self._div_stack:
            self._div_stack.pop()

    def _in_answer(self):
        # n6owBd = answer component wrapper, pTRUV = formatted answer text
        return any("n6owBd" in c or "pTRUV" in c for c in self._div_stack)

    def handle_data(self, data):
        if self._skip_stack:
            return
        if self._in_answer():
            stripped = data.strip()
            if stripped:
                self.text_parts.append(stripped)

    def get_text(self):
        return " ".join(self.text_parts)


# UI noise phrases to strip from tail of extracted text
_UI_NOISE = [
    "Copy", "Share", "Good response", "Bad response", "About this result",
    "View related links", "public link", "AI responses may include mistakes",
    "Tell me which", "Would you like", "This public link is valid",
    "If you share with", "cannot be deleted",
]


def parse_response_text(html_chunk):
    """Parse accumulated HTML response to extract AI answer text."""
    extractor = _TextExtractor()
    try:
        extractor.feed(html_chunk)
    except Exception:
        pass
    text = extractor.get_text()
    # Trim trailing UI noise
    changed = True
    while changed:
        changed = False
        for noise in _UI_NOISE:
            if text.endswith(noise) or text.endswith(noise + "."):
                text = text[: -len(noise)].rstrip(" .,")
                changed = True
    return text


class AIModeClient:
    """Pure-protocol AI Mode client."""

    def __init__(self, cookies=None, proxy=None):
        self.cookies = cookies or ""
        self.proxy = proxy
        self.tokens = None
        self.sca_esv = ""
        self.ved = ""
        self.session_query = ""
        self.page_html = None

    def init_session(self, query="hello"):
        """Load the AI Mode page for a query, extract tokens.

        The query binds the session — folwr must use the SAME query.
        """
        if not self.cookies:
            self.cookies = get_cookies()

        url = _SEARCH_URL.format(q=urllib.parse.quote(query))
        sink = {"cookies": self.cookies}
        html, _ = _fetch(url, self.cookies, cookie_sink=sink)
        self.cookies = sink["cookies"]
        self.page_html = html
        self.tokens = extract_tokens(html)
        self.sca_esv = extract_sca_esv(html)
        self.ved = extract_ved(html)
        self.session_query = query
        return self.tokens

    def ask(self, question, timeout=60, retries=3):
        """Ask a question, return full response text.

        Each question starts a fresh session (new page load + folwr).
        Retries on rate-limit (429) with exponential backoff.
        """
        last_err = None
        for attempt in range(retries):
            try:
                self.init_session(question)
                url = build_folwr_url(self.tokens, question, self.sca_esv, self.ved)
                sink = {"cookies": self.cookies}
                body, _ = _fetch(url, self.cookies, referer="https://www.google.com.hk/", cookie_sink=sink)
                self.cookies = sink["cookies"]
                text = parse_response_text(body)
                if text:
                    return text
                # Empty but no error — retry once
                if attempt < retries - 1:
                    time.sleep(2 * (attempt + 1))
                    continue
                return text
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code == 429:
                    wait = 5 * (attempt + 1)
                    time.sleep(wait)
                    continue
                raise
        if last_err:
            raise last_err
        return ""

    def ask_stream(self, question, timeout=60):
        """Ask a question, yield text chunks as they stream."""
        self.init_session(question)
        url = build_folwr_url(self.tokens, question, self.sca_esv, self.ved)
    def ask_stream(self, question, timeout=60, retries=3):
        """Ask a question, yield text chunks as they stream.

        Retries on rate-limit (429) with exponential backoff.
        """
        last_err = None
        for attempt in range(retries):
            try:
                self.init_session(question)
                url = build_folwr_url(self.tokens, question, self.sca_esv, self.ved)
                headers = {
                    "User-Agent": _UA,
                    "Accept": "text/html,*/*",
                    "Cookie": self.cookies,
                    "Referer": "https://www.google.com.hk/",
                }
                ctx = _ssl_ctx()
                req = urllib.request.Request(url, headers=headers, method="GET")

                accumulated = ""
                prev_text = ""
                resp = urllib.request.urlopen(req, context=ctx, timeout=timeout)
                has_yielded = False
                for raw in resp:
                    chunk = raw.decode("utf-8", errors="replace")
                    accumulated += chunk
                    text = parse_response_text(accumulated)
                    if len(text) > len(prev_text):
                        yield text[len(prev_text):]
                        prev_text = text
                        has_yielded = True
                if has_yielded or prev_text:
                    return
                # Empty result, retry
                if attempt < retries - 1:
                    time.sleep(2 * (attempt + 1))
                    continue
                return
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code == 429:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise
        if last_err:
            raise last_err


if __name__ == "__main__":
    import sys

    cookies = open("/tmp/all_cookies.txt").read().strip()
    client = AIModeClient(cookies=cookies)

    print("Initializing session...")
    tokens = client.init_session("hello")
    print(f"Tokens: {list(tokens.keys())}")
    print(f"  srtst: {tokens.get('data-srtst','')[:50]}...")
    print(f"  ei: {tokens.get('data-ei','')}")

    print("\n[1] ask: 'what is 2+2?'")
    answer = client.ask("what is 2+2? answer only the number")
    print(f"  → {answer[:200]}")

    print("\n[2] stream: 'explain python in 2 sentences'")
    full = ""
    for chunk in client.ask_stream("explain python in 2 sentences"):
        full += chunk
        print(f"  +{len(chunk)} chars")
    print(f"  Total: {len(full)} chars: {full[:200]}")
