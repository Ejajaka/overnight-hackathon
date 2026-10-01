from xml.sax.saxutils import escape

from .config import settings


def normalize_number(raw: str) -> str:
    return (raw or "").strip().lower()


def from_twilio(form: dict) -> dict:
    media_count = int(form.get("NumMedia", "0") or "0")
    return {
        "channel": "whatsapp",
        "user_id": normalize_number(form.get("From", "")),
        "text": (form.get("Body", "") or "").strip(),
        "media_url": form.get("MediaUrl0") if media_count else None,
        "media_type": form.get("MediaContentType0") if media_count else None,
    }


def twiml(messages: list[str]) -> str:
    body = "".join(f"<Message>{escape(m)}</Message>" for m in messages if m)
    return f"<?xml version='1.0' encoding='UTF-8'?><Response>{body}</Response>"


def absolute_buyer_url(path: str) -> str:
    if not path:
        return ""
    if settings.public_base_url:
        return f"{settings.public_base_url}{path}"
    return path
