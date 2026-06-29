"""Google AI Mode MCP Server — free web search for AI agents.

Exposes Google Search AI Mode as MCP tools. Any MCP-compatible client
(Claude Desktop, Claude Code, Cursor, etc.) can call these tools to get
real-time web-grounded answers powered by Gemini — zero API key, unlimited.
"""
import asyncio
from typing import Optional
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from google_ai_mode.engine import AIModeEngine

mcp = FastMCP(name="Google AI Mode")
READONLY = ToolAnnotations(readOnlyHint=True)

_engine: Optional[AIModeEngine] = None
_lock = asyncio.Lock()


async def _get_engine() -> AIModeEngine:
    global _engine
    if _engine is None:
        async with _lock:
            if _engine is None:
                import os
                _engine = AIModeEngine()
                cdp = os.environ.get("CDP_URL")
                channel = os.environ.get("BROWSER_CHANNEL", "chrome")
                headless = os.environ.get("HEADLESS", "1") != "0"
                await _engine.start(cdp_url=cdp, headless=headless, channel=channel)
    return _engine


@mcp.tool(annotations=READONLY)
async def web_search(
    query: str,
) -> str:
    """Search the web using Google AI Mode and get a synthesized answer with sources.

    Uses Google Search's AI Mode (powered by Gemini) to search the web in
    real-time and return a comprehensive, grounded answer. Results include
    information from current web pages, news, and data.

    This is equivalent to using Google Search's "AI Mode" tab — the AI reads
    multiple web sources and synthesizes an answer, similar to Perplexity or
    Grok's web search, but powered by Google's search index.

    Args:
        query: Search query or question. Can be anything you'd type into Google.
               Examples: "latest news about AI regulation", "Bitcoin price today",
               "how does mRNA vaccine work", "Python asyncio best practices 2026"

    Returns:
        A synthesized answer based on real-time web search results.
        The answer is grounded in actual web content found by Google.
    """
    engine = await _get_engine()
    return await engine.ask(query)


@mcp.tool(annotations=READONLY)
async def ask(
    prompt: str,
) -> str:
    """Ask Google AI Mode any question and get an AI-generated answer.

    Similar to web_search but intended for general questions that may or may
    not require web search. Google AI Mode will automatically decide whether
    to search the web or answer from its training data.

    Args:
        prompt: Any question or instruction. Google AI Mode will search the web
                if needed and synthesize an answer.

    Returns:
        AI-generated answer, potentially grounded in web search results.
    """
    engine = await _get_engine()
    return await engine.ask(prompt)


def main():
    mcp.run(transport='stdio')


if __name__ == "__main__":
    main()
