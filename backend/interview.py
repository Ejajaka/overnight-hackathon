QUESTIONS = [
    {
        "key": "identity",
        "question": "Let's build your listing. What is this piece called, and what is it?",
        "quick_replies": ["It's a handwoven shawl"],
    },
    {
        "key": "material",
        "question": "What is it made from? Tell me the real materials and dyes.",
        "quick_replies": ["Handspun cotton with natural indigo dye"],
    },
    {
        "key": "care",
        "question": "How should a buyer care for it? Say exactly what is safe (and unsafe).",
        "quick_replies": ["Hand wash cold, dry in shade, never machine wash"],
    },
    {
        "key": "process",
        "question": "How is it made, and roughly how long does one piece take?",
        "quick_replies": ["Handwoven on a pit loom, about two weeks per piece"],
    },
    {
        "key": "variation",
        "question": "What naturally varies from piece to piece?",
        "quick_replies": ["Dye shade and weave texture vary slightly"],
    },
    {
        "key": "photo",
        "question": "Is the photo the exact piece the buyer receives? Is it one of a kind?",
        "quick_replies": ["Yes, the photo is the exact piece and it's one of a kind"],
    },
    {
        "key": "cultural",
        "question": "Does the pattern have a cultural meaning? Share only what is truly known.",
        "quick_replies": ["It is a family motif; I won't describe meaning I can't confirm"],
    },
]

START_WORDS = {"new", "listing", "start", "begin", "hi", "hello", "hey", "product", "create"}
CONFIRM_WORDS = {"yes", "y", "yeah", "yep", "confirm", "confirmed", "correct", "right", "ok", "okay", "sure"}
DENY_WORDS = {"no", "n", "nope", "wrong", "incorrect", "edit", "change"}
PUBLISH_WORDS = {"publish", "done", "finish", "share", "ready"}
RETRY_PHRASES = {
    "no",
    "n",
    "nope",
    "wrong",
    "incorrect",
    "edit",
    "change",
    "no, let me fix it",
    "let me fix it",
    "fix it",
}
NEW_LISTING_PHRASES = {"new listing", "start over", "restart", "start a new listing", "new piece", "another listing"}


def first_question_index() -> int:
    return 0


def normalize(text: str) -> str:
    return (text or "").strip().lower()


def is_start(text: str) -> bool:
    words = set(normalize(text).replace("'", " ").split())
    return bool(words & START_WORDS)


def is_confirm(text: str) -> bool:
    norm = normalize(text)
    if norm in CONFIRM_WORDS:
        return True
    words = set(norm.replace("'", " ").split())
    return bool(words & CONFIRM_WORDS) and not bool(words & DENY_WORDS)


def is_deny(text: str) -> bool:
    norm = normalize(text)
    if norm in DENY_WORDS:
        return True
    words = set(norm.replace("'", " ").split())
    return bool(words & DENY_WORDS)


def is_publish(text: str) -> bool:
    words = set(normalize(text).replace("'", " ").split())
    return bool(words & PUBLISH_WORDS)


def is_retry(text: str) -> bool:
    return normalize(text).rstrip(".! ") in RETRY_PHRASES


def is_new_listing(text: str) -> bool:
    norm = normalize(text).rstrip(".! ")
    if norm in {"new", "start", "again", "restart"}:
        return True
    return any(phrase in norm for phrase in NEW_LISTING_PHRASES)
