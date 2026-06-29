"""Lightweight Playwright engine for Google AI Mode.

Architecture: single browser page, all queries run as JS fetch() inside it.
This gives Chrome TLS fingerprint (no rate limit) without page navigation overhead.

Performance: ~1s/query, 60+ asks/minute sustained, zero 429.
"""
import asyncio
import json
import re
from playwright.async_api import async_playwright, Page


_ASK_JS = """
async (q) => {
    const pageUrl = 'https://www.google.com.hk/search?q=' + encodeURIComponent(q) + '&hl=en&gl=us&udm=50&aep=1&ntc=1';
    const r1 = await fetch(pageUrl, {credentials:'include'});
    const html = await r1.text();
    const m = (p) => { const x = html.match(p); return x ? x[1] : ''; };
    const srtst = m(/data-srtst="([^"]+)"/);
    if (!srtst) return {error:'no_token', htmlLen:html.length};
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
    const fh = await r2.text();
    const div = document.createElement('div');
    div.innerHTML = fh;
    // Extract answer: pTRUV is the formatted answer div
    let text = '';
    const pTRUV = div.querySelector('.pTRUV');
    if (pTRUV) {
        pTRUV.querySelectorAll('script,style,button,span[style*="display:none"]').forEach(x => x.remove());
        text = pTRUV.textContent.trim();
    }
    // Fallback: n6owBd container (broader)
    if (!text) {
        const n6 = div.querySelector('.n6owBd');
        if (n6) {
            n6.querySelectorAll('script,style,button,span[style*="display:none"],.LGKDTe,.SGF5Lb,.YoEHmf').forEach(x => x.remove());
            text = n6.textContent.trim();
        }
    }
    // Fallback: largest dir="ltr" block excluding citations
    if (!text) {
        div.querySelectorAll('[dir="ltr"]').forEach(el => {
            el.querySelectorAll('script,style,button').forEach(x => x.remove());
            const t = el.textContent.trim();
            if (t.length > text.length && !el.classList.contains('mZJni')) text = t;
        });
    }
    return {ok:true, answer:text, folwrLen:fh.length};
}
"""


class AIModeEngine:
    """Single-page browser engine. All queries run as fetch() inside one tab."""

    def __init__(self):
        self._pw = None
        self._browser = None
        self._page = None
        self._lock = asyncio.Lock()

    async def start(self, cdp_url=None, headless=True, channel="chrome"):
        self._pw = await async_playwright().start()
        if cdp_url:
            self._browser = await self._pw.chromium.connect_over_cdp(cdp_url)
            ctx = self._browser.contexts[0] if self._browser.contexts else await self._browser.new_context()
        else:
            self._browser = await self._pw.chromium.launch(headless=headless, channel=channel)
            ctx = await self._browser.new_context()
        self._page = await ctx.new_page()
        await self._page.goto("https://www.google.com.hk/", wait_until="domcontentloaded")
        await self._page.wait_for_timeout(1000)

    async def ask(self, question: str, timeout_ms: int = 45000) -> str:
        async with self._lock:
            result = await self._page.evaluate(_ASK_JS, question)
        if isinstance(result, dict):
            if result.get("error"):
                raise RuntimeError(result["error"])
            return result.get("answer", "")
        return str(result)

    async def ask_stream(self, question: str, timeout_ms: int = 45000):
        """Yield text in one chunk (folwr doesn't truly stream via fetch)."""
        text = await self.ask(question, timeout_ms)
        if text:
            yield text

    async def stop(self):
        if self._page:
            await self._page.close()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()


async def e2e_test():
    import os, time
    cdp = os.environ.get("CDP_URL")
    engine = AIModeEngine()
    print(f"Starting... (cdp={cdp or 'self-launch'})")
    await engine.start(cdp_url=cdp)

    tests = [
        ("math", "what is 7*8? answer only the number"),
        ("web", "what is the current bitcoin price in USD today?"),
        ("chinese", "用中文简要介绍量子计算, 不超过2句话"),
    ]
    for name, q in tests:
        t0 = time.time()
        try:
            ans = await engine.ask(q)
            print(f"  [{name}] ({time.time()-t0:.1f}s): {ans[:120]}")
        except Exception as e:
            print(f"  [{name}] ERROR: {e}")

    await engine.stop()


if __name__ == "__main__":
    asyncio.run(e2e_test())
