# WhatsApp texting-clone bot

A personal auto-reply bot that answers WhatsApp messages in your texting style using Google Gemini.
It logs in as a **linked device** (QR code, like WhatsApp Web), so it does not use Meta's official
Cloud API. It is designed to run on Render's free tier, kept awake by UptimeRobot.

- **WhatsApp engine:** [neonize](https://github.com/krypton-byte/neonize) (Python wrapper around whatsmeow). No Chromium or Selenium.
- **AI:** `google-genai` SDK, model `gemini-2.5-flash` by default.
- **Web page:** Flask served by waitress. `/` shows the login QR, or `Status: Connected` once linked. `/health` is for UptimeRobot.
- **Session:** stored in Postgres so you scan the QR once and it survives Render restarts.

## How it behaves

| Behaviour | Detail |
|---|---|
| Who it replies to | Only numbers in `WHITELIST`. Group chats, status updates, channels and your own messages are always ignored. |
| Delay | Random 5-15 s (`MIN_DELAY_SECONDS` / `MAX_DELAY_SECONDS`) before every reply. Messages that arrive during the wait are merged into one reply. |
| Typing | Shows "typing..." while generating, and sends multi-line answers as separate short bubbles (`SPLIT_MESSAGES`, `MAX_BUBBLES`). |
| Gemini 429 / 5xx | Waits and retries (uses Gemini's own "retry in Ns" hint when present, otherwise exponential back-off), up to `GEMINI_MAX_RETRIES`. If it still fails, or the quota is daily, the bot stays silent instead of sending an error. |
| Loop guard | At most `MAX_REPLIES_PER_HOUR` replies per contact (protects you if the other side is also a bot). |
| Memory | Last `MAX_HISTORY_MESSAGES` turns per contact, kept in RAM (reset on restart). |
| Self-healing | If the QR is not scanned within `QR_STALE_SECONDS`, or a linked session stays offline for `RECONNECT_GRACE_SECONDS`, the process exits and Render restarts it (fresh QR / fresh connection). |

## Files

```
main.py            Flask page + WhatsApp listener + Gemini logic (the whole app)
requirements.txt   Python dependencies
Dockerfile         Debian-slim image (neonize ships a prebuilt Go binary; libmagic1 installed)
render.yaml        Optional Render Blueprint (Docker, free plan)
.env.example       Every environment variable with an example value
.dockerignore
```

## 1. Teach it your style

Open `main.py` and edit the **TEXTING STYLE** section at the top:

- `STYLE_INSTRUCTIONS`: rules such as "all lowercase", "max 12 words", slang you use.
- `FEW_SHOT_EXAMPLES`: `(what they said, what you replied)` pairs. The shipped ones are placeholders.
  Replace them with 10-20 real exchanges from your own chats; this is what makes the clone convincing.

## 2. Environment variables

| Variable | Required | Meaning |
|---|---|---|
| `GEMINI_API_KEY` | yes | API key from <https://aistudio.google.com>. |
| `WHITELIST` | yes | Comma-separated phone numbers, international format, **digits only, no `+`, no leading 0**. Example: `256701234567,256772345678`. |
| `DATABASE_URL` | yes on Render free | Postgres connection string where the WhatsApp session is stored (see below). Without it the session goes to a local SQLite file that Render deletes on every restart. |
| `PAGE_KEY` | recommended | If set, the QR is only shown at `/?key=<PAGE_KEY>`. Stops a stranger from scanning your QR first and linking *their* account to your bot. |
| `GEMINI_MODEL` | no | Default `gemini-2.5-flash`. |
| `MIN_DELAY_SECONDS` / `MAX_DELAY_SECONDS` | no | Default `5` / `15`. |
| `MAX_HISTORY_MESSAGES` | no | Default `12`. |
| `MAX_REPLIES_PER_HOUR` | no | Default `30` per contact. |
| `SPLIT_MESSAGES` / `MAX_BUBBLES` | no | Default `true` / `3`. |
| `GEMINI_MAX_RETRIES` / `GEMINI_MAX_WAIT_SECONDS` | no | Default `5` / `75`. |
| `QR_STALE_SECONDS` / `RECONNECT_GRACE_SECONDS` | no | Default `180` / `300`. |
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

## 3. Deploy on Render

1. Push this folder to a GitHub repository.
2. In Render: **New > Web Service**, pick the repo, set **Runtime = Docker** and **Instance type = Free**.
   (Or **New > Blueprint** to use `render.yaml`.)
3. Add the environment variables from the table above.
4. Deploy, then open `https://<your-service>.onrender.com/` (add `?key=<PAGE_KEY>` if you set one).
5. On your phone: WhatsApp > **Settings > Linked devices > Link a device**, scan the QR.
   The page changes to **Status: Connected**. You will not need to scan again.

### Keep it awake with UptimeRobot

Render spins a free service down after 15 minutes without inbound traffic. In
[UptimeRobot](https://uptimerobot.com) add an **HTTP(s)** monitor for
`https://<your-service>.onrender.com/health` with a 5-minute interval. `/health` always returns
200 (even while not linked), and `HEAD` requests work.

The 750 free instance hours per month cover one always-on service.

## 4. Run locally (optional)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # edit it
set -a; . ./.env; set +a        # the app does not load .env by itself
python main.py                  # http://localhost:10000
```

Leave `DATABASE_URL` empty locally to keep the session in `session.db`.

## Troubleshooting

**A whitelisted contact gets no reply.** Check the logs for
`Ignored message from non-whitelisted sender (ids seen: [...])`. WhatsApp sometimes identifies a
contact by a long privacy ID (LID) instead of a phone number. Add that ID to `WHITELIST` as well.
Also make sure numbers are written `2567...`, not `07...` and not `+256...`.

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

**First request after idle is slow.** Render free services take about a minute to wake. UptimeRobot
prevents this.

## Important warnings

- **This is unofficial.** Automating a personal WhatsApp account through a linked-device library
  goes against WhatsApp's terms of service and can get a number restricted or banned. The random delay,
  whitelist and reply caps reduce the risk but cannot remove it. Consider using a secondary number.
- **Gemini sees your contacts' messages.** Whitelisted contacts' messages are sent to the Gemini API
  to generate replies. Only whitelist people you are comfortable with that for.
- **Treat `DATABASE_URL` and `GEMINI_API_KEY` as secrets.** Anyone with the database can use your
  WhatsApp session. Never commit `.env`.
- Not tested against a live WhatsApp account at the time of writing: watch the logs on first deploy.
