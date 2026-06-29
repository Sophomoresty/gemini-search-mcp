# google-ai-mode

<p align="center">
  <img src="logo.png" width="200" alt="google-ai-mode logo">
</p>

Convert Google Search AI Mode into an OpenAI-compatible API. Unlimited usage, real-time web search, zero cost.

## Features

- **Unlimited**: No rate limits — leverages real Chrome TLS fingerprint
- **OpenAI Compatible**: Drop-in `/v1/chat/completions` and `/v1/models`
- **Web Grounded**: Responses include real-time web search results (today's news, live prices, etc.)
- **Fast**: ~1.5s average response time
- **Streaming**: SSE streaming support
- **Lightweight**: Single browser page, ~120 lines of core logic
- **Cross-Platform**: Windows / macOS / Linux

## How It Works

Google Search AI Mode (powered by Gemini) provides AI answers grounded in live web results. This tool runs a single Playwright page and executes all queries as `fetch()` calls inside it — giving every request a real Chrome TLS/HTTP2 fingerprint. Google's anti-bot system trusts these requests unconditionally, enabling unlimited usage without rate limits.

```
User query → Playwright page.evaluate(fetch) → Google AI Mode → Parse HTML → OpenAI response
```

## Quick Start

```bash
pip install playwright fastapi uvicorn
playwright install chrome

python -m google_ai_mode
```

Server starts at `http://localhost:8080/v1`.

### Connect to existing Chrome (recommended)

```bash
# Launch Chrome with debug port
google-chrome --remote-debugging-port=9222

# Start the server
python -m google_ai_mode --cdp-url http://127.0.0.1:9222
```

## Usage

```bash
# Non-streaming
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"google-ai-mode","messages":[{"role":"user","content":"What happened in the news today?"}]}'

# Streaming
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"google-ai-mode","stream":true,"messages":[{"role":"user","content":"Explain quantum computing"}]}'
```

### Client Configuration

| Field | Value |
|-------|-------|
| Base URL | `http://localhost:8080/v1` |
| API Key | anything |
| Model | `google-ai-mode` |

Works with Cherry Studio, ChatBox, Open WebUI, LobeChat, and any OpenAI-compatible client.

## Options

```
python -m google_ai_mode [OPTIONS]

--port          API port (default: 8080)
--host          Bind address (default: 0.0.0.0)
--cdp-url       Connect to existing Chrome (e.g. http://127.0.0.1:9222)
--channel       Browser: chrome, msedge, chromium (default: chrome)
--no-headless   Show browser window for debugging
```

## Docker

```bash
docker compose up -d
```

## Performance

| Metric | Value |
|--------|-------|
| Average latency | ~1.5s |
| Sustained throughput | 60+ asks/min |
| Rate limit | None (real Chrome fingerprint) |
| Web search | Built-in (Gemini native) |

## Limitations

- Requires Chrome/Edge/Chromium installed (Playwright drives it headless)
- No conversation memory between requests (each is independent)
- Streaming is chunked (1-3 chunks per response, not per-token)
- Answer extraction relies on Google's CSS class names which may change

## Requirements

- Python 3.10+
- Chrome, Edge, or Chromium browser
- `playwright`, `fastapi`, `uvicorn`

## How It Differs from gemini-web2api

| | gemini-web2api | google-ai-mode |
|---|---|---|
| Protocol | Pure HTTP (gemini.google.com) | Playwright fetch (Google Search) |
| Rate limit | Moderate (IP-based) | **None** |
| Web search | Gemini native | Google Search native (with citations) |
| Speed | ~3.5s | ~1.5s |
| Dependencies | Zero | playwright |

## Acknowledgments

- [GenericAgent](https://github.com/lsdefine/GenericAgent) — 本项目核心开发依仗 GA 提供的 AI 能力
- [linux.do](https://linux.do) community

## License

MIT
