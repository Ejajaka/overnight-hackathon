from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import agent, openai_client, transport
from .config import settings
from .sessions import store

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"

app = FastAPI(title="Source-Truth Listings")
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")


def _absolute(request: Request, path: str) -> str:
    if not path:
        return ""
    if settings.public_base_url:
        return f"{settings.public_base_url}{path}"
    return str(request.base_url).rstrip("/") + path


def _respond(request: Request, session, reply: dict) -> dict:
    return {
        "session_id": session.id,
        "messages": reply["messages"],
        "quick_replies": reply["quick_replies"],
        "listing": reply["listing"],
        "stage": reply["stage"],
        "buyer_url": _absolute(
            request,
            reply.get("buyer_path") or (f"/buyer/{quote(session.id)}" if reply["listing"] else ""),
        ),
        "mock_llm": not settings.llm_enabled,
    }


@app.get("/")
def index():
    return FileResponse(str(FRONTEND / "index.html"))


@app.get("/buyer/{sid}")
def buyer(sid: str):
    return FileResponse(str(FRONTEND / "buyer.html"))


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "llm_enabled": settings.llm_enabled,
        "twilio_enabled": settings.twilio_enabled,
    }


@app.post("/api/chat")
async def chat(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "invalid payload"}, status_code=400)
    session = store.get(payload.get("session_id"))
    reply = agent.handle_message(session, payload.get("message", ""))
    return _respond(request, session, reply)


@app.post("/api/reset")
async def reset(request: Request):
    payload = await request.json()
    sid = payload.get("session_id")
    if sid:
        store.remove(sid)
    return {"reset": True}


@app.post("/api/voice")
async def voice(request: Request, session_id: str = Form(""), audio: UploadFile = File(...)):
    session = store.get(session_id or None)
    data = await audio.read()
    text = openai_client.transcribe(data, audio.filename or "audio.webm")
    if not text:
        return {
            "session_id": session.id,
            "messages": [
                "I could not read that voice note. Please type your answer, or set "
                "OPENAI_API_KEY to enable transcription."
            ],
            "quick_replies": [],
            "listing": None,
            "stage": session.stage,
            "buyer_url": "",
            "mock_llm": not settings.llm_enabled,
            "transcript": "",
        }
    reply = agent.handle_message(session, text)
    result = _respond(request, session, reply)
    result["transcript"] = text
    return result


@app.get("/api/listing/{sid}")
def listing(sid: str):
    session = store.get(sid)
    if not session.audit:
        return JSONResponse({"error": "not ready"}, status_code=404)
    return {
        "session_id": session.id,
        "safe": session.audit["safe"],
        "draft": session.audit["draft"],
        "removed": session.audit["removed"],
        "claims": session.audit["claims"],
        "facts": session.ledger.all(),
        "published": session.published,
    }


@app.post("/webhook/twilio")
async def twilio_webhook(request: Request):
    form = dict((await request.form()).items())
    incoming = transport.from_twilio(form)
    session = store.get(incoming["user_id"])

    text = incoming["text"]
    if incoming.get("media_url"):
        try:
            auth = None
            if settings.twilio_enabled:
                auth = (settings.twilio_account_sid, settings.twilio_auth_token)
            async with httpx.AsyncClient() as client:
                media = await client.get(incoming["media_url"], auth=auth, follow_redirects=True)
                text = openai_client.transcribe(media.content, "audio.ogg")
        except Exception:
            text = text or ""

    reply = agent.handle_message(session, text)
    xml = transport.twiml(reply["messages"])
    return Response(content=xml, media_type="application/xml")
