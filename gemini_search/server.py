"""OpenAI-compatible API server using Playwright fetch engine."""
import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
import uvicorn

from .engine import AIModeEngine


engine = AIModeEngine()


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = app.state.config
    await engine.start(cdp_url=cfg.get("cdp_url"), headless=cfg["headless"], channel=cfg["channel"])
    print("AI Mode engine ready")
    yield
    await engine.stop()


app = FastAPI(lifespan=lifespan)


@app.get("/v1/models")
async def list_models():
    return {"object": "list", "data": [{"id": "gemini-search", "object": "model", "created": 1719600000, "owned_by": "google"}]}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    messages = body.get("messages", [])
    stream = body.get("stream", False)
    model = body.get("model", "gemini-search")

    prompt = _build_prompt(messages)
    if not prompt:
        return JSONResponse({"error": {"message": "No user message", "type": "invalid_request_error"}}, status_code=400)

    cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    if stream:
        return StreamingResponse(_stream(prompt, cid, created, model), media_type="text/event-stream",
                                 headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})

    text = await engine.ask(prompt)
    return {"id": cid, "object": "chat.completion", "created": created, "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": len(prompt.split()), "completion_tokens": len(text.split()), "total_tokens": len(prompt.split()) + len(text.split())}}


async def _stream(prompt, cid, created, model):
    yield _sse({"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
    async for chunk in engine.ask_stream(prompt):
        yield _sse({"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                    "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}]})
    yield _sse({"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    yield "data: [DONE]\n\n"


def _sse(d): return f"data: {json.dumps(d, ensure_ascii=False)}\n\n"


def _build_prompt(messages):
    system = user = ""
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if p.get("type") == "text")
        if role == "system": system = content
        elif role == "user": user = content
    return f"{system}\n\n{user}".strip() if system else user


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Gemini Search → OpenAI API (Playwright, unlimited)")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--cdp-url", default=None, help="Connect to existing Chrome (e.g. http://127.0.0.1:9222)")
    parser.add_argument("--channel", default="chrome", choices=["chrome", "msedge", "chromium"])
    parser.add_argument("--no-headless", action="store_true")
    args = parser.parse_args()

    app.state.config = {"cdp_url": args.cdp_url, "headless": not args.no_headless, "channel": args.channel}
    print(f"gemini-search-mcp v0.4.0")
    print(f"  API: http://{args.host}:{args.port}/v1")
    print(f"  Browser: {args.cdp_url or f'{args.channel} (headless={not args.no_headless})'}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
