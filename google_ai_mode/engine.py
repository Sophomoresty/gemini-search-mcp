"""Core engine: drive Google AI Mode via Playwright, extract streaming text.

Supports two modes:
1. Connect to external Chrome via CDP (recommended for anti-detect)
2. Launch built-in Chromium with stealth patches (for Docker)
"""
import asyncio
import os
from playwright.async_api import async_playwright, Page, Browser, BrowserContext


STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
window.chrome = {runtime: {}, loadTimes: () => {}, csi: () => {}};
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (params) => (
  params.name === 'notifications' ?
    Promise.resolve({state: Notification.permission}) :
    originalQuery(params)
);
Object.defineProperty(navigator, 'plugins', {
  get: () => {
    const plugins = [
      {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer'},
      {name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai'},
      {name: 'Native Client', filename: 'internal-nacl-plugin'},
    ];
    plugins.length = 3;
    return plugins;
  }
});
Object.defineProperty(navigator, 'maxTouchPoints', {get: () => 0});
"""


async def create_session(cdp_url: str = None) -> tuple:
    """Create a browser session connected to AI Mode.

    Args:
        cdp_url: Chrome DevTools Protocol URL. If None, launches built-in Chromium.

    Returns:
        (pw, browser, page) tuple
    """
    pw = await async_playwright().start()

    if cdp_url:
        browser = await pw.chromium.connect_over_cdp(cdp_url)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await context.new_page()
    else:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-infobars",
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--window-size=1280,720",
            ],
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720},
            locale="en-US",
            timezone_id="America/New_York",
        )
        await context.add_init_script(STEALTH_SCRIPT)
        page = await context.new_page()

    # Navigate to Google Search, enter AI Mode
    await page.goto(
        "https://www.google.com/search?q=hello&hl=en&gl=us",
        wait_until="domcontentloaded",
    )
    await page.wait_for_timeout(3000)

    if "/sorry/" in page.url:
        raise RuntimeError(f"CAPTCHA: {page.url} — use a real Chrome with --cdp-url")

    ai_link = page.locator("a:has-text('AI Mode')")
    if await ai_link.count() > 0:
        await ai_link.first.click()
        await page.wait_for_timeout(5000)
    else:
        raise RuntimeError("AI Mode tab not found on Google SERP")

    # Verify AI Mode loaded
    if "udm=50" not in page.url:
        raise RuntimeError(f"Failed to enter AI Mode. URL: {page.url}")

    return pw, browser, page


async def ask(page: Page, question: str, timeout_ms: int = 45000) -> str:
    """Submit a question and return the full response."""
    textarea = page.locator("textarea").last
    await textarea.wait_for(state="visible", timeout=10000)
    await textarea.fill(question)
    await textarea.press("Enter")

    await page.wait_for_timeout(2000)

    prev_text = ""
    stable_count = 0
    for _ in range(timeout_ms // 1500):
        text = await _extract_last_response(page)
        if text and text == prev_text:
            stable_count += 1
            if stable_count >= 3:
                break
        else:
            stable_count = 0
            prev_text = text
        await page.wait_for_timeout(1500)

    return prev_text


async def ask_stream(page: Page, question: str, timeout_ms: int = 45000):
    """Submit a question and yield text chunks as they arrive."""
    textarea = page.locator("textarea").last
    await textarea.wait_for(state="visible", timeout=10000)
    await textarea.fill(question)
    await textarea.press("Enter")

    await page.wait_for_timeout(2000)

    prev_text = ""
    stable_count = 0
    for _ in range(timeout_ms // 1000):
        text = await _extract_last_response(page)
        if text and len(text) > len(prev_text):
            delta = text[len(prev_text):]
            yield delta
            prev_text = text
            stable_count = 0
        elif text == prev_text and text:
            stable_count += 1
            if stable_count >= 4:
                break
        await page.wait_for_timeout(1000)


async def _extract_last_response(page: Page) -> str:
    """Extract the text of the latest AI Mode response."""
    return await page.evaluate("""() => {
        // Primary selector for AI Mode response blocks
        const blocks = document.querySelectorAll('[dir="ltr"].mZJni');
        if (blocks.length > 0) {
            return blocks[blocks.length - 1].innerText || '';
        }
        // Fallback: any large text block in the response area
        const turns = document.querySelectorAll('[data-subtree="aimc"]');
        if (turns.length > 0) {
            return turns[turns.length - 1].innerText || '';
        }
        return '';
    }""")


async def e2e_test():
    """End-to-end verification."""
    cdp_url = os.environ.get("CDP_URL", "http://127.0.0.1:19221")
    print(f"Connecting to {cdp_url}...")
    pw, browser, page = await create_session(cdp_url)

    try:
        print(f"AI Mode URL: {page.url}")
        print("\n[1] Non-streaming test: 'what is 2+2?'")
        answer = await ask(page, "what is 2+2? answer briefly")
        print(f"    → {answer}")

        print("\n[2] Streaming test: 'explain python in 2 sentences'")
        full = ""
        async for chunk in ask_stream(page, "explain python in 2 sentences"):
            full += chunk
            print(f"    +{len(chunk)} chars: {chunk[:60]}")
        print(f"    Total: {len(full)} chars")

        ok = len(answer) > 3 and len(full) > 20
        print(f"\n{'PASS' if ok else 'FAIL'}")
        return ok
    finally:
        await page.close()
        await pw.stop()


if __name__ == "__main__":
    asyncio.run(e2e_test())
