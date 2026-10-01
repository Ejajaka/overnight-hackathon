#!/usr/bin/env python3
"""Source-Truth Listings - single-file app.

Run it:
    python app.py

It installs missing dependencies, starts the server, and opens the browser.
Set OPENAI_API_KEY in a .env file (or environment) to use real AI; otherwise it
runs fully offline with a deterministic mock.
"""

import importlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from urllib.parse import quote
from xml.sax.saxutils import escape

DEPENDENCIES = [
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn[standard]"),
    ("openai", "openai"),
    ("dotenv", "python-dotenv"),
    ("pydantic", "pydantic"),
    ("multipart", "python-multipart"),
    ("httpx", "httpx"),
]


def _ensure_dependencies():
    missing = []
    for module, package in DEPENDENCIES:
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(package)
    if missing:
        print(f"Installing missing packages: {', '.join(missing)}")
        subprocess.check_call([sys.executable, "-m", "pip", "install", *missing])


_ensure_dependencies()

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

import httpx  # noqa: E402
from fastapi import FastAPI, File, Form, Request, UploadFile  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, Response  # noqa: E402

try:
    from openai import OpenAI
except Exception:
    OpenAI = None


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
def _bool(value, default=False):
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_TRANSCRIBE_MODEL = os.getenv("OPENAI_TRANSCRIBE_MODEL", "whisper-1")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
USE_MOCK_LLM = _bool(os.getenv("USE_MOCK_LLM"), False)
LLM_ENABLED = bool(OPENAI_API_KEY) and not USE_MOCK_LLM


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------
EXTRACT_SYSTEM = """You convert an artisan's spoken answer into confirmed facts for a product listing.
Return JSON only, shaped as {"facts": [{"type": "...", "text": "..."}]}.
Allowed types: identity, material, care, process, variation, photo, cultural, provenance, general.
Only extract what the artisan actually said. Never infer, embellish, or add typical values.
If the answer contains nothing factual, return {"facts": []}."""

DRAFT_SYSTEM = """You are an expert marketplace copywriter for handmade crafts.
Write a complete, warm, trust-building product listing that helps the piece sell and
reduces buyer questions. Return JSON only with these string fields:
title, story, materials, care, production, variations, cultural_note, photo_note, buyer_faq.
- story: 2-3 sentences about the maker and the craft.
- materials: what it is made from.
- care: how to care for it.
- production: how it is made and why handmade takes longer.
- variations: what naturally differs between pieces.
- cultural_note: the meaning or significance of the pattern, if any.
- photo_note: whether the photo shows the exact piece and whether it is one of a kind.
- buyer_faq: 3-5 short Q&A lines answering common buyer questions.
Write naturally and completely."""

GUARD_SYSTEM = """You are a strict claim auditor for handmade crafts.
You receive a draft listing and a ledger of facts the maker explicitly confirmed.
Break every text field into atomic claims and audit each one. Return JSON only:
{"claims": [{"field": "...", "claim": "...", "type": "...", "status": "...", "evidence": "...", "replacement": "..."}]}

Rules:
- type is one of: material, care, cultural, process, variation, photo, general, neutral.
- status is one of: supported, unsupported, cultural_unverified.
- "supported" ONLY when the ledger explicitly states it. Put the matching ledger fact in evidence.
- Cultural claims about meaning/symbolism are "cultural_unverified" unless the ledger has an
  explicitly confirmed cultural fact. Never allow an invented heritage claim.
- Care claims are "unsupported" unless the ledger explicitly confirms care.
- "neutral" is for non-factual pleasantries (e.g. "Thank you for supporting handmade").
- Be conservative. When unsure, choose unsupported.
- For unsupported/cultural_unverified claims, put a safe replacement in replacement, or "" to drop it.
Split into the smallest meaningful claims. Use the exact field names from the draft."""

LISTING_FIELDS = [
    "title",
    "story",
    "materials",
    "care",
    "production",
    "variations",
    "cultural_note",
    "photo_note",
    "buyer_faq",
]


# --------------------------------------------------------------------------
# LLM client (with offline mock)
# --------------------------------------------------------------------------
_CARE_WORDS = {"wash", "washing", "washable", "dry", "clean", "cleaning", "iron", "bleach", "detergent", "dryclean"}
_CULTURAL_WORDS = {"symbol", "symbolize", "symbolizes", "symbolise", "symbolises", "meaning", "means", "heritage", "represents", "signifies", "auspicious", "prosperity", "fortune", "luck", "sacred", "ritual", "blessing"}
_MATERIAL_WORDS = {"cotton", "silk", "wool", "linen", "jute", "dye", "indigo", "thread", "fibre", "fiber", "handspun", "clay", "brass", "wood"}
_PROCESS_WORDS = {"made", "handmade", "woven", "weave", "loom", "handloom", "takes", "days", "weeks", "hand", "crafted", "produced", "dyed", "spin"}
_VARIATION_WORDS = {"varies", "variation", "vary", "unique", "slight", "shade", "texture", "no two", "each piece"}
_PHOTO_WORDS = {"photo", "photograph", "picture", "exact", "one of a kind", "one-of-a-kind", "pictured"}
NON_ANSWER_PHRASES = ("don't know", "do not know", "dont know", "not sure", "no idea", "skip", "n/a", "not applicable", "none", "nothing to add")


def _client():
    if not LLM_ENABLED or OpenAI is None:
        return None
    return OpenAI(api_key=OPENAI_API_KEY)


def _extract_json(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


def _chat_json(system, user):
    client = _client()
    if client is None:
        raise RuntimeError("llm disabled")
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.2,
        response_format={"type": "json_object"},
    )
    return _extract_json(response.choices[0].message.content or "{}")


def _facts_block(facts):
    return "\n".join(f"- [{f['type']}] {f['text']}" for f in facts) or "(none confirmed)"


def _draft_block(draft):
    return "\n".join(f"{f}: {draft.get(f, '')}" for f in LISTING_FIELDS if draft.get(f))


def _tokens(text):
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _split_sentences(text):
    parts = re.split(r"(?<=[.!?])\s+|\n+", (text or "").strip())
    return [p.strip(" -\t") for p in parts if p and p.strip(" -\t")]


def _classify(sentence):
    stripped = sentence.strip()
    if stripped.upper().startswith("Q:") or stripped.endswith("?"):
        return "neutral"
    if stripped.upper().startswith("A:"):
        stripped = stripped[2:].strip()
    tokens = _tokens(stripped)
    if tokens & _CULTURAL_WORDS:
        return "cultural"
    if tokens & _CARE_WORDS:
        return "care"
    if tokens & _PHOTO_WORDS:
        return "photo"
    if tokens & _VARIATION_WORDS:
        return "variation"
    if tokens & _MATERIAL_WORDS:
        return "material"
    if tokens & _PROCESS_WORDS:
        return "process"
    return "general"


def is_non_answer(text):
    norm = (text or "").strip().lower()
    return any(phrase in norm for phrase in NON_ANSWER_PHRASES)


def transcribe(data, filename="audio.webm"):
    client = _client()
    if client is None:
        return ""
    try:
        result = client.audio.transcriptions.create(model=OPENAI_TRANSCRIBE_MODEL, file=(filename, data))
        return (result.text or "").strip()
    except Exception:
        return ""


def extract_facts(question_type, question, answer):
    client = _client()
    if client is None:
        return _mock_extract(question_type, answer)
    try:
        payload = _chat_json(
            EXTRACT_SYSTEM,
            f"Question topic: {question_type}\nQuestion asked: {question}\nArtisan answer: {answer}\n\n"
            "Extract the minimal set of facts, each as one short sentence in the artisan's own terms.",
        )
        cleaned = []
        for fact in payload.get("facts") or []:
            text = str(fact.get("text", "")).strip()
            ftype = str(fact.get("type", question_type)).strip() or question_type
            if text:
                cleaned.append({"type": ftype, "text": text})
        return cleaned or _mock_extract(question_type, answer)
    except Exception:
        return _mock_extract(question_type, answer)


def draft_listing(facts):
    client = _client()
    if client is None:
        return _mock_draft(facts)
    try:
        payload = _chat_json(
            DRAFT_SYSTEM,
            f"Confirmed maker facts:\n{_facts_block(facts)}\n\nWrite the listing now.",
        )
        return {field: str(payload.get(field, "")).strip() for field in LISTING_FIELDS}
    except Exception:
        return _mock_draft(facts)


def audit_listing(draft, facts):
    client = _client()
    if client is None:
        return _mock_audit(draft, facts)
    try:
        payload = _chat_json(
            GUARD_SYSTEM,
            f"Confirmed maker facts (the only source of truth):\n{_facts_block(facts)}\n\n"
            f"Draft listing to audit:\n{_draft_block(draft)}\n\nReturn the audited claims.",
        )
        cleaned = []
        for claim in payload.get("claims") or []:
            cleaned.append(
                {
                    "field": str(claim.get("field", "")).strip(),
                    "claim": str(claim.get("claim", "")).strip(),
                    "type": str(claim.get("type", "general")).strip().lower(),
                    "status": str(claim.get("status", "unsupported")).strip().lower(),
                    "evidence": str(claim.get("evidence", "")).strip(),
                    "replacement": str(claim.get("replacement", "")).strip(),
                }
            )
        return cleaned or _mock_audit(draft, facts)
    except Exception:
        return _mock_audit(draft, facts)


def _mock_extract(question_type, answer):
    answer = (answer or "").strip()
    if not answer:
        return []
    return [{"type": question_type, "text": answer}]


def _mock_draft(facts):
    by_type = {}
    for fact in facts:
        by_type.setdefault(fact["type"], []).append(fact["text"])

    def first(ftype, default=""):
        return by_type.get(ftype, [default])[0]

    title = first("identity") or first("material", "Handmade Piece")
    story = first("provenance") or first("material", "A handmade piece.")
    materials = first("material", "")
    care = first("care", "Machine wash on a gentle cycle and tumble dry low.")
    if first("care"):
        care = care.rstrip(".") + ". It is also machine washable."
    production = first("process", "Each piece is made to order.")
    if first("process"):
        production = production.rstrip(".") + ". Ships within two days."
    variations = first("variation", "")
    cultural_base = first("cultural", "")
    cultural_note = (cultural_base.rstrip(".") + ". This traditional motif symbolises prosperity and good fortune.").strip()
    photo_note = first("photo", "")

    faq_lines = []
    if first("care"):
        faq_lines.append("Q: Is this machine washable? A: " + first("care") + ".")
    if first("photo"):
        faq_lines.append("Q: Is the photo the exact item? A: " + first("photo") + ".")
    if first("process"):
        faq_lines.append("Q: Why does delivery take longer? A: " + first("process") + ".")
    if first("variation"):
        faq_lines.append("Q: Will mine look exactly like the photo? A: " + first("variation") + ".")
    buyer_faq = "\n".join(faq_lines) or "Q: Any questions? A: Please ask the maker."

    return {
        "title": title or "Handmade Piece",
        "story": story,
        "materials": materials,
        "care": care,
        "production": production,
        "variations": variations,
        "cultural_note": cultural_note,
        "photo_note": photo_note,
        "buyer_faq": buyer_faq,
    }


def _has_support(ctype, sentence, facts_by_type):
    if ctype == "neutral":
        return True
    candidates = facts_by_type.get(ctype, [])
    if not candidates:
        if ctype == "general":
            candidates = [t for values in facts_by_type.values() for t in values]
        else:
            return False
    sentence_tokens = _tokens(sentence)
    for candidate in candidates:
        candidate_tokens = _tokens(candidate)
        if not candidate_tokens:
            continue
        overlap = len(sentence_tokens & candidate_tokens) / max(len(candidate_tokens), 1)
        if overlap >= 0.3:
            return True
    return False


def _mock_audit(draft, facts):
    facts_by_type = {}
    for fact in facts:
        facts_by_type.setdefault(fact["type"], []).append(fact["text"])

    claims = []
    for field in LISTING_FIELDS:
        value = draft.get(field, "")
        if not value:
            continue
        for sentence in _split_sentences(value):
            ctype = _classify(sentence)
            supported = _has_support(ctype, sentence, facts_by_type)
            claims.append(
                {
                    "field": field,
                    "claim": sentence,
                    "type": ctype,
                    "status": "supported" if supported else "unsupported",
                    "evidence": facts_by_type.get(ctype, [""])[0] if supported else "",
                    "replacement": "",
                }
            )
    return claims


# --------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------
class Ledger:
    def __init__(self):
        self._facts = []
        self._counter = 0

    def add(self, fact_type, text, source_turn=0):
        text = (text or "").strip()
        if not text:
            return {}
        existing = {(f["type"], f["text"].lower()) for f in self._facts}
        if (fact_type, text.lower()) in existing:
            return {}
        self._counter += 1
        self._facts.append({"id": self._counter, "type": fact_type, "text": text, "source_turn": source_turn, "confirmed": True})
        return self._facts[-1]

    def add_many(self, facts, source_turn=0):
        added = []
        for fact in facts:
            item = self.add(fact.get("type", "general"), fact.get("text", ""), source_turn)
            if item:
                added.append(item)
        return added

    def all(self):
        return list(self._facts)

    def to_facts(self):
        return [{"type": f["type"], "text": f["text"]} for f in self._facts]

    def is_empty(self):
        return not self._facts


# --------------------------------------------------------------------------
# Interview
# --------------------------------------------------------------------------
QUESTIONS = [
    {"key": "identity", "question": "Let's build your listing. What is this piece called, and what is it?", "quick_replies": ["It's a handwoven shawl"]},
    {"key": "material", "question": "What is it made from? Tell me the real materials and dyes.", "quick_replies": ["Handspun cotton with natural indigo dye"]},
    {"key": "care", "question": "How should a buyer care for it? Say exactly what is safe (and unsafe).", "quick_replies": ["Hand wash cold, dry in shade, never machine wash"]},
    {"key": "process", "question": "How is it made, and roughly how long does one piece take?", "quick_replies": ["Handwoven on a pit loom, about two weeks per piece"]},
    {"key": "variation", "question": "What naturally varies from piece to piece?", "quick_replies": ["Dye shade and weave texture vary slightly"]},
    {"key": "photo", "question": "Is the photo the exact piece the buyer receives? Is it one of a kind?", "quick_replies": ["Yes, the photo is the exact piece and it's one of a kind"]},
    {"key": "cultural", "question": "Does the pattern have a cultural meaning? Share only what is truly known.", "quick_replies": ["It is a family motif; I won't describe meaning I can't confirm"]},
]

CONFIRM_WORDS = {"yes", "y", "yeah", "yep", "confirm", "confirmed", "correct", "right", "ok", "okay", "sure"}
DENY_WORDS = {"no", "n", "nope", "wrong", "incorrect", "edit", "change"}
PUBLISH_WORDS = {"publish", "done", "finish", "share", "ready"}
RETRY_PHRASES = {"no", "n", "nope", "wrong", "incorrect", "edit", "change", "no, let me fix it", "let me fix it", "fix it"}
NEW_LISTING_PHRASES = {"new listing", "start over", "restart", "start a new listing", "new piece", "another listing"}


def _normalize(text):
    return (text or "").strip().lower()


def is_confirm(text):
    norm = _normalize(text)
    words = set(norm.replace("'", " ").split())
    if norm in CONFIRM_WORDS:
        return True
    return bool(words & CONFIRM_WORDS) and not bool(words & DENY_WORDS)


def is_retry(text):
    return _normalize(text).rstrip(".! ") in RETRY_PHRASES


def is_publish(text):
    return bool(set(_normalize(text).replace("'", " ").split()) & PUBLISH_WORDS)


def is_new_listing(text):
    norm = _normalize(text).rstrip(".! ")
    if norm in {"new", "start", "again", "restart"}:
        return True
    return any(phrase in norm for phrase in NEW_LISTING_PHRASES)


# --------------------------------------------------------------------------
# Guard
# --------------------------------------------------------------------------
CARE_FALLBACK = "Care for this piece is not yet confirmed - ask the maker."
CULTURAL_FALLBACK = "This pattern holds significance in the maker's community; its detailed meaning is not documented here."
ALLOWED_STATUS = {"supported", "unsupported", "cultural_unverified"}


def _normalize_status(status):
    status = (status or "").strip().lower()
    return status if status in ALLOWED_STATUS else "unsupported"


def _enforce(claims, ledger_facts):
    ledger_types = {f["type"] for f in ledger_facts}
    for claim in claims:
        claim["type"] = (claim.get("type") or "general").strip().lower()
        claim["status"] = _normalize_status(claim.get("status"))
        if claim["type"] == "care":
            if "care" not in ledger_types:
                claim["status"] = "unsupported"
                claim["replacement"] = CARE_FALLBACK
            elif claim["status"] != "supported":
                claim["replacement"] = ""
        elif claim["type"] == "cultural":
            if "cultural" not in ledger_types:
                claim["status"] = "cultural_unverified"
                claim["replacement"] = CULTURAL_FALLBACK
            elif claim["status"] != "supported":
                claim["replacement"] = ""
        if claim["status"] != "supported":
            claim["evidence"] = ""
    return claims


def _reassemble(draft, claims):
    grouped = {}
    for claim in claims:
        field = claim.get("field") or "story"
        if field not in LISTING_FIELDS:
            field = "story"
        text = claim.get("claim", "").strip()
        kept = text if claim["status"] in ("supported", "neutral") else claim.get("replacement", "").strip()
        if not kept:
            continue
        grouped.setdefault(field, [])
        if kept not in grouped[field]:
            grouped[field].append(kept)
    safe = {field: " ".join(grouped.get(field, [])).strip() for field in LISTING_FIELDS}
    safe["title"] = (draft.get("title") or safe.get("title") or "Handmade piece").strip()
    return safe


def guard_audit(draft, ledger_facts):
    claims = _enforce(audit_listing(draft, ledger_facts), ledger_facts)
    safe = _reassemble(draft, claims)
    ledger_types = {f["type"] for f in ledger_facts}
    if "care" not in ledger_types and not safe.get("care"):
        safe["care"] = CARE_FALLBACK
    if "cultural" not in ledger_types and not safe.get("cultural_note"):
        safe["cultural_note"] = CULTURAL_FALLBACK
    removed = [c for c in claims if c["status"] != "supported" and c["type"] != "neutral" and c.get("field") != "title"]
    return {"draft": draft, "safe": safe, "claims": claims, "removed": removed}


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------
class Session:
    def __init__(self, sid):
        self.id = sid
        self.ledger = Ledger()
        self.stage = "idle"
        self.q_index = 0
        self.pending_facts = []
        self.audit = None
        self.published = False
        self.photo = None
        self.created = time.time()


class SessionStore:
    def __init__(self):
        self._sessions = {}

    def get(self, sid=None):
        if sid and sid in self._sessions:
            return self._sessions[sid]
        sid = sid or uuid.uuid4().hex[:10]
        session = Session(sid)
        self._sessions[sid] = session
        return session

    def remove(self, sid):
        self._sessions.pop(sid, None)


store = SessionStore()


# --------------------------------------------------------------------------
# Agent
# --------------------------------------------------------------------------
INTRO = "Hi! I turn your craft knowledge into a buyer-ready listing where nothing is invented. Every claim stays traceable to you."

SAMPLE_FACTS = [
    {"type": "identity", "text": "Handwoven indigo shawl"},
    {"type": "material", "text": "Handspun cotton with natural indigo dye"},
    {"type": "care", "text": "Hand wash cold, dry in shade, never machine wash"},
    {"type": "process", "text": "Handwoven on a pit loom, about two weeks per piece"},
    {"type": "variation", "text": "Dye shade and weave texture vary slightly"},
    {"type": "photo", "text": "The photo shows the exact piece the buyer receives; it is one of a kind"},
    {"type": "cultural", "text": "A family motif; the meaning is not documented here"},
]


def _reply(messages, quick_replies=None, listing=None, stage=None, buyer_path=None):
    if isinstance(messages, str):
        messages = [messages]
    return {"messages": messages, "quick_replies": quick_replies or [], "listing": listing, "stage": stage, "buyer_path": buyer_path}


def _current_question(session):
    return QUESTIONS[session.q_index]


def _ask_question(session):
    question = _current_question(session)
    session.stage = "interview"
    return _reply(question["question"], question["quick_replies"], stage=session.stage)


def _ask_confirm(session):
    lines = "\n".join(f"- {f['text']}" for f in session.pending_facts)
    question = _current_question(session)
    message = f"Got it. I'll record this as *{question['key']}*:\n{lines}\n\nIs that correct?"
    session.stage = "confirm"
    return _reply(message, ["Yes", "No, let me fix it"], stage=session.stage)


def _extract(session, text):
    question = _current_question(session)
    return extract_facts(question["key"], question["question"], text)


def _advance(session):
    session.q_index += 1
    if session.q_index >= len(QUESTIONS):
        return _listing_reply(session, prefix="All facts confirmed.")
    return _ask_question(session)


def _run_audit(session):
    facts = session.ledger.to_facts()
    session.audit = guard_audit(draft_listing(facts), facts)
    return session.audit


def _listing_payload(session):
    audit = session.audit
    return {
        "safe": audit["safe"],
        "removed": audit["removed"],
        "claims": audit["claims"],
        "published": session.published,
        "buyer_path": f"/buyer/{quote(session.id)}",
    }


def _listing_reply(session, prefix=""):
    audit = _run_audit(session)
    safe = audit["safe"]
    removed = audit["removed"]
    lines = [prefix] if prefix else []
    lines.append("Here is your buyer-ready listing, every claim traced to you:\n")
    lines.append(f"*{safe.get('title') or 'Your piece'}*\n")
    labels = [
        ("story", "Story"),
        ("materials", "Materials"),
        ("care", "Care"),
        ("production", "Production time"),
        ("variations", "Natural variations"),
        ("cultural_note", "Cultural note"),
        ("photo_note", "The exact piece"),
    ]
    for field, label in labels:
        if safe.get(field):
            lines.append(f"*{label}:* {safe[field]}")
    if safe.get("buyer_faq"):
        lines.append(f"*Buyer FAQ:* {safe['buyer_faq']}")
    if removed:
        lines.append(f"\nI flagged or removed {len(removed)} claim(s) I could not verify against your facts:")
        for claim in removed:
            lines.append(f"- ({claim['type']}) {claim['claim']}")
    lines.append("\nReply *Publish* to create the buyer link, or send a correction.")
    session.stage = "review"
    return _reply(lines, ["Publish", "Fix something"], listing=_listing_payload(session), stage=session.stage)


def start(session):
    session.stage = "interview"
    session.q_index = 0
    session.audit = None
    session.published = False
    return _reply([INTRO, _current_question(session)["question"]], _current_question(session)["quick_replies"], stage=session.stage)


def handle_message(session, text):
    text = (text or "").strip()
    if not text:
        return _reply("Please send a message or a voice note.", stage=session.stage)

    if session.stage == "idle":
        return start(session)

    if session.stage == "review" and not is_publish(text) and is_new_listing(text):
        session.ledger = Ledger()
        return start(session)

    if session.stage == "confirm":
        if is_confirm(text):
            if session.pending_facts:
                session.ledger.add_many(session.pending_facts, source_turn=session.q_index)
            session.pending_facts = []
            return _advance(session)
        if is_retry(text):
            return _reply("No problem - please send the correct details for this one.", [], stage="confirm")
        if is_non_answer(text):
            session.pending_facts = []
            return _advance(session)
        session.pending_facts = _extract(session, text) or [{"type": _current_question(session)["key"], "text": text}]
        return _ask_confirm(session)

    if session.stage == "interview":
        if is_non_answer(text):
            session.pending_facts = []
            key = _current_question(session)["key"]
            reply = _advance(session)
            reply["messages"].insert(0, f"Understood - I won't make any claim about {key}.")
            return reply
        session.pending_facts = _extract(session, text) or [{"type": _current_question(session)["key"], "text": text}]
        return _ask_confirm(session)

    if session.stage == "review":
        if is_publish(text):
            session.published = True
            return _reply(
                ["Published. Share this buyer page - it answers care, the exact piece, why it takes longer, and natural variations, all from your confirmed facts."],
                ["Start a new listing"],
                listing=_listing_payload(session),
                stage=session.stage,
                buyer_path=f"/buyer/{quote(session.id)}",
            )
        facts = extract_facts("correction", "The maker is correcting the listing.", text) or [{"type": "general", "text": text}]
        session.ledger.add_many(facts)
        return _listing_reply(session, prefix="Updated with your correction.")

    return start(session)


# --------------------------------------------------------------------------
# Transport (Twilio)
# --------------------------------------------------------------------------
def _twiml(messages):
    body = "".join(f"<Message>{escape(m)}</Message>" for m in messages if m)
    return f"<?xml version='1.0' encoding='UTF-8'?><Response>{body}</Response>"


# --------------------------------------------------------------------------
# Web app
# --------------------------------------------------------------------------
app = FastAPI(title="Source-Truth Listings")

SHARED_CSS = """
:root{--g-dark:#075e54;--g:#128c7e;--bin:#fff;--bout:#dcf8c6;--bg:#e5ddd5;--ink:#111b21;--muted:#667781;--danger:#b3261e;--ok:#1f7a3d;--warn:#a15c00}
*{box-sizing:border-box}
body{margin:0;font-family:"Segoe UI",system-ui,-apple-system,sans-serif;background:#0b141a;color:var(--ink);display:flex;justify-content:center;min-height:100vh}
.phone{width:100%;max-width:480px;height:100vh;display:flex;flex-direction:column;background:var(--bg);box-shadow:0 0 40px rgba(0,0,0,.5)}
.wa-header{background:var(--g-dark);color:#fff;display:flex;align-items:center;gap:12px;padding:10px 14px}
.avatar{width:40px;height:40px;border-radius:50%;background:var(--g);display:grid;place-items:center;font-weight:700}
.header-text{flex:1}.header-text .title{font-size:16px;font-weight:600}.header-text .subtitle{font-size:12px;opacity:.8}
.ghost{background:transparent;border:none;color:#fff;font-size:20px;cursor:pointer;opacity:.9}.ghost:hover{opacity:1}
.chat{flex:1;overflow-y:auto;padding:16px 12px 8px;display:flex;flex-direction:column;gap:8px}
.msg{display:flex}.msg.out{justify-content:flex-end}
.bubble{max-width:88%;padding:8px 11px 6px;border-radius:10px;background:var(--bin);box-shadow:0 1px 1px rgba(0,0,0,.12);animation:pop .16s ease-out}
.msg.out .bubble{background:var(--bout)}
@keyframes pop{from{transform:translateY(4px);opacity:0}to{transform:none;opacity:1}}
.bubble .text{white-space:pre-wrap;font-size:14.5px;line-height:1.4}.bubble .meta{font-size:10px;color:var(--muted);text-align:right;margin-top:2px}
.typing .bubble{font-style:italic;color:var(--muted)}
.quick{display:flex;gap:8px;padding:6px 12px;overflow-x:auto;background:var(--bg)}.quick:empty{display:none}
.chip{flex:0 0 auto;border:1px solid var(--g);color:var(--g-dark);background:#f3fff9;border-radius:16px;padding:6px 12px;font-size:13px;cursor:pointer;white-space:nowrap}.chip:hover{background:#e2f7ec}
.composer{display:flex;gap:8px;align-items:center;padding:8px 10px;background:#f0f0f0}
.composer input[type=text]{flex:1;border:none;border-radius:22px;padding:11px 16px;font-size:14.5px;outline:none}
.send,.mic{border:none;width:42px;height:42px;border-radius:50%;cursor:pointer;font-size:17px}
.send{background:var(--g);color:#fff}.mic{background:#e3e3e3;color:#4a4a4a}.mic.recording{background:var(--danger);color:#fff;animation:pulse 1s infinite}
@keyframes pulse{50%{opacity:.6}}
.card{background:#fff;border-radius:12px;overflow:hidden;margin-top:8px;border:1px solid #e2e2e2}
.card h3{margin:0;padding:10px 12px;background:var(--g-dark);color:#fff;font-size:15px}
.card .body{padding:10px 12px}.card .row{margin-bottom:8px}.card .row .label{font-size:11px;text-transform:uppercase;letter-spacing:.4px;color:var(--muted)}.card .row .value{font-size:14px;line-height:1.4}
.card .flagged{background:#fff6f5;border-top:1px solid #f3d4d1;padding:10px 12px}.card .flagged h4{margin:0 0 6px;font-size:12px;color:var(--danger);text-transform:uppercase}
.claim{font-size:13px;padding:4px 0;display:flex;gap:8px;align-items:flex-start}.claim .tag{flex:0 0 auto;font-size:10px;font-weight:700;padding:2px 6px;border-radius:6px;text-transform:uppercase}
.tag.unsupported{background:#fde1df;color:var(--danger)}.tag.cultural_unverified{background:#ffeccc;color:var(--warn)}.tag.supported{background:#dcf5e4;color:var(--ok)}.tag.neutral{background:#eceaea;color:var(--muted)}
.buyer-link{display:inline-block;margin:8px 12px;background:var(--g);color:#fff;text-decoration:none;padding:8px 14px;border-radius:20px;font-size:13px}
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(20px);background:rgba(0,0,0,.82);color:#fff;padding:9px 16px;border-radius:18px;font-size:13px;opacity:0;pointer-events:none;transition:all .25s}.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
.buyer-page{background:#f7f4ef}.buyer-wrap{max-width:640px;margin:0 auto;padding:24px 16px 60px}.buyer-wrap h1{font-size:26px;margin:0 0 4px}.buyer-wrap .tagline{color:var(--muted);margin-bottom:18px}
.buyer-section{background:#fff;border-radius:12px;padding:14px 16px;margin-bottom:12px;box-shadow:0 1px 2px rgba(0,0,0,.06)}.buyer-section h2{font-size:12px;text-transform:uppercase;letter-spacing:.5px;color:var(--g-dark);margin:0 0 6px}.buyer-section p{margin:0;line-height:1.5}
.provenance{font-size:12px;color:var(--muted);margin-top:6px;border-top:1px dashed #e5e5e5;padding-top:6px}
.trust-banner{background:#e8f5ec;border:1px solid #bfe3ca;color:#14532d;border-radius:12px;padding:12px 14px;font-size:13px;margin-bottom:16px}
.removed-note{background:#fff6f5;border:1px solid #f3d4d1;color:#7f1d1d;border-radius:12px;padding:12px 14px;font-size:13px;margin-top:12px}
"""

FRONTEND_JS = r"""
const chat=document.getElementById("chat"),quick=document.getElementById("quick"),input=document.getElementById("input"),sendBtn=document.getElementById("send"),micBtn=document.getElementById("mic"),photoBtn=document.getElementById("photoBtn"),photoFile=document.getElementById("photoFile"),resetBtn=document.getElementById("reset"),sampleBtn=document.getElementById("sample"),statusEl=document.getElementById("status"),toast=document.getElementById("toast");
let sessionId=localStorage.getItem("stl_session")||null,busy=false;
const LABELS=[["story","Story"],["materials","Materials"],["care","Care"],["production","Production time"],["variations","Natural variations"],["cultural_note","Cultural note"],["photo_note","The exact piece"],["buyer_faq","Buyer FAQ"]];
function esc(v){return String(v||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")}
function now(){return new Date().toLocaleTimeString([],{hour:"2-digit",minute:"2-digit"})}
function addBubble(text,out){const w=document.createElement("div");w.className="msg"+(out?" out":"");const b=document.createElement("div");b.className="bubble";const t=document.createElement("div");t.className="text";t.innerHTML=esc(text).replace(/\*(.+?)\*/g,"<strong>$1</strong>");const m=document.createElement("div");m.className="meta";m.textContent=now();b.appendChild(t);b.appendChild(m);w.appendChild(b);chat.appendChild(w);chat.scrollTop=chat.scrollHeight;return w}
function addTyping(){const w=document.createElement("div");w.className="msg typing";w.innerHTML='<div class="bubble"><div class="text">typing...</div></div>';chat.appendChild(w);chat.scrollTop=chat.scrollHeight;return w}
function addListingCard(listing){const card=document.createElement("div");card.className="card";const safe=listing.safe||{};let html=`<h3>${esc(safe.title||"Your listing")}</h3><div class="body">`;for(const[f,l]of LABELS){if(safe[f])html+=`<div class="row"><div class="label">${l}</div><div class="value">${esc(safe[f])}</div></div>`}html+="</div>";if(listing.removed&&listing.removed.length){html+='<div class="flagged"><h4>Blocked by the source-truth guard</h4>';for(const c of listing.removed)html+=`<div class="claim"><span class="tag ${esc(c.status)}">${esc(c.status)}</span><span>${esc(c.claim)}</span></div>`;html+="</div>"}card.innerHTML=html;if(listing.published&&listing.buyer_path){const a=document.createElement("a");a.className="buyer-link";a.href=listing.buyer_path;a.target="_blank";a.rel="noopener";a.textContent="Open buyer page";card.appendChild(a)}chat.appendChild(card);chat.scrollTop=chat.scrollHeight}
function setQuick(r){quick.innerHTML="";(r||[]).forEach(t=>{const c=document.createElement("button");c.className="chip";c.textContent=t;c.onclick=()=>send(t);quick.appendChild(c)})}
function showToast(m){toast.textContent=m;toast.classList.add("show");setTimeout(()=>toast.classList.remove("show"),2200)}
function handleResponse(resp){if(resp.session_id){sessionId=resp.session_id;localStorage.setItem("stl_session",sessionId)}if(resp.buyer_url&&resp.listing&&resp.listing.published)resp.listing.buyer_path=resp.buyer_url;(resp.messages||[]).forEach(t=>addBubble(t,false));if(resp.listing)addListingCard(resp.listing);setQuick(resp.quick_replies);if(resp.mock_llm)statusEl.textContent="maker assistant · offline mock"}
async function send(text){if(busy||!text)return;busy=true;setQuick([]);addBubble(text,true);const typing=addTyping();try{const res=await fetch("/api/chat",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({session_id:sessionId,message:text})});const data=await res.json();typing.remove();handleResponse(data)}catch(e){typing.remove();addBubble("Connection error. Is the server running?",false)}finally{busy=false}}
sendBtn.onclick=()=>{const v=input.value.trim();input.value="";send(v)};
input.addEventListener("keydown",e=>{if(e.key==="Enter"){const v=input.value.trim();input.value="";send(v)}});
resetBtn.onclick=async()=>{await fetch("/api/reset",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({session_id:sessionId})});localStorage.removeItem("stl_session");sessionId=null;chat.innerHTML="";setQuick([]);send("hi")};
sampleBtn.onclick=async()=>{if(busy)return;busy=true;setQuick([]);chat.innerHTML="";addBubble("Show a sample maker",true);const typing=addTyping();try{const res=await fetch("/api/sample",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({session_id:sessionId})});const data=await res.json();typing.remove();handleResponse(data)}catch(e){typing.remove();addBubble("Could not load sample.",false)}finally{busy=false}};
let recorder=null,chunks=[];
micBtn.onclick=async()=>{if(recorder&&recorder.state==="recording"){recorder.stop();return}try{const stream=await navigator.mediaDevices.getUserMedia({audio:true});recorder=new MediaRecorder(stream);chunks=[];recorder.ondataavailable=e=>chunks.push(e.data);recorder.onstop=async()=>{micBtn.classList.remove("recording");stream.getTracks().forEach(t=>t.stop());await sendVoice(new Blob(chunks,{type:"audio/webm"}))};recorder.start();micBtn.classList.add("recording");showToast("Recording... tap again to send")}catch(e){showToast("Microphone not available")}};
async function sendVoice(blob){busy=true;addBubble("[voice note]",true);const typing=addTyping();const form=new FormData();form.append("session_id",sessionId||"");form.append("audio",blob,"voice.webm");try{const res=await fetch("/api/voice",{method:"POST",body:form});const data=await res.json();typing.remove();if(data.transcript)addBubble("Transcript: "+data.transcript,false);handleResponse(data)}catch(e){typing.remove();addBubble("Could not send voice note.",false)}finally{busy=false}}
photoBtn.onclick=()=>photoFile.click();
photoFile.onchange=()=>{const f=photoFile.files[0];if(f)sendPhoto(f);photoFile.value=""};
async function sendPhoto(file){busy=true;const typing=addTyping();const form=new FormData();form.append("session_id",sessionId||"");form.append("image",file);try{const res=await fetch("/api/photo",{method:"POST",body:form});const data=await res.json();typing.remove();handleResponse(data)}catch(e){typing.remove();addBubble("Could not send photo.",false)}finally{busy=false}}
window.addEventListener("DOMContentLoaded",()=>send("hi"));
"""

INDEX_HTML = (
    "<!doctype html><html lang='en'><head><meta charset='utf-8'/>"
    "<meta name='viewport' content='width=device-width, initial-scale=1'/>"
    "<title>Source-Truth Listings</title><style>" + SHARED_CSS + "</style></head><body>"
    "<div class='phone'><header class='wa-header'><div class='avatar'>ST</div>"
    "<div class='header-text'><div class='title'>Source-Truth Listings</div>"
    "<div class='subtitle' id='status'>maker assistant · online</div></div>"
    "<button class='ghost' id='sample' title='Load sample demo'>&#9654;</button>"
    "<button class='ghost' id='reset' title='New listing'>&#8635;</button></header>"
    "<main id='chat' class='chat'></main><div id='quick' class='quick'></div>"
    "<footer class='composer'><button class='mic' id='photoBtn' title='Send photo'>&#128247;</button>"
    "<button class='mic' id='mic' title='Record voice note'>&#127908;</button>"
    "<input id='input' type='text' placeholder='Type a message' autocomplete='off'/>"
    "<button class='send' id='send' title='Send'>&#10148;</button>"
    "<input id='photoFile' type='file' accept='image/*' hidden/></footer></div>"
    "<div id='toast' class='toast'></div>"
    "<script>" + FRONTEND_JS + "</script></body></html>"
)

BUYER_HTML = (
    "<!doctype html><html lang='en'><head><meta charset='utf-8'/>"
    "<meta name='viewport' content='width=device-width, initial-scale=1'/>"
    "<title>Listing · Source-Truth</title><style>" + SHARED_CSS + "</style></head>"
    "<body class='buyer-page'><div class='buyer-wrap'>"
    "<div class='trust-banner'>Every statement below is traced to facts the maker confirmed. Nothing is generated or invented.</div>"
    "<h1 id='title'>Loading...</h1><div class='tagline' id='tagline'>Handmade by the maker</div>"
    "<div id='sections'></div><div id='removed'></div>"
    "<div class='buyer-section' style='margin-top:16px'><h2>Confirmed facts used</h2><div id='facts'></div></div>"
    "</div><script>"
    "const SID=window.location.pathname.split('/').filter(Boolean).pop();"
    "const LABELS={story:'Story',materials:'Materials',care:'Care',production:'Production time',variations:'Natural variations',cultural_note:'Cultural note',photo_note:'The exact piece',buyer_faq:'Buyer questions'};"
    "const FL={identity:'Identity',material:'Material',care:'Care',process:'Process',variation:'Variation',photo:'Photo',cultural:'Cultural',provenance:'Provenance',general:'General'};"
    "function esc(v){return String(v||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}"
    "fetch('/api/listing/'+SID).then(r=>r.json()).then(d=>{if(d.error){document.getElementById('title').textContent='Listing not ready yet';return}"
    "const s=d.safe||{};document.getElementById('title').textContent=s.title||'Handmade piece';document.getElementById('tagline').textContent='Handmade · one of a kind';"
    "if(d.photo_url){const img=document.createElement('img');img.src=d.photo_url;img.alt='Product photo';img.style.cssText='width:100%;border-radius:12px;margin-bottom:16px';document.querySelector('.buyer-wrap').insertBefore(img,document.getElementById('title'))}"
    "const sec=document.getElementById('sections');for(const f of Object.keys(LABELS)){if(!s[f])continue;const div=document.createElement('div');div.className='buyer-section';div.innerHTML='<h2>'+LABELS[f]+'</h2><p>'+esc(s[f])+'</p>';sec.appendChild(div)}"
    "if(d.removed&&d.removed.length){const n=document.createElement('div');n.className='removed-note';n.innerHTML='<strong>Guarded for your trust:</strong> '+d.removed.length+' claim(s) the maker did not confirm were blocked from this listing instead of being invented.';document.getElementById('removed').appendChild(n)}"
    "const facts=document.getElementById('facts');(d.facts||[]).forEach(x=>{const div=document.createElement('div');div.className='provenance';div.textContent=(FL[x.type]||x.type)+': '+x.text;facts.appendChild(div)})"
    "}).catch(()=>{document.getElementById('title').textContent='Could not load listing'});"
    "</script></body></html>"
)


def _absolute(request, path):
    if not path:
        return ""
    if PUBLIC_BASE_URL:
        return f"{PUBLIC_BASE_URL}{path}"
    return str(request.base_url).rstrip("/") + path


def _respond(request, session, reply):
    return {
        "session_id": session.id,
        "messages": reply["messages"],
        "quick_replies": reply["quick_replies"],
        "listing": reply["listing"],
        "stage": reply["stage"],
        "buyer_url": _absolute(request, reply.get("buyer_path") or (f"/buyer/{quote(session.id)}" if reply["listing"] else "")),
        "mock_llm": not LLM_ENABLED,
    }


@app.get("/")
def index():
    return HTMLResponse(INDEX_HTML)


@app.get("/buyer/{sid}")
def buyer(sid: str):
    return HTMLResponse(BUYER_HTML)


@app.get("/api/health")
def health():
    return {"ok": True, "llm_enabled": LLM_ENABLED, "twilio_enabled": bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN)}


@app.post("/api/chat")
async def chat(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "invalid payload"}, status_code=400)
    session = store.get(payload.get("session_id"))
    return _respond(request, session, handle_message(session, payload.get("message", "")))


@app.post("/api/sample")
async def sample(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    session = store.get(payload.get("session_id"))
    session.ledger = Ledger()
    session.ledger.add_many(SAMPLE_FACTS)
    session.q_index = len(QUESTIONS)
    reply = _listing_reply(session, prefix="Sample maker: seven confirmed facts loaded.")
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
    text = transcribe(await audio.read(), audio.filename or "audio.webm")
    if not text:
        return {
            "session_id": session.id,
            "messages": ["I could not read that voice note. Please type your answer, or set OPENAI_API_KEY to enable transcription."],
            "quick_replies": [],
            "listing": None,
            "stage": session.stage,
            "buyer_url": "",
            "mock_llm": not LLM_ENABLED,
            "transcript": "",
        }
    result = _respond(request, session, handle_message(session, text))
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
        "photo_url": _media_url(session),
    }


def _media_url(session):
    return f"/media/{quote(session.id)}" if session.photo else None


@app.get("/media/{sid}")
def media(sid: str):
    session = store.get(sid)
    if not session.photo:
        return Response(status_code=404)
    return Response(content=session.photo["bytes"], media_type=session.photo["content_type"])


async def _download_media(url):
    auth = (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN) else None
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(url, auth=auth, follow_redirects=True)
        return response.content


@app.post("/api/photo")
async def photo(request: Request, session_id: str = Form(""), image: UploadFile = File(...)):
    session = store.get(session_id or None)
    data = await image.read()
    content_type = image.content_type or "image/jpeg"
    session.photo = {"bytes": data, "content_type": content_type}
    message = "Got your photo - I'll show this exact photo on the buyer page."
    if session.stage == "interview" and _current_question(session)["key"] == "photo":
        session.pending_facts = [
            {"type": "photo", "text": "The attached photo shows the exact piece the buyer will receive."}
        ]
        message += " Is that correct?"
        session.stage = "confirm"
    return {
        "session_id": session.id,
        "messages": [message],
        "quick_replies": ["Yes", "No, let me fix it"] if session.stage == "confirm" else [],
        "listing": _listing_payload(session) if session.audit else None,
        "stage": session.stage,
        "buyer_url": "",
        "mock_llm": not LLM_ENABLED,
    }


@app.post("/webhook/twilio")
async def twilio_webhook(request: Request):
    form = dict((await request.form()).items())
    media_count = int(form.get("NumMedia", "0") or "0")
    user_id = (form.get("From", "") or "").strip().lower()
    session = store.get(user_id)
    text = (form.get("Body", "") or "").strip()

    for index in range(media_count):
        media_url = form.get(f"MediaUrl{index}")
        content_type = form.get(f"MediaContentType{index}", "") or ""
        if not media_url:
            continue
        try:
            data = await _download_media(media_url)
        except Exception:
            continue
        if content_type.startswith("image"):
            session.photo = {"bytes": data, "content_type": content_type}
        elif content_type.startswith("audio") or content_type in ("video/ogg", "application/ogg"):
            text = transcribe(data, "audio.ogg") or text

    reply = handle_message(session, text)

    messages = list(reply["messages"])
    if reply.get("buyer_path") and reply["listing"] and reply["listing"].get("published"):
        url = (PUBLIC_BASE_URL or "") + reply["buyer_path"]
        if url:
            messages.append(f"Buyer page: {url}")
    return Response(content=_twiml(messages), media_type="application/xml")


_tunnel_proc = None


def _find_cloudflared():
    from shutil import which

    found = which("cloudflared")
    if found:
        return found
    candidates = [
        r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
        r"C:\Program Files\cloudflared\cloudflared.exe",
        os.path.expanduser(r"~\cloudflared\cloudflared.exe"),
        "/usr/local/bin/cloudflared",
        "/usr/bin/cloudflared",
        "/opt/homebrew/bin/cloudflared",
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return None


def _start_cloudflared(port):
    exe = _find_cloudflared()
    if not exe:
        return None
    proc = subprocess.Popen(
        [exe, "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    deadline = time.time() + 40
    for line in proc.stdout:
        match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
        if match:
            return match.group(0), proc
        if time.time() > deadline:
            break
    proc.terminate()
    return None


def _start_pyngrok(port):
    try:
        importlib.import_module("pyngrok")
    except ImportError:
        print("Installing pyngrok for the public tunnel...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyngrok"])
    from pyngrok import conf, ngrok

    token = os.getenv("NGROK_AUTHTOKEN", "").strip()
    if token:
        conf.get_default().auth_token = token
    tunnel = ngrok.connect(port, "http")
    public_url = tunnel.public_url
    if public_url.startswith("http://"):
        public_url = "https://" + public_url[len("http://"):]
    return public_url.rstrip("/")


def start_tunnel(port):
    global PUBLIC_BASE_URL, _tunnel_proc
    result = _start_cloudflared(port)
    if result:
        url, proc = result
        _tunnel_proc = proc
    else:
        url = _start_pyngrok(port)
    PUBLIC_BASE_URL = url
    return PUBLIC_BASE_URL


def main():
    import uvicorn

    live = "--live" in sys.argv or os.getenv("LIVE", "").strip().lower() in {"1", "true", "yes"}
    host = "0.0.0.0" if live else os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    local_url = f"http://127.0.0.1:{port}"

    print("=" * 66)
    print("  Source-Truth Listings")
    print(f"  Open:  {local_url}")
    print(f"  Mode:  {'OpenAI (' + OPENAI_MODEL + ')' if LLM_ENABLED else 'offline mock (set OPENAI_API_KEY for real AI)'}")

    public_url = PUBLIC_BASE_URL
    if live:
        try:
            public_url = start_tunnel(port)
            print(f"  Public: {public_url}")
            print("")
            print("  WhatsApp (Twilio sandbox):")
            print("   1. Twilio Console > Messaging > Try it out > Send a WhatsApp message")
            print("   2. Join the sandbox from your phone with the code Twilio shows")
            print(f"   3. Set the inbound webhook (POST) to: {public_url}/webhook/twilio")
            print("   4. Message the sandbox - photos and voice notes work")
        except Exception as exc:
            print(f"  Tunnel failed: {exc}")
            print("  Fix it one of these ways, then re-run:")
            print("    a) Set NGROK_AUTHTOKEN in .env  (free: https://dashboard.ngrok.com/get-started/your-authtoken)")
            print("    b) Install cloudflared (winget install --id Cloudflare.cloudflared) - no account needed")
            print(f"    c) Run your own tunnel, then set PUBLIC_BASE_URL=https://... and restart")
            print("  The app is still running locally; WhatsApp just needs the public URL.")
    print("=" * 66)

    if os.getenv("NO_BROWSER", "").strip().lower() not in {"1", "true", "yes"}:
        threading.Timer(1.8, lambda: webbrowser.open(local_url)).start()
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
