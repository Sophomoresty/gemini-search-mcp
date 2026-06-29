# gemini-search-mcp

<p align="center">
  <img src="logo.png" width="200" alt="gemini-search-mcp logo">
</p>

MCP server for web search powered by Google AI Mode (Gemini). Free, unlimited, no API key.

## What is this

An MCP server that gives any AI agent (Claude, Cursor, Windsurf, etc.) the ability to search the web in real-time using Google's AI Mode — the same Gemini-powered search that lives in the "AI Mode" tab on Google Search.

Think of it as a free, unlimited alternative to Grok MCP / Tavily / SerpAPI, backed by Google's search index.

## Features

- **Free**: No API key, no subscription, no quota
- **Unlimited**: 60+ requests/min with zero rate limiting
- **Google quality**: Powered by Gemini + Google Search (grounded in real web results)
- **MCP native**: Works with Claude Desktop, Claude Code, Cursor, Windsurf, Cline
- **Also ships OpenAI API**: `/v1/chat/completions` for non-MCP clients
- **Fast**: ~1.5s average response time

## Quick Start

```bash
pip install playwright fastapi uvicorn "mcp[cli]"
playwright install chrome
```

### MCP Server (for AI agents)

```bash
python mcp_server.py
```

### OpenAI-compatible API

```bash
python -m gemini_search --port 8080
```

## MCP Integration

### Claude Code

```bash
claude mcp add gemini-search -- python /path/to/gemini-search-mcp/mcp_server.py
```

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "gemini-search": {
      "command": "python",
      "args": ["/path/to/gemini-search-mcp/mcp_server.py"],
      "env": {
        "CDP_URL": "http://127.0.0.1:9222"
      }
    }
  }
}
```

### Cursor / Windsurf

Same pattern — point to `mcp_server.py` as an stdio MCP server.

## MCP Tools

| Tool | Description |
|------|-------------|
| `web_search(query)` | Search the web and get a synthesized answer grounded in real-time results |
| `ask(prompt)` | General question — AI Mode auto-decides whether to search the web |

### Examples

```
web_search("latest AI regulation news 2026")
→ "The EU AI Act enforcement began on June 1, 2026, requiring..."

web_search("Bitcoin price today")
→ "As of June 30, 2026, Bitcoin is trading at $59,687 USD..."

ask("what is 1847 * 293")
→ "541171"
```

## OpenAI API Usage

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-search","messages":[{"role":"user","content":"What happened in the news today?"}]}'
```

| Field | Value |
|-------|-------|
| Base URL | `http://localhost:8080/v1` |
| API Key | anything |
| Model | `gemini-search` |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CDP_URL` | (none) | Chrome DevTools URL. If set, connects to existing Chrome instead of launching one |
| `BROWSER_CHANNEL` | `chrome` | Browser to use: `chrome`, `msedge`, `chromium` |
| `HEADLESS` | `1` | Set to `0` to show browser window |

## How It Works

Google rate-limits by TLS fingerprint quality — not by IP. Automated HTTP clients (curl, requests, httpx) get throttled after a few requests. But a real Chrome browser's `fetch()` calls are trusted unconditionally.

This tool runs a single Playwright page and executes all queries as `fetch()` inside it, giving every request an authentic Chrome TLS/HTTP2 fingerprint. Google sees normal browser traffic and applies no rate limits.

```
Agent calls web_search("query")
  → Playwright page.evaluate(fetch)
    → Google Search AI Mode (token extraction + folwr endpoint)
      → Parse answer from HTML response
        → Return to agent
```

## Comparison

| | gemini-search-mcp | Grok MCP | Tavily |
|---|---|---|---|
| Cost | **Free** | xAI API key ($) | API key ($) |
| Rate limit | **None** | API quota | API quota |
| Search backend | Google Search | Grok + web | Proprietary |
| Answer quality | Gemini synthesized | Grok synthesized | Extracted snippets |
| Setup | Chrome + playwright | API key | API key |

## Docker

```bash
docker compose up -d
```

## Requirements

- Python 3.10+
- Chrome, Edge, or Chromium
- `playwright`, `fastapi`, `uvicorn`, `mcp[cli]`

## Limitations

- Requires Chrome/Edge/Chromium installed
- No conversation memory between requests
- Answer extraction relies on Google's DOM structure (may break on updates)
- Streaming is chunked, not per-token

## Acknowledgments

- [GenericAgent](https://github.com/lsdefine/GenericAgent) — 本项目核心开发依仗 GA 提供的 AI 能力
- [linux.do](https://linux.do) community

## License

MIT
