"""End-to-end smoke test using FastAPI's TestClient. Run: python -m scripts.smoke_test"""

import sys

from fastapi.testclient import TestClient

from backend.main import app

client = TestClient(app)
FAILURES = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" - {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def chat(message, sid=None):
    body = {"message": message}
    if sid:
        body["session_id"] = sid
    return client.post("/api/chat", json=body).json()


ANSWERS = [
    "Handwoven wool shawl",
    "Pure wool with natural dyes",
    "One week per piece",
    "Ships in about two weeks",
    "Hand wash only, dry in shade",
    "Woven on a handloom",
    "Colour and texture vary slightly",
    "Yes, this is the exact piece, one of a kind",
    "A family pattern whose meaning I cannot confirm",
    "1200",
]


def run_interview(sid):
    reply = {}
    for answer in ANSWERS:
        reply = chat(answer, sid)
        reply = chat("Yes", sid)
    return reply


def main():
    health = client.get("/api/health").json()
    check("health endpoint", health.get("ok") is True)

    first = chat("hi")
    sid = first["session_id"]
    check("session created", bool(sid))
    check("interview starts", first["stage"] == "interview")

    reply = run_interview(sid)
    check("reaches review stage", reply["stage"] == "review", reply.get("stage"))
    removed = (reply.get("listing") or {}).get("removed", [])
    removed_types = sorted(c["type"] for c in removed)
    check("guard blocks care claim", "care" in removed_types, str(removed_types))
    check("guard blocks cultural claim", "cultural" in removed_types, str(removed_types))
    check("guard blocks timeline claim", "process" in removed_types, str(removed_types))

    safe = (reply.get("listing") or {}).get("safe", {})
    check("confirmed care kept", "Hand wash only" in safe.get("care", ""), safe.get("care"))
    check("invented washable blocked", "machine washable" not in safe.get("care", "").lower())
    check(
        "invented meaning blocked",
        "prosperity" not in safe.get("cultural_note", "").lower(),
        safe.get("cultural_note"),
    )

    published = chat("publish", sid)
    check("publish works", (published.get("listing") or {}).get("published") is True)
    check("buyer url present", bool(published.get("buyer_url")))

    listing = client.get(f"/api/listing/{sid}").json()
    check("listing endpoint", len(listing.get("facts", [])) == len(ANSWERS))
    check("listing has provenance claims", len(listing.get("claims", [])) > 0)

    buyer = client.get(f"/buyer/{sid}")
    check("buyer page served", buyer.status_code == 200)

    restarted = chat("Start a new listing", sid)
    check("restart begins fresh interview", restarted["stage"] == "interview")
    check("restart clears listing", restarted.get("listing") is None)

    r2 = chat("a cotton scarf", sid)
    check("new answer asked to confirm", r2["stage"] == "confirm")
    retry = chat("No, let me fix it", sid)
    check("retry keeps confirm stage", retry["stage"] == "confirm")
    check("retry does not record junk", "let me fix it" not in retry["messages"][0])

    voice = client.post(
        "/api/voice",
        data={"session_id": sid},
        files={"audio": ("voice.webm", b"fake-audio", "audio/webm")},
    ).json()
    check("voice endpoint responds", "messages" in voice)

    non = chat("hi")
    sid2 = non["session_id"]
    answers2 = list(ANSWERS)
    answers2[4] = "I don't know"
    answers2[8] = "no idea"
    for answer in answers2:
        chat(answer, sid2)
        chat("Yes", sid2)
    listing2 = client.get(f"/api/listing/{sid2}").json()
    check(
        "care fallback when unconfirmed",
        "not yet confirmed" in listing2["safe"]["care"].lower(),
        listing2["safe"]["care"],
    )
    check(
        "cultural fallback when unconfirmed",
        "not documented" in listing2["safe"]["cultural_note"].lower(),
        listing2["safe"]["cultural_note"],
    )
    check(
        "non-answers not stored as facts",
        all("i don't know" not in f["text"].lower() and "no idea" not in f["text"].lower() for f in listing2["facts"]),
    )

    reset = client.post("/api/reset", json={"session_id": sid}).json()
    check("reset works", reset.get("reset") is True)

    bad = client.post(
        "/api/chat", content=b"not json", headers={"Content-Type": "application/json"}
    )
    check("invalid json rejected", bad.status_code == 400)

    twilio = client.post(
        "/webhook/twilio",
        data={"From": "whatsapp:+910000000000", "Body": "hi", "NumMedia": "0"},
    )
    check("twilio webhook status", twilio.status_code == 200)
    check("twilio returns twiml", "Response" in twilio.text and "Message" in twilio.text)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
