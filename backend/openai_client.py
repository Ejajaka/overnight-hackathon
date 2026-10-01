import json
import re
from typing import Any, Optional

from . import prompts
from .config import settings

try:
    from openai import OpenAI
except Exception:
    OpenAI = None


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

_CARE_WORDS = {"wash", "washing", "washable", "dry", "clean", "cleaning", "iron", "bleach", "detergent", "dryclean"}
_CULTURAL_WORDS = {"symbol", "symbolize", "symbolizes", "symbolise", "symbolises", "meaning", "means", "heritage", "represents", "signifies", "auspicious", "prosperity", "fortune", "luck", "sacred", "ritual", "blessing"}
_MATERIAL_WORDS = {"cotton", "silk", "wool", "linen", "jute", "dye", "indigo", "thread", "fibre", "fiber", "handspun", "clay", "brass", "wood"}
_PROCESS_WORDS = {"made", "handmade", "woven", "weave", "loom", "handloom", "takes", "days", "weeks", "hand", "crafted", "produced", "dyed", "spin"}
_VARIATION_WORDS = {"varies", "variation", "vary", "unique", "slight", "shade", "texture", "no two", "each piece"}
_PHOTO_WORDS = {"photo", "photograph", "picture", "exact", "one of a kind", "one-of-a-kind", "pictured"}


def _client():
    if not settings.llm_enabled or OpenAI is None:
        return None
    return OpenAI(api_key=settings.openai_api_key)


def _extract_json(text: str) -> dict:
    text = text.strip()
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


def chat_json(system: str, user: str) -> dict:
    client = _client()
    if client is None:
        raise RuntimeError("llm disabled")
    response = client.chat.completions.create(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.2,
        response_format={"type": "json_object"},
    )
    return _extract_json(response.choices[0].message.content or "{}")


def transcribe(data: bytes, filename: str = "audio.webm") -> str:
    client = _client()
    if client is None:
        return ""
    try:
        result = client.audio.transcriptions.create(
            model=settings.openai_transcribe_model,
            file=(filename, data),
        )
        return (result.text or "").strip()
    except Exception:
        return ""


def extract_facts(question_type: str, question: str, answer: str) -> list[dict]:
    client = _client()
    if client is None:
        return _mock_extract(question_type, answer)
    try:
        payload = chat_json(
            prompts.EXTRACT_SYSTEM,
            prompts.extract_user(question_type, question, answer),
        )
        facts = payload.get("facts") or []
        cleaned = []
        for fact in facts:
            text = str(fact.get("text", "")).strip()
            ftype = str(fact.get("type", question_type)).strip() or question_type
            if text:
                cleaned.append({"type": ftype, "text": text})
        return cleaned or _mock_extract(question_type, answer)
    except Exception:
        return _mock_extract(question_type, answer)


def draft_listing(facts: list[dict]) -> dict:
    client = _client()
    facts_block = _facts_block(facts)
    if client is None:
        return _mock_draft(facts)
    try:
        payload = chat_json(prompts.DRAFT_SYSTEM, prompts.draft_user(facts_block))
        return {field: str(payload.get(field, "")).strip() for field in LISTING_FIELDS}
    except Exception:
        return _mock_draft(facts)


def audit_listing(draft: dict, facts: list[dict]) -> list[dict]:
    client = _client()
    draft_block = _draft_block(draft)
    facts_block = _facts_block(facts)
    if client is None:
        return _mock_audit(draft, facts)
    try:
        payload = chat_json(
            prompts.GUARD_SYSTEM,
            prompts.guard_user(draft_block, facts_block),
        )
        claims = payload.get("claims") or []
        cleaned = []
        for claim in claims:
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


NON_ANSWER_PHRASES = (
    "don't know",
    "do not know",
    "dont know",
    "not sure",
    "no idea",
    "skip",
    "n/a",
    "not applicable",
    "none",
    "nothing to add",
)


def is_non_answer(text: str) -> bool:
    norm = (text or "").strip().lower()
    return any(phrase in norm for phrase in NON_ANSWER_PHRASES)


def _facts_block(facts: list[dict]) -> str:
    return "\n".join(f"- [{f['type']}] {f['text']}" for f in facts) or "(none confirmed)"


def _draft_block(draft: dict) -> str:
    lines = []
    for field in LISTING_FIELDS:
        value = draft.get(field, "")
        if value:
            lines.append(f"{field}: {value}")
    return "\n".join(lines)


def _tokens(text: str) -> set:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", text.strip())
    return [p.strip(" -•\t") for p in parts if p and p.strip(" -•\t")]


def _classify(sentence: str) -> str:
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


def _mock_extract(question_type: str, answer: str) -> list[dict]:
    answer = answer.strip()
    if not answer:
        return []
    return [{"type": question_type, "text": answer}]


def _mock_draft(facts: list[dict]) -> dict:
    by_type: dict[str, list[str]] = {}
    for fact in facts:
        by_type.setdefault(fact["type"], []).append(fact["text"])

    def first(ftype: str, default: str = "") -> str:
        values = by_type.get(ftype)
        return values[0] if values else default

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
    cultural_note = (
        cultural_base.rstrip(".") + ". This traditional motif symbolises prosperity and good fortune."
    ).strip()
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


def _mock_audit(draft: dict, facts: list[dict]) -> list[dict]:
    facts_by_type: dict[str, list[str]] = {}
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


def _has_support(ctype: str, sentence: str, facts_by_type: dict[str, list[str]]) -> bool:
    if ctype == "neutral":
        return True
    candidates = facts_by_type.get(ctype, [])
    if not candidates:
        if ctype in ("general",):
            all_facts = [t for values in facts_by_type.values() for t in values]
            candidates = all_facts
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
