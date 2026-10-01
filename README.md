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

1. **Interview** - the bot asks quick-reply questions. Some are **compulsory** and always
   asked (identity, material, making time, delivery time, care, photo/exactness, cultural
   meaning, price); a couple are optional (process, natural variation). Every answer is
   stored as a confirmed fact, so the same facts seed the buyer knowledge base.
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

Then the **buyer loop**:

6. **Browse** - buyers open the storefront `/shop`, see published products, and open one.
7. **Ask** - the buyer types a question. It is looked up in the product's knowledge base
   (confirmed facts + previously answered questions).
8. **Answer or escalate** - if the knowledge base has it, the buyer gets an instant answer.
   If not, the question is escalated to the maker on WhatsApp.
9. **Learn** - the maker replies with text or a voice note; it is processed into a fact,
   stored in the knowledge base, and shown back to the buyer.
10. **Order** - the buyer places an order from the product page. The order is stored with the
    price and buyer contact, and the maker is notified on WhatsApp and in the seller view.

```
Maker (WhatsApp) -> FastAPI /webhook or simulator -> agent.py
                                                      | interview -> ledger (KB)
                                                      | draft (LLM)
                                                      | guard (audit + repair)
                                                      v
                                          product  /shop/{id}

Buyer /shop -> ask -> kb_answer --> hit  -> instant answer
                                 \-> miss -> escalate on WhatsApp -> maker reply
                                            -> extract fact -> add to KB -> answer buyer
```

## Quick start (one file)

```powershell
python app.py
```

That is it. `app.py` is fully self-contained: it installs any missing dependencies,
starts the server, and opens the browser automatically.

Open http://127.0.0.1:8000 - the simulated WhatsApp UI opens and starts the interview.

Useful environment variables: `PORT` (default 8000), `NO_BROWSER=1` to skip opening the
browser, `HOST` (default 127.0.0.1).

## Quick start (modular version)

The same app is also split into `backend/` + `frontend/` if you prefer to work on it
modularly:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m uvicorn backend.main:app --reload --port 8000
```

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
python -m scripts.smoke_test        # modular version
python -m scripts.smoke_single      # single-file app.py
```

`smoke_test` exercises the whole flow (interview, guard, publish, buyer page, restart,
retry, non-answer handling, voice, reset, invalid input, Twilio webhook). `smoke_single`
additionally covers the compulsory questions, the knowledge-base answers (care, delivery,
making time), seller escalation/answer, and orders. Both exit non-zero on any failure.

## Go live on WhatsApp (Twilio)

You stay the **owner** (you hold the Twilio account + the app). The artisan side runs on
WhatsApp and the **buyer/customer version is a shareable web link** you send to your friend.

### 1. Twilio account + connect your phone
Create a free account at https://www.twilio.com, then
**Messaging > Try out WhatsApp**. Twilio shows a WhatsApp number and a join code, e.g.:

```
Send a WhatsApp message to +1 737 250 8034 with code: join twilio-trial
```

From **your** phone, open WhatsApp and send that join message to that number. Twilio
confirms you are connected. (This is the "with code" option - no business verification.)

Put your credentials in `.env`:

```
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
OPENAI_API_KEY=sk-...        # needed for voice-note transcription
```

### 2. Public URL + run

```powershell
python app.py --live
```

This starts the app and creates a public HTTPS tunnel, then prints the exact webhook URL.
It tries `cloudflared` first if installed (no account needed:
`winget install --id Cloudflare.cloudflared`), otherwise ngrok
(set `NGROK_AUTHTOKEN`, free). If both are blocked, run your own tunnel and set
`PUBLIC_BASE_URL=https://...` in `.env`.

### 3. Point Twilio at your app
On the **Try out WhatsApp** page, open the inbound message settings / auto-reply, choose
**Custom** (a webhook), and set it to `https://<your-public-url>/webhook/twilio`
(method **POST**, content type `application/x-www-form-urlencoded`). Save.

### 4. Test with your friend
- **You (maker):** message the WhatsApp number. Send **photos** and **voice notes** -
  photos are attached to the listing and voice notes are transcribed (with an OpenAI key).
- When you reply **Publish**, the bot messages back the **buyer page link**. Forward that
  link to your friend.
- **Your friend (customer):** opens the link - no WhatsApp needed. They see the photo,
  materials, care, production time, natural variations, and the provenance panel.

Sessions are keyed by phone number, so several testers can share one number without
colliding. The Twilio path and the simulator share the same `handle_message` core.

> Trial accounts can only message numbers that joined/verified. Your friend only needs the
> buyer web link, so they do not need to join WhatsApp.

## Project layout

```
app.py              single-file standalone app (recommended): maker flow + buyer
                    storefront/Q&A loop + escalation, all in one file
docs/
  Architecture_and_Proposed_Solution.docx   full architecture + rationale document
scripts/make_docs.py  regenerates the Word document
backend/
  main.py           FastAPI app: maker, storefront, buyer Q&A, orders, Twilio webhook
  agent.py          interview -> draft -> guard -> reply orchestration + translation
  interview.py      guided questions (compulsory + optional) and reply keywords
  ledger.py         the confirmed-facts Claim Ledger
  draft.py          listing generation
  guard.py          claim audit + deterministic repair  <-- core contribution
  buyer.py          knowledge-base Q&A, seller escalation, orders
  openai_client.py  OpenAI wrapper + offline mock + KB answering + translation
  prompts.py        extraction, drafting, guard, KB, and translation prompts
  transport.py      Twilio normalization, TwiML, WhatsApp send
  sessions.py       in-memory session store
frontend/
  index.html        WhatsApp-style simulator
  app.js            chat logic, quick replies, voice, seller Q&A and order polling
  buyer.html        buyer page with provenance
  shop.html         storefront listing all published products (cart)
  product.html      product detail with buyer Q&A and Buy now
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
