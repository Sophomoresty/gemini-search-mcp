"""Lightweight engine for Google AI Mode.

Architecture: launch a real Chrome with minimal flags via subprocess,
connect via CDP WebSocket, run queries as JS fetch() inside one tab.
No Playwright — just subprocess + websockets + json.

Key insight: Playwright's launch injects automation markers that Google
instantly detects (CAPTCHA). A plain subprocess Chrome with only
--remote-debugging-port and --user-data-dir passes as a normal browser.
"""
import asyncio
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import websockets
except ImportError:
    websockets = None

try:
    from playwright.async_api import async_playwright
    _HAS_PLAYWRIGHT = True
except ImportError:
    _HAS_PLAYWRIGHT = False


_ASK_JS = """
(async (q) => {
    try {
        const pageUrl = 'https://www.google.com.hk/search?q=' + encodeURIComponent(q) + '&hl=en&gl=us&udm=50&aep=1&ntc=1';
        const r1 = await fetch(pageUrl, {credentials:'include'});
        if (!r1.ok) return {error:'fetch_status_' + r1.status, htmlLen:0};
        const html = await r1.text();
        const m = (p) => { const x = html.match(p); return x ? x[1] : ''; };
        const srtst = m(/data-srtst="([^"]+)"/);
        if (!srtst) return {error:'no_token', htmlLen:html.length, preview:html.substring(0,200)};
        const xsrf = m(/data-xsrf-folwr-token="([^"]+)"/);
        const garc = m(/data-garc="([^"]+)"/);
        const lro = m(/data-lro-token="([^"]+)"/);
        const mlros = m(/data-lro-signature="([^"]+)"/);
        const ei = m(/data-ei="([^"]+)"/);
        const stkp = m(/data-stkp="([^"]+)"/);
        const ved = m(/aria-current="page"[^>]*data-ved="([^"]+)"/);
        const sca = m(/sca_esv=([a-f0-9]+)/);
        const p = new URLSearchParams({srtst,garc,mlro:lro,mlros,ei,q,yv:'3',vet:'1'+ved+'..i',ved,aep:'1',gl:'us',hl:'en',sca_esv:sca,udm:'50',stkp,cs:'0',async:'_fmt:adl,_xsrf:'+xsrf});
        const r2 = await fetch('https://www.google.com.hk/async/folwr?'+p.toString(), {credentials:'include'});
        if (!r2.ok) return {error:'folwr_status_' + r2.status};
        const fh = await r2.text();
        const div = document.createElement('div');
        div.innerHTML = fh;
        // Remove non-content elements
        div.querySelectorAll('script,style,button,noscript,[aria-hidden="true"],span[style*="display:none"],.LGKDTe,.SGF5Lb').forEach(x => x.remove());
        // Collect text from ALL answer blocks: pTRUV first (short answers), then n6owBd (paragraphs)
        let parts = [];
        div.querySelectorAll('.pTRUV').forEach(el => {
            const t = el.textContent.trim();
            if (t && t.length > 1) parts.push(t);
        });
        div.querySelectorAll('.n6owBd').forEach(el => {
            const t = el.textContent.trim();
            if (t && t.length > 10) parts.push(t);
        });
        // Fallback: all dir=ltr blocks minus citation containers
        if (!parts.length) {
            div.querySelectorAll('.mZJni,.XEqVsf,.ub891').forEach(x => x.remove());
            div.querySelectorAll('[dir="ltr"]').forEach(el => {
                const t = el.textContent.trim();
                if (t.length > 30) parts.push(t);
            });
        }
        let text = parts.join('\\n\\n');
        // Clean trailing UI noise
        const noise = ['Copy','Share','Good response','Bad response','About this result','Show all','AI responses may include mistakes','Tell me which'];
        for (const n of noise) { while (text.endsWith(n)) text = text.slice(0, -n.length).trim(); }
        return {ok:true, answer:text, folwrLen:fh.length};
    } catch(e) {
        return {error:'js_exception', message:e.message};
    }
})(%QUERY%)
"""


def _find_chrome() -> str:
    """Find Chrome/Chromium binary path on the system."""
    system = platform.system()
    candidates = []
    if system == "Windows":
        for base in [os.environ.get("PROGRAMFILES", ""), os.environ.get("PROGRAMFILES(X86)", ""),
                     os.environ.get("LOCALAPPDATA", "")]:
            if base:
                candidates.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
                candidates.append(os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"))
    elif system == "Darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    else:
        candidates = ["google-chrome", "google-chrome-stable", "chromium-browser", "chromium"]

    for c in candidates:
        if os.path.isfile(c):
            return c
        found = shutil.which(c)
        if found:
            return found
    raise RuntimeError("Chrome/Edge/Chromium not found. Install Chrome or set CHROME_PATH env var.")


class AIModeEngine:
    """Single-tab Chrome engine via raw CDP. No Playwright needed."""

    def __init__(self):
        self._proc = None
        self._ws = None
        self._ws_url = None
        self._page_target = None
        self._lock = asyncio.Lock()
        self._msg_id = 0
        self._cdp_url = None
        self._user_data_dir = None

    async def start(self, cdp_url=None, headless=True, channel="chrome"):
        """Start Chrome and connect via CDP.

        If cdp_url is provided, connects to existing Chrome (no subprocess).
        Otherwise launches a new Chrome instance with minimal flags.
        """
        self._cdp_url = cdp_url
        if cdp_url:
            await self._connect_cdp(cdp_url)
        else:
            await self._launch_chrome(headless)
        await self._warmup()

    async def _launch_chrome(self, headless=True):
        """Launch Chrome subprocess with minimal automation footprint."""
        chrome_path = os.environ.get("CHROME_PATH") or _find_chrome()
        self._user_data_dir = tempfile.mkdtemp(prefix="gemini-search-mcp-")
        port = 19250  # dedicated port for this tool

        args = [
            chrome_path,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={self._user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-timer-throttling",
        ]
        if headless:
            args.append("--headless=new")
        args.append("about:blank")

        self._proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        # Wait for CDP to be ready
        import urllib.request
        for _ in range(30):
            await asyncio.sleep(0.5)
            try:
                data = urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2).read()
                info = json.loads(data)
                self._ws_url = info["webSocketDebuggerUrl"]
                break
            except Exception:
                continue
        else:
            raise RuntimeError(f"Chrome did not start (pid={self._proc.pid})")

        await self._connect_cdp(f"http://127.0.0.1:{port}")

    async def _connect_cdp(self, http_url):
        """Connect to Chrome via CDP WebSocket."""
        import urllib.request
        # Get browser WS URL
        try:
            data = urllib.request.urlopen(f"{http_url}/json/version", timeout=5).read()
            info = json.loads(data)
            self._ws_url = info["webSocketDebuggerUrl"]
        except Exception as e:
            raise RuntimeError(f"Cannot connect to Chrome at {http_url}: {e}")

        # Get or create a page target
        pages = json.loads(urllib.request.urlopen(f"{http_url}/json/list", timeout=5).read())
        page_targets = [p for p in pages if p.get("type") == "page"]
        if page_targets:
            self._page_target = page_targets[0]["webSocketDebuggerUrl"]
        else:
            # Create new tab
            new_tab = json.loads(urllib.request.urlopen(f"{http_url}/json/new?about:blank", timeout=5).read())
            self._page_target = new_tab["webSocketDebuggerUrl"]

        if not websockets:
            raise RuntimeError("websockets package required: pip install websockets")
        self._ws = await websockets.connect(self._page_target, max_size=10 * 1024 * 1024)

    async def _cdp_send(self, method, params=None):
        """Send a CDP command and return the result."""
        self._msg_id += 1
        msg = {"id": self._msg_id, "method": method, "params": params or {}}
        await self._ws.send(json.dumps(msg))
        while True:
            resp = json.loads(await self._ws.recv())
            if resp.get("id") == self._msg_id:
                if "error" in resp:
                    raise RuntimeError(f"CDP error: {resp['error']}")
                return resp.get("result", {})

    async def _evaluate(self, expression):
        """Evaluate JS in the page and return the result."""
        result = await self._cdp_send("Runtime.evaluate", {
            "expression": expression,
            "awaitPromise": True,
            "returnByValue": True,
        })
        val = result.get("result", {}).get("value")
        exc = result.get("exceptionDetails")
        if exc:
            desc = exc.get("exception", {}).get("description", exc.get("text", str(exc)))
            raise RuntimeError(f"JS error: {desc}")
        return val

    async def _navigate(self, url):
        """Navigate the page to a URL and wait for load."""
        await self._cdp_send("Page.enable")
        await self._cdp_send("Page.navigate", {"url": url})
        # Wait for loadEventFired
        for _ in range(60):
            msg = json.loads(await self._ws.recv())
            if msg.get("method") == "Page.loadEventFired":
                break
        await asyncio.sleep(1)

    async def _warmup(self):
        """Navigate to Google search to build cookie session."""
        await self._navigate("https://www.google.com.hk/search?q=hello&hl=en&gl=us")
        # Check for CAPTCHA
        url = await self._evaluate("window.location.href")
        if "/sorry/" in (url or ""):
            raise RuntimeError(
                f"Google CAPTCHA during warmup. "
                "If using self-launch, try with your normal Chrome: "
                "chrome --remote-debugging-port=9222, then --cdp-url http://127.0.0.1:9222"
            )

    async def ask(self, question: str, timeout_ms: int = 45000) -> str:
        """Ask a question via Google AI Mode."""
        async with self._lock:
            js = _ASK_JS.replace("%QUERY%", json.dumps(question))
            try:
                result = await asyncio.wait_for(self._evaluate(js), timeout=timeout_ms / 1000)
            except asyncio.TimeoutError:
                raise RuntimeError("Query timed out")
            except Exception:
                # Try recovery
                await self._warmup()
                result = await self._evaluate(js)

        if isinstance(result, dict):
            if result.get("error"):
                raise RuntimeError(f"{result['error']}: {result.get('message','')}")
            return result.get("answer", "")
        return str(result) if result else ""

    async def ask_stream(self, question: str, timeout_ms: int = 45000):
        """Yield answer in one chunk."""
        text = await self.ask(question, timeout_ms)
        if text:
            yield text

    async def stop(self):
        """Shutdown."""
        if self._ws:
            await self._ws.close()
        if self._proc:
            self._proc.terminate()
            self._proc.wait()
        if self._user_data_dir:
            shutil.rmtree(self._user_data_dir, ignore_errors=True)


async def e2e_test():
    import time
    cdp = os.environ.get("CDP_URL")
    engine = AIModeEngine()
    print(f"Starting... (cdp={cdp or 'self-launch'})")
    t0 = time.time()
    await engine.start(cdp_url=cdp)
    print(f"  Ready in {time.time()-t0:.1f}s")

    tests = [
        ("math", "what is 7*8? answer only the number"),
        ("web", "what is the current bitcoin price in USD today?"),
        ("chinese", "用中文简要介绍量子计算, 不超过2句话"),
    ]
    passed = 0
    for name, q in tests:
        t0 = time.time()
        try:
            ans = await engine.ask(q)
            print(f"  [{name}] ({time.time()-t0:.1f}s): {ans[:120]}")
            if ans:
                passed += 1
        except Exception as e:
            print(f"  [{name}] ERROR: {e}")

    await engine.stop()
    print(f"\n{'PASSED' if passed == len(tests) else 'PARTIAL'} ({passed}/{len(tests)})")


if __name__ == "__main__":
    asyncio.run(e2e_test())
