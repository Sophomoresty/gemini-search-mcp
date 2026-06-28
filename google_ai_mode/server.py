"""OpenAI-compatible API server for Google AI Mode (pure protocol, no browser)."""
import json
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
import uvicorn

from .protocol import AIModeClient


CONFIG = {
    "cookies": "",
    "cookie_file": None,
    "api_keys": [],
    "proxy": None,
}


def _load_cookies() -> str:
    if CONFIG["cookies"]:
        return CONFIG["cookies"]
    f = CONFIG.get("cookie_file")
    if f:
        try:
            with open(f) as fh:
                return fh.read().strip()
        except Exception:
            pass
    return ""


def _make_client() -> AIModeClient:
    return AIModeClient(cookies=_load_cookies(), proxy=CONFIG.get("proxy"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("google-ai-mode (pure protocol) ready")
    yield


app = FastAPI(lifespan=lifespan)


def _check_auth(request: Request) -> bool:
    keys = CONFIG.get("api_keys") or []
    if not keys:
        return True
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:] in keys
    xk = request.headers.get("x-api-key", "")
    return xk in keys


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [{
            "id": "google-ai-mode",
            "object": "model",
            "created": 1719600000,
            "owned_by": "google",
        }],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    if not _check_auth(request):
        return JSONResponse({"error": {"message": "invalid api key", "type": "invalid_request_error"}}, status_code=401)

    body = await request.json()
    messages = body.get("messages", [])
    stream = body.get("stream", False)
    model = body.get("model", "google-ai-mode")

    prompt = _build_prompt(messages)
    if not prompt:
        return JSONResponse({"error": {"message": "No user message", "type": "invalid_request_error"}}, status_code=400)

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    if stream:
        return StreamingResponse(
            _stream_response(prompt, completion_id, created, model),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
        )

    import asyncio
    loop = asyncio.get_event_loop()
    client = _make_client()
    try:
        text = await loop.run_in_executor(None, client.ask, prompt)
    except Exception as e:
        return JSONResponse({"error": {"message": str(e), "type": "api_error"}}, status_code=502)

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": len(prompt.split()),
            "completion_tokens": len(text.split()),
            "total_tokens": len(prompt.split()) + len(text.split()),
        },
    }


async def _stream_response(prompt: str, completion_id: str, created: int, model: str):
    import asyncio
    yield _sse({"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})

    client = _make_client()
    loop = asyncio.get_event_loop()

    def _collect():
        chunks = []
        try:
            for chunk in client.ask_stream(prompt):
                chunks.append(chunk)
        except Exception as e:
            chunks.append(f"[error: {e}]")
        return chunks

    chunks = await loop.run_in_executor(None, _collect)
    for chunk in chunks:
        yield _sse({
            "id": completion_id, "object": "chat.completion.chunk", "created": created, "model": model,
            "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}],
        })

    yield _sse({
        "id": completion_id, "object": "chat.completion.chunk", "created": created, "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    })
    yield "data: [DONE]\n\n"


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _build_prompt(messages: list) -> str:
    system = ""
    user = ""
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if p.get("type") == "text")
        if role == "system":
            system = content
        elif role == "user":
            user = content
    if system and user:
        return f"{system}\n\n{user}"
    return user


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Google AI Mode → OpenAI API (pure protocol, no browser)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # With cookies file (exported from browser, includes HttpOnly)
  python -m google_ai_mode --cookie-file cookies.txt

  # Inline cookies
  python -m google_ai_mode --cookies "NID=...; AEC=..."

  # With API key auth
  python -m google_ai_mode --api-key sk-mykey
""",
    )
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--cookie-file", type=str, default=None, help="File containing Google cookies")
    parser.add_argument("--cookies", type=str, default=None, help="Inline Google cookie string")
    parser.add_argument("--api-key", type=str, default=None, action="append", help="Allowed API key (repeatable)")
    parser.add_argument("--proxy", type=str, default=None)
    args = parser.parse_args()

    if args.cookie_file:
        CONFIG["cookie_file"] = args.cookie_file
    if args.cookies:
        CONFIG["cookies"] = args.cookies
    if args.api_key:
        CONFIG["api_keys"] = args.api_key
    if args.proxy:
        CONFIG["proxy"] = args.proxy

    print(f"google-ai-mode v0.2.0 (pure protocol)")
    print(f"  Listening:  http://{args.host}:{args.port}/v1")
    print(f"  Cookies:    {'file:' + args.cookie_file if args.cookie_file else ('inline' if args.cookies else 'none (will bootstrap)')}")
    print(f"  Auth:       {'enabled (' + str(len(args.api_key)) + ' keys)' if args.api_key else 'disabled'}")
    print()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
