# google-ai-mode

Reverse-proxy Google Search AI Mode as an OpenAI-compatible API. Free, no API key, no Google account needed.

## How it works

Google Search has an "AI Mode" tab (powered by Gemini) that provides AI-generated answers with web search grounding. This tool drives a real Chrome browser via CDP to interact with AI Mode and exposes the responses through a standard OpenAI API.

## Features

- **OpenAI Compatible**: Drop-in `/v1/chat/completions` and `/v1/models`
- **Streaming**: SSE streaming support
- **Free**: No API key, no Google account, no rate limits (browser-based)
- **Web Grounded**: Responses include real-time web search results
- **Docker Ready**: One-command deployment

## Quick Start

### Option 1: Connect to existing Chrome (recommended)

Launch Chrome with remote debugging:

```bash
# macOS
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222

# Windows
chrome.exe --remote-debugging-port=9222

# Linux
google-chrome --remote-debugging-port=9222
```

Then run the server:

```bash
pip install -e .
python -m google_ai_mode --cdp-url http://127.0.0.1:9222
```

### Option 2: Docker

```bash
docker compose up -d
```

## Usage

```bash
# Non-streaming
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"google-ai-mode","messages":[{"role":"user","content":"Hello!"}]}'

# Streaming
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"google-ai-mode","stream":true,"messages":[{"role":"user","content":"Explain quantum computing"}]}'
```

### Client Configuration

| Field | Value |
|-------|-------|
| Base URL | `http://localhost:8080/v1` |
| API Key | anything (not validated) |
| Model | `google-ai-mode` |

## CLI Options

```
--port      API port (default: 8080)
--host      Bind host (default: 0.0.0.0)
--cdp-url   Chrome DevTools Protocol URL (default: http://127.0.0.1:9222)
```

## Anti-Detection Notes

Google aggressively detects headless/automated browsers. For reliable operation:

1. **Best**: Use a real Chrome instance with `--remote-debugging-port=9222`
2. **Good**: Use the Docker image (includes patched Chromium)
3. **Avoid**: Raw Playwright headless without stealth patches

If you get CAPTCHA errors, use option 1 with a real browser.

## Limitations

- Single browser session = sequential requests (one at a time)
- Response extraction is DOM-based; Google may change class names
- No conversation history (each request starts fresh in AI Mode)

## License

MIT
