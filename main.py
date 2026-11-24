"""HTTP entry points for the public planner."""

import json
import logging
import os
import time
from collections import OrderedDict, deque
from functools import lru_cache
from ipaddress import ip_address
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from planner import run_plan

app = FastAPI(title="Perfect Saturday Planner")
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")
recent = OrderedDict()


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/health")
def health(request: Request, details: bool = False):
    """Report service health and, on request, integration availability without exposing keys."""
    if not details:
        return {"status": "ok"}
    _, source = visitor_identity(request)
    return {
        "status": "ok",
        "openai_configured": bool(os.getenv("OPENAI_API_KEY", "").strip()),
        "geoapify_configured": bool(os.getenv("GEOAPIFY_API_KEY", "").strip()),
        "rate_limit_identity": source,
    }


def visitor_identity(request):
    # This override applies only behind Render's public Cloudflare ingress.
    if os.getenv("RENDER") == "true":
        try:
            return str(ip_address(request.headers.get("cf-connecting-ip", ""))), "CF-Connecting-IP"
        except ValueError:
            pass
    return (request.client.host if request.client else "visitor"), "socket peer"


@lru_cache(maxsize=2)
def log_identity_source(source):
    logging.getLogger("uvicorn.error").info("Planner rate-limit identity source: %s", source)


def visitor_key(request):
    key, source = visitor_identity(request)
    if os.getenv("RENDER") == "true":
        log_identity_source(source)
    return key


def rate_limited(key):
    """Apply a rolling one-minute allowance in this worker and evict the least recently used visitor bucket at capacity."""
    now = time.monotonic()
    history = recent.setdefault(key, deque())
    recent.move_to_end(key)
    if len(recent) > 1024:
        recent.popitem(last=False)
    while history and history[0] < now - 60:
        history.popleft()
    if len(history) >= 12:
        return True
    history.append(now)
    return False


@app.post("/api/plan")
async def plan(request: Request):
    """Read a size-limited request and stream one newline-delimited JSON event per planning step."""
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 18000:
            return JSONResponse({"error": "Please shorten the request."}, 413)
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeError):
        return JSONResponse({"error": "Send a JSON object or a quoted text request."}, 400)

    if rate_limited(visitor_key(request)):
        return JSONResponse(
            {"error": "Please wait a minute before making another plan."},
            429,
            headers={"Retry-After": "60"},
        )
    value = body.get("input", body) if isinstance(body, dict) else body

    async def stream():
        try:
            async for event in run_plan(value):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except Exception:
            yield (
                json.dumps(
                    {
                        "type": "empty",
                        "message": "Planning was interrupted. Please retry or adjust your details.",
                    }
                )
                + "\n"
            )

    return StreamingResponse(
        stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
