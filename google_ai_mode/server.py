"""OpenAI-compatible API server for Google AI Mode."""
import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
import uvicorn

from .engine import create_session, ask, ask_stream


_session = {"pw": None, "browser": None, "page": None, "lock": None}


@asynccontextmanager
async def lifespan(app: FastAPI):
    _session["lock"] = asyncio.Lock()
    cdp_url = app.state.cdp_url
    print(f"Connecting to Chrome at {cdp_url}...")
    pw, browser, page = await create_session(cdp_url)
    _session.update(pw=pw, browser=browser, page=page)
    print(f"AI Mode ready. Page: {page.url}")
    yield
    if _session["page"]:
        await _session["page"].close()
    if _session["pw"]:
        await _session["pw"].stop()


app = FastAPI(lifespan=lifespan)


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": "google-ai-mode",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "google",
            }
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    messages = body.get("messages", [])
    stream = body.get("stream", False)

    prompt = ""
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(p.get("text", "") for p in content if p.get("type") == "text")
            prompt = content

    if not prompt:
        return JSONResponse({"error": "No user message"}, status_code=400)

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    if stream:
        return StreamingResponse(
            _stream_response(prompt, completion_id, created),
            media_type="text/event-stream",
        )
    else:
        async with _session["lock"]:
            text = await ask(_session["page"], prompt)
        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": "google-ai-mode",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": len(prompt), "completion_tokens": len(text), "total_tokens": len(prompt) + len(text)},
        }


async def _stream_response(prompt: str, completion_id: str, created: int):
    async with _session["lock"]:
        async for chunk in ask_stream(_session["page"], prompt):
            data = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": "google-ai-mode",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": chunk},
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(data)}\n\n"

    # Send final chunk
    data = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": "google-ai-mode",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(data)}\n\n"
    yield "data: [DONE]\n\n"


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Google AI Mode → OpenAI API")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--cdp-url", type=str, default="http://127.0.0.1:19221",
                        help="Chrome DevTools Protocol URL")
    args = parser.parse_args()

    app.state.cdp_url = args.cdp_url
    print(f"google-ai-mode v0.1.0")
    print(f"  API: http://0.0.0.0:{args.port}/v1")
    print(f"  CDP: {args.cdp_url}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
