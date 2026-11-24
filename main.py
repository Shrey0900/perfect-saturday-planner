"""Public web and streaming API entry points."""
import json
import time
from collections import OrderedDict, deque
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from planner import run_plan

app=FastAPI(title="Perfect Saturday Planner")
STATIC=Path(__file__).parent/"static"
app.mount("/static",StaticFiles(directory=STATIC),name="static")
recent=OrderedDict()
@app.get("/")
def index():
    return FileResponse(STATIC/"index.html")

@app.get("/health")
def health():
    return {"status":"ok"}

@app.post("/api/plan")
async def plan(request: Request):
    # Limit body size even when a client omits Content-Length.
    raw=bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw)>18000: return JSONResponse({"error":"Please shorten the request."},413)
    try:
        obj=json.loads(raw)
    except (ValueError,UnicodeError):
        return JSONResponse({"error":"Send a JSON object or a quoted text request."},400)
    key=request.client.host if request.client else "visitor"
    now=time.monotonic()
    history=recent.setdefault(key,deque())
    recent.move_to_end(key)
    if len(recent)>1024: recent.popitem(last=False)
    while history and history[0]<now-60: history.popleft()
    if len(history)>=12: return JSONResponse({"error":"Please wait a minute before making another plan."},429)
    history.append(now)
    value=obj.get("input",obj) if isinstance(obj,dict) else obj
    async def stream():
        try:
            async for event in run_plan(value):
                yield json.dumps(event,ensure_ascii=False)+"\n"
        except Exception:
            yield json.dumps({"type":"empty","message":"Planning was interrupted. Please try again or edit your details."})+"\n"
    return StreamingResponse(stream(),media_type="application/x-ndjson",headers={"Cache-Control":"no-store","X-Accel-Buffering":"no"})
