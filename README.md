# WhatsApp texting-clone bot

A personal auto-reply bot that answers WhatsApp messages in your texting style using Google Gemini.
It logs in as a **linked device** (QR code, like WhatsApp Web), so it does not use Meta's official
Cloud API. It is designed to run on Render's free tier, kept awake by UptimeRobot.

- **WhatsApp engine:** [neonize](https://github.com/krypton-byte/neonize) (Python wrapper around whatsmeow). No Chromium or Selenium.
- **AI:** `google-genai` SDK, model `gemini-2.5-flash` by default.
- **Web page:** Flask served by waitress. `/` shows the login QR, or `Status: Connected` once linked. `/health` is for UptimeRobot.
- **Session:** stored in Postgres so you scan the QR once and it survives Render restarts.
- **Knowledge pack (optional):** `build_knowledge.py` turns your exported chats into a profile of your life, per-contact notes and real reply examples, so the bot makes fewer blunders.

## How it behaves

| Behaviour | Detail |
|---|---|
| Who it replies to | `REPLY_MODE=whitelist` (default): only numbers in `WHITELIST`. `REPLY_MODE=all`: every 1:1 chat except `BLOCKLIST`. **Group chats, status updates, channels and your own messages are never answered, in any mode.** |
| Delay | Random 5-15 s (`MIN_DELAY_SECONDS` / `MAX_DELAY_SECONDS`) before every reply, plus a short "typing..." pause. Messages that arrive during the wait are merged into one reply. |
| You take over | If you type in a chat yourself, the bot stays out of it for `HANDOFF_MINUTES` (default 60) and cancels any reply it was about to send. |
| Stays silent when unsure | The model is told to output `[SKIP]` for money/OTP/password requests, spam, strangers, serious or emotional topics, "are you a bot?", or anything it is unsure about. Nothing is sent. If `NOTIFY_NUMBER` is set, you get a WhatsApp note with the message so you can answer yourself. |
| Never invents your life | Rules forbid guessing facts, agreeing to meet/pay/lend, or revealing other people's chats. It is told today's date and time (`TIMEZONE`). |
| Stale messages | Messages older than `MAX_MESSAGE_AGE_SECONDS` (default 15 min), e.g. delivered after a restart, are ignored instead of answered hours late. |
| Gemini 429 / 5xx | Waits and retries (uses Gemini's own "retry in Ns" hint when present, otherwise exponential back-off), up to `GEMINI_MAX_RETRIES`. If it still fails, or the quota is daily, the bot stays silent instead of sending an error. |
| Loop guard | At most `MAX_REPLIES_PER_HOUR` replies per contact (protects you if the other side is also a bot). |
| Chat memory | Last `MAX_HISTORY_MESSAGES` turns per contact, kept in RAM (reset on restart). Older history comes from the knowledge pack. |
| Self-healing | If the QR is not scanned within `QR_STALE_SECONDS`, or a linked session stays offline for `RECONNECT_GRACE_SECONDS`, the process exits and Render restarts it (fresh QR / fresh connection). |

## Files

```
main.py              Flask page + WhatsApp listener + Gemini logic (the whole app)
build_knowledge.py   Run on YOUR computer: exported chats -> knowledge pack (not deployed)
knowledge/           Where the knowledge pack lives (profile.md, style.md, contacts.json, examples.json)
requirements.txt     Python dependencies of the app
Dockerfile           Debian-slim image (neonize ships a prebuilt Go binary; libmagic1 + tzdata installed)
render.yaml          Optional Render Blueprint (Docker, free plan)
.env.example         Every environment variable with an example value
```

## 1. Teach it your style

Open `main.py` and edit the **TEXTING STYLE** section at the top:

- `STYLE_INSTRUCTIONS`: rules such as "all lowercase", "max 12 words", slang you use.
- `FEW_SHOT_EXAMPLES`: `(what they said, what you replied)` pairs. The shipped ones are placeholders.
  If `knowledge/examples.json` exists (step 3) it replaces them with real ones automatically.

## 2. Choose who it answers

Start small: `REPLY_MODE=whitelist` with a few trusted people, watch the Render logs for a few days,
then widen it.

- **Whitelist mode:** set `WHITELIST=256701234567,256772345678` (international format, digits only,
  no `+`, no leading 0).
- **All mode:** set `REPLY_MODE=all`. Put anyone it must never answer (boss, bank, family members you
  prefer to answer yourself, official numbers) in `BLOCKLIST`. Add `ONLY_KNOWN_CONTACTS=true` to answer
  only people listed in `knowledge/contacts.json` and ignore strangers.

Answering everyone is riskier than a whitelist: more conversations means more chances for a wrong or
out-of-character reply, and more outgoing volume for WhatsApp to look at.

## 3. Teach it your life (knowledge pack)

The bot cannot "read all your chats" live. Instead you export them once, a script boils them down, and the
result is added to every prompt. It then knows who people are, your routine, places, plans and how you talk
to each person. Re-run it every few weeks to keep it current.

**a) Export your chats** (1:1 chats that matter, **without media**):
WhatsApp > open a chat > ⋮ (Android) or the contact name (iPhone) > **Export chat** > **Without media**.
Save the `.txt` or `.zip` files into a `chats/` folder next to `build_knowledge.py`. WhatsApp caps each export
(roughly the latest 40,000 messages), which is plenty. Group exports are detected and skipped.

**b) Run it on your own computer:**

```bash
pip install google-genai          # only needed for the AI step
export GEMINI_API_KEY=...         # only needed for the AI step
python build_knowledge.py                 # asks for confirmation before sending anything to Gemini
python build_knowledge.py --no-llm        # fully offline: only examples + style stats + recent messages
python build_knowledge.py --me "Your Name"   # if it cannot tell which sender is you
```

It writes to `knowledge/`:

| File | Content | Made by |
|---|---|---|
| `examples.json` | Real "they said -> I replied" pairs | locally, no AI |
| `style.md` | Measured habits: message length, % lowercase, punctuation, emoji, favourite words, quiet hours | locally, no AI |
| `contacts.json` | Per contact: notes (who they are, how you talk to them), 12 real reply examples, 20 recent messages | notes by Gemini, rest local |
| `profile.md` | Facts about your life: people, routine, places, studies/work, plans | Gemini |

**c) Review it.** Read `profile.md` and fix mistakes; the bot trusts it. Delete anything you do not want it
to know or say. Sensitive-looking content (links, emails, long numbers, passwords/PINs, money and
mobile-money words) is filtered out of examples and recent messages, and redacted before anything goes to Gemini,
but check anyway.

**d) Tell it who is who.** Open `knowledge/contacts.json` and fill in each person's `"numbers"`, e.g.
`"numbers": ["256701234567"]`. Without numbers the bot can still recognise a contact whose WhatsApp profile
name equals their saved name, or whose saved name is just a phone number. Your entries are kept when you
re-run the script.

**e) Give it to the bot**, either way:

- **Render Secret Files (recommended, nothing goes to GitHub):** Render service > **Environment > Secret Files >
  Add Secret File**. Create `profile.md`, `style.md`, `contacts.json` and `examples.json` with the file contents
  pasted in (1 MB total limit). The bot reads them from `/etc/secrets/`. Saving redeploys.
- **Private GitHub repo:** commit the `knowledge/` folder (the Dockerfile copies it). The repo must be
  **private**; it contains other people's messages. Never commit `chats/`.

If a file exists in both places, `knowledge/` wins.

### Privacy: read before you run it

- On Gemini's **free tier**, [Google may use submitted content to improve its products and human reviewers may
  read it](https://ai.google.dev/gemini-api/terms), and it asks you not to submit personal information. This
  applies to the AI step of `build_knowledge.py` **and** to every live reply the bot generates (your contacts'
  messages are sent to Gemini). Enabling billing on the key's Google Cloud project switches it to the paid terms
  (prompts not used to improve products). Consider whether your contacts would be comfortable either way.
- `--no-llm` keeps everything on your computer.
- The knowledge pack contains other people's messages. Treat it like the chats themselves.

## 4. Environment variables

| Variable | Required | Meaning |
|---|---|---|
| `GEMINI_API_KEY` | yes | API key from <https://aistudio.google.com>. |
| `REPLY_MODE` | no | `whitelist` (default) or `all`. Groups are never answered. |
| `WHITELIST` | whitelist mode | Comma-separated phone numbers, international format, digits only. Example: `256701234567,256772345678`. |
| `BLOCKLIST` | no | Numbers that are never answered, in any mode. |
| `ONLY_KNOWN_CONTACTS` | no | In `all` mode, only answer people found in `contacts.json`. Default `false`. |
| `DATABASE_URL` | yes on Render free | Postgres connection string where the WhatsApp session is stored (see below). Without it the session goes to a local SQLite file that Render deletes on every restart. |
| `NOTIFY_NUMBER` | recommended | **Your own** number. When the bot skips a message you get a WhatsApp note. |
| `HANDOFF_MINUTES` | no | Pause a chat this long after you reply by hand. Default `60`, `0` disables. |
| `MAX_MESSAGE_AGE_SECONDS` | no | Ignore older messages. Default `900`. |
| `TIMEZONE` | no | Default `Africa/Kampala`. |
| `PAGE_KEY` | recommended | If set, the QR is only shown at `/?key=<PAGE_KEY>`. Stops a stranger from scanning your QR first and linking *their* account to your bot. |
| `GEMINI_MODEL` | no | Default `gemini-2.5-flash`. |
| `MIN_DELAY_SECONDS` / `MAX_DELAY_SECONDS` | no | Default `5` / `15`. |
| `MAX_HISTORY_MESSAGES` | no | Default `12`. |
| `MAX_REPLIES_PER_HOUR` | no | Default `30` per contact. |
| `SPLIT_MESSAGES` / `MAX_BUBBLES` | no | Default `true` / `3`. |
| `GEMINI_MAX_RETRIES` / `GEMINI_MAX_WAIT_SECONDS` | no | Default `5` / `75`. |
| `QR_STALE_SECONDS` / `RECONNECT_GRACE_SECONDS` | no | Default `180` / `300`. |
| `KNOWLEDGE_DIR` / `SECRETS_DIR` | no | Defaults `knowledge` and `/etc/secrets`. |
| `SESSION_NAME` | no | Default `clone-bot`. |
| `SQLITE_PATH` | no | Local session file when `DATABASE_URL` is empty. Default `session.db`. |
| `LOG_LEVEL` | no | Default `INFO`. |
| `PORT` | no | Render sets this automatically. Default `10000`. |

### Getting a free Postgres

Render's own free Postgres expires after 30 days, so use an external one such as
[Neon](https://neon.tech) or [Supabase](https://supabase.com). Create a database and copy the
**direct (non-pooled)** connection string, e.g.
`postgresql://user:pass@host/dbname?sslmode=require`. Pooled (pgbouncer) strings can break the
WhatsApp session store.

## 5. Deploy on Render

1. Push this folder to a GitHub repository (private if `knowledge/` holds real data).
2. In Render: **New > Web Service**, pick the repo, set **Runtime = Docker** and **Instance type = Free**.
   (Or **New > Blueprint** to use `render.yaml`.)
3. Add the environment variables from the table above (and Secret Files if you use them).
4. Deploy, then open `https://<your-service>.onrender.com/` (add `?key=<PAGE_KEY>` if you set one).
5. On your phone: WhatsApp > **Settings > Linked devices > Link a device**, scan the QR.
   The page changes to **Status: Connected**. You will not need to scan again.
6. Check the logs: at startup you should see `Session storage: Postgres` and a line starting with
   `Knowledge pack` showing the profile size and number of contacts loaded.

### Keep it awake with UptimeRobot

Render spins a free service down after 15 minutes without inbound traffic. In
[UptimeRobot](https://uptimerobot.com) add an **HTTP(s)** monitor for
`https://<your-service>.onrender.com/health` with a 5-minute interval. `/health` always returns
200 (even while not linked), and `HEAD` requests work.

The 750 free instance hours per month cover one always-on service.

## 6. Run locally (optional)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # edit it
set -a; . ./.env; set +a        # the app does not load .env by itself
python main.py                  # http://localhost:10000
```

Leave `DATABASE_URL` empty locally to keep the session in `session.db`.

## Troubleshooting

**A contact gets no reply.** Look in the logs for the reason:
- `Ignored message from non-whitelisted sender (ids seen: [...])`: WhatsApp sometimes identifies a contact by a
  long privacy ID (LID) instead of a phone number. Add that ID to `WHITELIST` as well. Also make sure numbers
  are written `2567...`, not `07...` and not `+256...`.
- `bot paused here because you replied yourself`: the hand-over timer (`HANDOFF_MINUTES`) is running.
- `model chose to stay silent ([SKIP])`: the model judged it risky (you also get a note if `NOTIFY_NUMBER` is set).
- `Ignored stale message`: it was delivered more than `MAX_MESSAGE_AGE_SECONDS` after it was sent.
- `Ignored message from an unknown contact`: `ONLY_KNOWN_CONTACTS=true` and the person is not in `contacts.json`.

**It does not recognise a contact / ignores my notes about them.** Fill in `"numbers"` for that person in
`contacts.json` (digits only, international format).

**The bot answered someone it should not have.** Add them to `BLOCKLIST`, or switch back to
`REPLY_MODE=whitelist`.

**I have to scan the QR again after every restart.** The session is not persisting. Confirm
`DATABASE_URL` is set and reachable. The startup log prints `Session storage: Postgres`. Note that
neonize's README documents a `database=` argument that its released 0.4.x code did not have; `main.py`
supports both forms, but if sessions still do not persist, pin `neonize` to a version you have tested.

**The QR on the page is dead.** QR codes expire after about two minutes. If nobody scans in time the
watchdog restarts the service and a new one appears; just reload the page.

**`Logged out` in the logs.** You removed the device from the phone (or WhatsApp revoked it). The
service restarts itself and shows a new QR.

**Gemini 429 in the logs.** Normal on the free Gemini tier. The bot waits and retries. If the quota is
exhausted for the day it gives up on that message and stays silent.

**`build_knowledge.py` says it cannot tell which sender is you.** Pass `--me "Your Name"` exactly as it appears
in the exports.

**First request after idle is slow.** Render free services take about a minute to wake. UptimeRobot
prevents this.

## Limits (be realistic)

- The knowledge pack is a summary, not a memory of everything. The bot does not know what happened since the
  last export, what you did today, or anything you never texted about, so it can still get things wrong. That is
  what the `[SKIP]` rule, the hand-over pause and the whitelist/blocklist are for.
- Voice notes, images and stickers are ignored (no reply).

## Important warnings

- **This is unofficial.** Automating a personal WhatsApp account through a linked-device library
  goes against WhatsApp's terms of service and can get a number restricted or banned. The random delay,
  whitelist and reply caps reduce the risk but cannot remove it. Consider using a secondary number.
- **People are not told they are talking to a bot.** The bot is instructed to stay silent rather than deny it when
  someone asks, but think about whether that is fair to the people you let it talk to.
- **Treat `DATABASE_URL` and `GEMINI_API_KEY` as secrets.** Anyone with the database can use your
  WhatsApp session. Never commit `.env`.
- Not tested against a live WhatsApp account at the time of writing: watch the logs on first deploy.
