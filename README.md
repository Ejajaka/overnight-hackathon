# Source-Truth Listings

Turn an artisan's spoken knowledge into a buyer-ready listing where **every claim is
traceable to the maker and nothing is invented**. A WhatsApp-style chat interviews the
maker, an LLM drafts the listing, and a **source-truth guard** blocks any care, process,
or cultural claim the maker never confirmed.

## The problem

Artisans hold real knowledge about materials, care, production time, and pattern meaning,
but it rarely reaches listings. Existing AI writers produce polished copy and freely invent
care instructions or cultural meaning - which for traditional crafts can misrepresent a
community's heritage. Buyers then ask whether the photo is the exact item, whether it is
machine washable, why it takes longer, and what will naturally vary.

**How might we convert an artisan's spoken knowledge into a trustworthy, buyer-ready
listing in minutes, where every claim is traceable to the maker and nothing is invented?**

## How it works

1. **Interview** - the bot asks a few quick-reply questions (identity, material, care,
   process, natural variation, photo, cultural meaning).
2. **Confirm** - each answer is extracted into facts and echoed back for a one-tap confirm.
   Only confirmed facts enter the **Claim Ledger**.
3. **Draft** - an LLM writes a complete, appealing listing (it is allowed to be persuasive,
   which is exactly when hallucinations appear).
4. **Guard** - the draft is split into atomic claims and audited against the ledger:
   - `supported` - explicitly backed by a confirmed fact (kept, with evidence)
   - `unsupported` - invented care/process claim (stripped)
   - `cultural_unverified` - invented heritage meaning (blocked, never described)
5. **Publish** - the maker shares a buyer page that answers the four buyer questions using
   only confirmed facts.

```
Maker (WhatsApp)  ->  FastAPI /webhook or simulator  ->  agent.py
                                                          |  interview.py -> ledger.py
                                                          |  draft.py (LLM)
                                                          |  guard.py  (audit + repair)
                                                          v
                                              buyer page  /buyer/{id}
```

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m uvicorn backend.main:app --reload --port 8000
```

(Installing into a fresh virtual environment avoids conflicts with unrelated global
packages such as streamlit.)

Open http://127.0.0.1:8000 - the simulated WhatsApp UI opens and starts the interview.

Runs fully offline with a deterministic mock if no API key is set. To use real LLMs:

```powershell
Copy-Item .env.example .env
# set OPENAI_API_KEY in .env, then restart
```

The mock deliberately adds plausible-but-invented claims so you can watch the guard block
them without any API key.

### Command-line demo

```powershell
python -m scripts.demo
```

Prints a full interview, the guarded listing, and the blocked claims.

### Smoke test

```powershell
python -m scripts.smoke_test
```

Exercises the whole flow (interview, guard, publish, buyer page, restart, retry,
non-answer handling, voice, reset, invalid input, Twilio webhook) and exits non-zero on
any failure.

## Real WhatsApp (Twilio sandbox)

1. In Twilio Console, open **Messaging > Try it out > Send a WhatsApp message** and join
   the sandbox from your phone.
2. Expose your server publicly (e.g. `ngrok http 8000`) and set `PUBLIC_BASE_URL` to the
   public URL in `.env`, plus `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN`.
3. Set the sandbox **"When a message comes in"** webhook to
   `https://<your-url>/webhook/twilio` (HTTP POST).
4. Message the sandbox: the same flow runs over real WhatsApp, including voice notes
   (transcribed with Whisper).

The Twilio path and the simulator share the same `agent.handle_message` core through the
`backend/transport.py` adapter.

## Project layout

```
backend/
  main.py           FastAPI app, chat/voice/listing endpoints, Twilio webhook
  agent.py          interview -> draft -> guard -> reply orchestration
  interview.py      guided questions and reply keywords
  ledger.py         the confirmed-facts Claim Ledger
  draft.py          listing generation
  guard.py          claim audit + deterministic repair  <-- core contribution
  openai_client.py  OpenAI wrapper + offline mock
  prompts.py        extraction, drafting, and guard prompts
  transport.py      Twilio normalization + TwiML
  sessions.py       in-memory session store
frontend/
  index.html        WhatsApp-style simulator
  app.js            chat logic, quick replies, voice recording, listing card
  buyer.html        buyer page with provenance
  styles.css
scripts/demo.py     end-to-end command-line walkthrough
```

## Guard design notes

- **Default-deny.** The guard is conservative: unsupported claims are removed, not softened.
- **Deterministic safety net.** Even if the auditor marks a care or cultural claim as
  supported, `guard.enforce` overrides it unless the ledger actually contains a confirmed
  fact of that type.
- **Closed fallbacks.** Unknown care becomes *"Care for this piece is not yet confirmed -
  ask the maker."* Unverified pattern meaning becomes *"This pattern holds significance in
  the maker's community; its detailed meaning is not documented here."*
- **Evidence.** Supported claims carry the matching ledger fact for the provenance view.

## Limitations

- Sessions are in-memory (fine for a demo); restart clears them.
- The offline mock uses keyword heuristics; with an API key the LLM performs extraction,
  drafting, and auditing.
- Token-overlap support matching in the mock is approximate; the LLM auditor is the
  intended production path.
