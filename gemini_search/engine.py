"""Lightweight engine for Google AI Mode.

The default backend launches a real Chrome/Chromium subprocess and connects to
it through the Chrome DevTools Protocol (CDP). An optional
undetected-chromedriver backend can be enabled when a normal subprocess profile
is challenged by Google CAPTCHA.
"""
from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

try:
    import websockets
except ImportError:  # pragma: no cover - surfaced at runtime by _connect_cdp
    websockets = None


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



def _env_or_value(value: Optional[str], *env_names: str) -> Optional[str]:
    """Return an explicit value, or the first non-empty environment value."""
    if value:
        return value
    for name in env_names:
        candidate = os.environ.get(name)
        if candidate:
            return candidate
    return None


def _version_sort_key(path: Path) -> tuple[int, ...]:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)\.(\d+)", str(path))
    if not match:
        return ()
    return tuple(int(part) for part in match.groups())


def _existing(paths: list[Path]) -> list[str]:
    return [str(path) for path in paths if path.is_file()]


def _find_chrome(channel: str = "chrome") -> str:
    """Find a Chrome/Edge/Chromium binary path on the system."""
    system = platform.system()
    requested = (channel or "chrome").lower()
    candidates: list[str] = []

    explicit = _env_or_value(None, "CHROME_PATH", "UC_CHROME_BINARY")
    if explicit:
        candidates.append(explicit)

    home = Path.home()
    if system == "Windows":
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            cft_root = Path(local_appdata) / "agent-browser-cli" / "chrome-for-testing"
            cft = sorted(
                cft_root.glob("*/chrome-win64/chrome.exe"),
                key=_version_sort_key,
                reverse=True,
            )
            candidates.extend(str(path) for path in cft)

        by_channel: dict[str, list[Path]] = {"chrome": [], "msedge": [], "chromium": []}
        for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base_value = os.environ.get(key)
            if not base_value:
                continue
            base = Path(base_value)
            by_channel["chrome"].append(base / "Google" / "Chrome" / "Application" / "chrome.exe")
            by_channel["msedge"].append(base / "Microsoft" / "Edge" / "Application" / "msedge.exe")
        if requested in by_channel:
            candidates.extend(_existing(by_channel[requested]))
        for name, paths in by_channel.items():
            if name != requested:
                candidates.extend(_existing(paths))
        candidates.extend(["chrome.exe", "msedge.exe"])
    elif system == "Darwin":
        by_channel = {
            "chrome": [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")],
            "msedge": [Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")],
            "chromium": [Path("/Applications/Chromium.app/Contents/MacOS/Chromium")],
        }
        if requested in by_channel:
            candidates.extend(_existing(by_channel[requested]))
        for name, paths in by_channel.items():
            if name != requested:
                candidates.extend(_existing(paths))
    else:
        local_chromes: list[Path] = []
        for root in (
            home / ".local/share/browser-binaries/puppeteer/chrome",
            home / ".local/share/browser-binaries/ms-playwright",
        ):
            local_chromes.extend(root.glob("**/chrome-linux64/chrome"))
        candidates.extend(
            str(path)
            for path in sorted(local_chromes, key=_version_sort_key, reverse=True)
        )
        linux_by_channel = {
            "chrome": ["google-chrome", "google-chrome-stable", "chrome"],
            "msedge": ["microsoft-edge", "microsoft-edge-stable", "msedge"],
            "chromium": ["chromium", "chromium-browser"],
        }
        if requested in linux_by_channel:
            candidates.extend(linux_by_channel[requested])
        for name, names in linux_by_channel.items():
            if name != requested:
                candidates.extend(names)

    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        path = Path(candidate).expanduser()
        if path.is_file() and (system == "Windows" or os.access(path, os.X_OK)):
            return str(path)
        found = shutil.which(candidate)
        if found:
            return found
    raise RuntimeError("Chrome/Edge/Chromium not found. Install Chrome or set CHROME_PATH env var.")


def _chrome_major_version(binary: str) -> Optional[int]:
    """Best-effort Chrome major version detection for undetected-chromedriver."""
    path_match = re.search(r"(?:^|[\\/])(\d+)\.\d+\.\d+\.\d+(?:[\\/]|$)", str(binary))
    if path_match:
        return int(path_match.group(1))

    try:
        cp = subprocess.run(
            [binary, "--version"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except Exception:
        return None
    text = f"{cp.stdout}\n{cp.stderr}"
    match = re.search(r"(\d+)\.\d+\.\d+\.\d+", text)
    return int(match.group(1)) if match else None


def _normalize_browser_backend(backend: Optional[str]) -> str:
    value = (backend or "subprocess").strip().lower()
    aliases = {
        "raw": "subprocess",
        "chrome": "subprocess",
        "subprocess": "subprocess",
        "uc": "undetected",
        "undetected": "undetected",
        "undetected-chromedriver": "undetected",
    }
    if value not in aliases:
        raise ValueError("browser_backend must be 'subprocess' or 'undetected'")
    return aliases[value]


class AIModeEngine:
    """Single-tab Chrome engine via raw CDP."""

    def __init__(self):
        self._proc = None
        self._uc_driver = None
        self._ws = None
        self._ws_url = None
        self._page_target = None
        self._lock = asyncio.Lock()
        self._msg_id = 0
        self._cdp_url = None
        self._user_data_dir = None
        self._owns_user_data_dir = False
        self._browser_backend = "subprocess"

    async def start(
        self,
        cdp_url=None,
        headless=True,
        channel="chrome",
        user_data_dir: Optional[str] = None,
        browser_backend: Optional[str] = None,
        proxy_server: Optional[str] = None,
        chromedriver_path: Optional[str] = None,
    ):
        """Start Chrome and connect via CDP.

        If cdp_url is provided, connects to an existing Chrome instance.
        Otherwise launches a browser using the selected backend:
        - subprocess: plain Chrome/Edge/Chromium with minimal flags.
        - undetected: undetected-chromedriver + CDP, useful for CAPTCHA probes.

        user_data_dir can be supplied to persist cookies across runs. When it is
        omitted, a temporary profile is created and deleted on stop.
        """
        self._cdp_url = cdp_url
        self._browser_backend = _normalize_browser_backend(
            _env_or_value(browser_backend, "GEMINI_SEARCH_BROWSER_BACKEND")
        )
        try:
            if cdp_url:
                await self._connect_cdp(cdp_url)
            else:
                await self._launch_chrome(
                    headless=headless,
                    channel=channel,
                    user_data_dir=user_data_dir,
                    browser_backend=self._browser_backend,
                    proxy_server=proxy_server,
                    chromedriver_path=chromedriver_path,
                )
            await self._warmup()
        except Exception:
            await self.stop()
            raise

    def _prepare_user_data_dir(self, user_data_dir: Optional[str]) -> str:
        if user_data_dir:
            profile_path = Path(user_data_dir).expanduser().resolve()
            profile_path.mkdir(parents=True, exist_ok=True)
            self._user_data_dir = str(profile_path)
            self._owns_user_data_dir = False
        else:
            self._user_data_dir = tempfile.mkdtemp(prefix="gemini-search-mcp-")
            self._owns_user_data_dir = True
        return self._user_data_dir

    async def _launch_chrome(
        self,
        headless=True,
        channel="chrome",
        user_data_dir: Optional[str] = None,
        browser_backend: Optional[str] = None,
        proxy_server: Optional[str] = None,
        chromedriver_path: Optional[str] = None,
    ):
        backend = _normalize_browser_backend(browser_backend)
        if backend == "undetected":
            await self._launch_undetected_chrome(
                headless=headless,
                channel=channel,
                user_data_dir=user_data_dir,
                proxy_server=proxy_server,
                chromedriver_path=chromedriver_path,
            )
            return
        await self._launch_subprocess_chrome(
            headless=headless,
            channel=channel,
            user_data_dir=user_data_dir,
            proxy_server=proxy_server,
        )

    async def _launch_subprocess_chrome(
        self,
        headless=True,
        channel="chrome",
        user_data_dir: Optional[str] = None,
        proxy_server: Optional[str] = None,
    ):
        """Launch Chrome subprocess with minimal automation footprint."""
        chrome_path = _find_chrome(channel)
        profile_dir = self._prepare_user_data_dir(user_data_dir)
        port = int(os.environ.get("GEMINI_SEARCH_CDP_PORT", "19250"))
        proxy = _env_or_value(proxy_server, "GEMINI_SEARCH_PROXY_SERVER")

        args = [
            chrome_path,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-timer-throttling",
        ]
        if proxy:
            args.append(f"--proxy-server={proxy}")
        if headless:
            args.append("--headless=new")
        args.append("about:blank")

        self._proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        await self._wait_for_cdp(port, f"Chrome subprocess (pid={self._proc.pid})")
        await self._connect_cdp(f"http://127.0.0.1:{port}")

    async def _launch_undetected_chrome(
        self,
        headless=True,
        channel="chrome",
        user_data_dir: Optional[str] = None,
        proxy_server: Optional[str] = None,
        chromedriver_path: Optional[str] = None,
    ):
        """Launch Chrome through undetected-chromedriver, then use CDP."""
        try:
            import undetected_chromedriver as uc
        except ImportError as exc:
            raise RuntimeError(
                "undetected backend requires: pip install -e '.[undetected]'"
            ) from exc

        chrome_path = _env_or_value(None, "UC_CHROME_BINARY", "CHROME_PATH") or _find_chrome(channel)
        profile_dir = self._prepare_user_data_dir(user_data_dir)
        port = int(os.environ.get("GEMINI_SEARCH_CDP_PORT", "19250"))
        proxy = _env_or_value(proxy_server, "GEMINI_SEARCH_PROXY_SERVER")
        driver_path = _env_or_value(
            chromedriver_path,
            "GEMINI_SEARCH_CHROMEDRIVER",
            "UC_CHROMEDRIVER",
        )

        options = uc.ChromeOptions()
        options.binary_location = chrome_path
        options.add_argument("--lang=en-US,en")
        options.add_argument("--window-size=1365,900")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--no-first-run")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        if proxy:
            options.add_argument(f"--proxy-server={proxy}")

        self._uc_driver = uc.Chrome(
            options=options,
            user_data_dir=profile_dir,
            driver_executable_path=driver_path or None,
            browser_executable_path=chrome_path,
            version_main=_chrome_major_version(chrome_path),
            port=port,
            headless=headless,
            use_subprocess=True,
        )
        await self._wait_for_cdp(port, "undetected-chromedriver Chrome")
        await self._connect_cdp(f"http://127.0.0.1:{port}")

    async def _wait_for_cdp(self, port: int, label: str, timeout_sec: float = 20.0):
        """Wait until Chrome exposes /json/version on the requested CDP port."""
        import urllib.request

        last_error = None
        attempts = max(1, int(timeout_sec * 2))
        for _ in range(attempts):
            await asyncio.sleep(0.5)
            try:
                data = urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/version", timeout=2
                ).read()
                info = json.loads(data)
                self._ws_url = info["webSocketDebuggerUrl"]
                return
            except Exception as exc:  # noqa: BLE001 - diagnostic retry loop
                last_error = exc
        raise RuntimeError(f"{label} did not expose CDP on port {port}: {last_error}")

    async def _connect_cdp(self, http_url):
        """Connect to Chrome via CDP WebSocket."""
        import urllib.request

        try:
            data = urllib.request.urlopen(f"{http_url}/json/version", timeout=5).read()
            info = json.loads(data)
            self._ws_url = info["webSocketDebuggerUrl"]
        except Exception as e:
            raise RuntimeError(f"Cannot connect to Chrome at {http_url}: {e}")

        pages = json.loads(urllib.request.urlopen(f"{http_url}/json/list", timeout=5).read())
        page_targets = [p for p in pages if p.get("type") == "page"]
        if page_targets:
            self._page_target = page_targets[0]["webSocketDebuggerUrl"]
        else:
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
        for _ in range(60):
            msg = json.loads(await self._ws.recv())
            if msg.get("method") == "Page.loadEventFired":
                break
        await asyncio.sleep(1)

    async def _warmup(self):
        """Navigate to Google search to build cookie session."""
        await self._navigate("https://www.google.com.hk/search?q=hello&hl=en&gl=us")
        url = await self._evaluate("window.location.href")
        if "/sorry/" in (url or ""):
            raise RuntimeError(
                "Google CAPTCHA during warmup. Try a visible persistent profile, "
                "an existing Chrome via --cdp-url, or --browser-backend undetected --no-headless."
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
        """Shutdown browser resources and remove owned temporary profile."""
        if self._ws:
            await self._ws.close()
            self._ws = None
        if self._uc_driver:
            try:
                self._uc_driver.quit()
            except Exception:
                pass
            self._uc_driver = None
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=5)
            self._proc = None
        if self._user_data_dir:
            if self._owns_user_data_dir:
                shutil.rmtree(self._user_data_dir, ignore_errors=True)
            self._user_data_dir = None
            self._owns_user_data_dir = False


async def e2e_test():
    import time

    cdp = os.environ.get("CDP_URL")
    channel = os.environ.get("BROWSER_CHANNEL", "chrome")
    headless = os.environ.get("HEADLESS", "1") != "0"
    browser_backend = os.environ.get("GEMINI_SEARCH_BROWSER_BACKEND")
    user_data_dir = os.environ.get("GEMINI_SEARCH_USER_DATA_DIR")
    proxy_server = os.environ.get("GEMINI_SEARCH_PROXY_SERVER")
    chromedriver_path = os.environ.get("GEMINI_SEARCH_CHROMEDRIVER") or os.environ.get("UC_CHROMEDRIVER")
    engine = AIModeEngine()
    print(
        "Starting... "
        f"(cdp={cdp or 'self-launch'}, backend={browser_backend or 'subprocess'}, "
        f"channel={channel}, headless={headless})"
    )
    t0 = time.time()
    await engine.start(
        cdp_url=cdp,
        headless=headless,
        channel=channel,
        user_data_dir=user_data_dir,
        browser_backend=browser_backend,
        proxy_server=proxy_server,
        chromedriver_path=chromedriver_path,
    )
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
