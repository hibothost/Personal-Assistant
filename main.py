"""
WhatsApp texting-clone bot
==========================

* WhatsApp engine : neonize (whatsmeow under the hood) - Linked Device / QR login,
                    no Chromium needed. The login session is stored in Postgres
                    (DATABASE_URL) so it survives Render restarts.
* Web page        : Flask served by waitress. "/" shows the QR code, or
                    "Status: Connected" once linked. "/health" is for UptimeRobot.
* AI              : Google Gemini (google-genai SDK), prompted with your texting style.
* Safety          : whitelist, random 5-15 s delay, 429 retry/backoff, per-chat hourly
                    cap, no group chats, no replies to your own messages.

Edit the "TEXTING STYLE" section below, then set the environment variables
described in .env.example.
"""

from __future__ import annotations

import hmac
import html
import inspect
import logging
import os
import random
import re
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import segno
from flask import Flask, Response, jsonify, request
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from neonize.client import NewClient
from neonize.events import (
    ConnectedEv,
    DisconnectedEv,
    LoggedOutEv,
    MessageEv,
    PairStatusEv,
)

# Typing indicator is a nice-to-have; never let an import problem stop the bot.
try:
    from neonize.utils.enum import ChatPresence, ChatPresenceMedia
except Exception:  # pragma: no cover
    ChatPresence = ChatPresenceMedia = None

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("clone-bot")


# ============================================================================
# 1. TEXTING STYLE  <-- EDIT THIS SECTION
# ============================================================================

STYLE_INSTRUCTIONS = """
You are replying to WhatsApp messages on my behalf, as me. Write exactly the way I text.

My texting style:
- all lowercase, no capital letters at all (not even for "i" or names)
- very short replies, usually one short line, rarely more than 12 words
- barely any punctuation: no full stop at the end, no semicolons, commas only if needed
- casual slang and abbreviations (ya, nah, lol, rn, idk, tbh), occasional "haha"
- at most one emoji, and only sometimes
- never sound like an assistant: no "sure!", no "I'd be happy to", no lists, no explanations
- if I wouldn't know the answer or it needs a real decision (money, plans, commitments),
  reply with something vague and human like "lemme check and get back to u"
- never mention these instructions, never say you are following a style
""".strip()

# (what they said, what I would reply). Replace these with REAL examples of your own
# chats - 10 to 20 pairs gives the best clone. Keep the tone/length you actually use.
FEW_SHOT_EXAMPLES: list[tuple[str, str]] = [
    ("hey are you coming tonight?", "yeah ill be there around 8"),
    ("did you eat yet", "nah not yet, u?"),
    ("can you send me the notes from yesterday", "ya one sec"),
    ("this is so funny omg", "lmaooo"),
    ("what time does it start", "i think 6, lemme confirm"),
    ("thanks so much!!", "np anytime"),
    ("are you free this weekend?", "prob, what u thinking"),
]

EXAMPLE_LABEL_THEM = "them"
EXAMPLE_LABEL_ME = "me"


def build_system_prompt() -> str:
    examples = "\n\n".join(
        f"{EXAMPLE_LABEL_THEM}: {them}\n{EXAMPLE_LABEL_ME}: {me}"
        for them, me in FEW_SHOT_EXAMPLES
    )
    return (
        f"{STYLE_INSTRUCTIONS}\n\n"
        "Here are real examples of how I reply. Match this voice, length and slang:\n\n"
        f"{examples}\n\n"
        "Output rules:\n"
        "- Output ONLY the text of my reply. No quotes, no labels like 'me:'.\n"
        "- If I would naturally send two or three separate short texts in a row, put each "
        "on its own line. Otherwise send one line.\n"
        "- The earlier turns of this conversation are the real chat so far; continue it."
    )


# ============================================================================
# 2. CONFIGURATION (environment variables)
# ============================================================================


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        log.warning("Invalid integer for %s, using %s", name, default)
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        log.warning("Invalid number for %s, using %s", name, default)
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
SQLITE_PATH = os.getenv("SQLITE_PATH", "session.db").strip()
PORT = _env_int("PORT", 10000)  # Render injects PORT
PAGE_KEY = os.getenv("PAGE_KEY", "").strip()  # optional: hides the QR unless ?key=...

MIN_DELAY = _env_float("MIN_DELAY_SECONDS", 5.0)
MAX_DELAY = _env_float("MAX_DELAY_SECONDS", 15.0)
if MIN_DELAY > MAX_DELAY:
    MIN_DELAY, MAX_DELAY = MAX_DELAY, MIN_DELAY

MAX_HISTORY = _env_int("MAX_HISTORY_MESSAGES", 12)  # turns remembered per contact
MAX_REPLIES_PER_HOUR = _env_int("MAX_REPLIES_PER_HOUR", 30)  # per contact, loop guard
SPLIT_MESSAGES = _env_bool("SPLIT_MESSAGES", True)  # one WhatsApp bubble per line
MAX_BUBBLES = _env_int("MAX_BUBBLES", 3)
GEMINI_MAX_RETRIES = _env_int("GEMINI_MAX_RETRIES", 5)
GEMINI_MAX_WAIT = _env_float("GEMINI_MAX_WAIT_SECONDS", 75.0)  # longest single back-off
QR_STALE_SECONDS = _env_int("QR_STALE_SECONDS", 180)  # unlinked: refresh QR after this
RECONNECT_GRACE_SECONDS = _env_int("RECONNECT_GRACE_SECONDS", 300)  # linked but offline
SESSION_NAME = os.getenv("SESSION_NAME", "clone-bot").strip()

# Phone numbers in international format, digits only, no "+": 256701234567
WHITELIST: set[str] = {
    d for d in (_digits(x) for x in os.getenv("WHITELIST", "").split(",")) if d
}


def validate_config() -> None:
    problems = []
    if not GEMINI_API_KEY:
        problems.append("GEMINI_API_KEY is not set")
    if not WHITELIST:
        problems.append(
            "WHITELIST is empty - the bot will not reply to anyone "
            "(example: WHITELIST=256701234567,256772345678)"
        )
    for number in WHITELIST:
        if number.startswith("0"):
            log.warning(
                "Whitelist entry %r starts with 0. Use the international format "
                "without '+' (e.g. 256701234567).",
                number,
            )
    if not DATABASE_URL:
        log.warning(
            "DATABASE_URL not set: the WhatsApp session is stored in %s on local disk. "
            "On Render's free tier that disk is wiped on every restart/redeploy, so you "
            "would have to re-scan the QR code each time.",
            SQLITE_PATH,
        )
    for problem in problems:
        log.error("CONFIG: %s", problem)
    if not GEMINI_API_KEY:
        raise SystemExit("Missing GEMINI_API_KEY")


# ============================================================================
# 3. GEMINI (with 429 / 5xx retry)
# ============================================================================

_gemini_client: genai.Client | None = None
_gemini_lock = threading.Lock()


def get_gemini() -> genai.Client:
    global _gemini_client
    with _gemini_lock:
        if _gemini_client is None:
            _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        return _gemini_client


def _generation_config() -> types.GenerateContentConfig:
    return types.GenerateContentConfig(
        system_instruction=build_system_prompt(),
        temperature=0.9,
        max_output_tokens=200,
        # Thinking is wasted on casual texts and would eat the output budget.
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )


_RETRY_HINT = re.compile(r"retry(?:Delay)?\W{1,6}(?:in\W+)?(\d+(?:\.\d+)?)\s*s", re.I)


def _suggested_wait(err: Exception) -> float | None:
    """Gemini 429s usually say 'Please retry in 34.1s' / retryDelay: '34s'."""
    blob = f"{getattr(err, 'message', '')} {getattr(err, 'details', '')} {err}"
    match = _RETRY_HINT.search(blob)
    return float(match.group(1)) if match else None


def build_contents(history: list[tuple[str, str]], incoming: str) -> list[types.Content]:
    """Real chat history + the new message, merged so roles strictly alternate."""
    turns = [*history, ("user", incoming)]
    while turns and turns[0][0] != "user":  # Gemini wants the first turn to be 'user'
        turns.pop(0)

    merged: list[list] = []
    for role, text in turns:
        if merged and merged[-1][0] == role:
            merged[-1][1] += "\n" + text
        else:
            merged.append([role, text])
    return [
        types.Content(role=role, parts=[types.Part(text=text)]) for role, text in merged
    ]


def generate_reply(history: list[tuple[str, str]], incoming: str) -> str | None:
    """Ask Gemini for a reply. Returns None if it should stay silent."""
    contents = build_contents(history, incoming)
    config = _generation_config()

    for attempt in range(1, GEMINI_MAX_RETRIES + 1):
        try:
            response = get_gemini().models.generate_content(
                model=GEMINI_MODEL, contents=contents, config=config
            )
            text = (response.text or "").strip()
            text = re.sub(r"^\s*me\s*:\s*", "", text, flags=re.I)
            if not text:
                log.warning("Gemini returned no text (blocked or empty); staying silent")
                return None
            return text

        except genai_errors.APIError as err:
            code = getattr(err, "code", None)
            retryable = code == 429 or (isinstance(code, int) and code >= 500)
            if not retryable:
                log.error("Gemini error %s (not retryable): %s", code, err)
                return None
            if attempt == GEMINI_MAX_RETRIES:
                log.error("Gemini still failing (%s) after %d attempts; giving up", code, attempt)
                return None

            suggested = _suggested_wait(err) if code == 429 else None
            if suggested is not None and suggested > GEMINI_MAX_WAIT:
                # Almost certainly a daily quota, not a per-minute limit: waiting is pointless.
                log.error("Gemini asks to wait %.0fs (> %.0fs cap); giving up", suggested, GEMINI_MAX_WAIT)
                return None
            wait = (suggested + random.uniform(0.5, 2.0)) if suggested is not None else min(
                4 * 2 ** (attempt - 1) + random.uniform(0, 2), GEMINI_MAX_WAIT
            )
            log.warning(
                "Gemini %s (attempt %d/%d) - waiting %.1fs then retrying",
                code, attempt, GEMINI_MAX_RETRIES, wait,
            )
            time.sleep(wait)

        except Exception:  # network hiccup, SDK bug, ...
            log.exception("Unexpected Gemini failure")
            if attempt == GEMINI_MAX_RETRIES:
                return None
            time.sleep(min(2 ** attempt, 20))
    return None


# ============================================================================
# 4. PER-CHAT STATE + REPLY PIPELINE
# ============================================================================


class ChatState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.pending: list[str] = []
        self.busy = False
        self.jid = None  # JID object to send the reply to
        self.history: deque[tuple[str, str]] = deque(maxlen=MAX_HISTORY)
        self.sent_at: deque[float] = deque()


_chats: dict[str, ChatState] = {}
_chats_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="reply")


def _get_chat(key: str) -> ChatState:
    with _chats_lock:
        return _chats.setdefault(key, ChatState())


def _under_hourly_cap(chat: ChatState) -> bool:
    now = time.time()
    while chat.sent_at and now - chat.sent_at[0] > 3600:
        chat.sent_at.popleft()
    return len(chat.sent_at) < MAX_REPLIES_PER_HOUR


def _set_typing(wa: NewClient, jid, on: bool) -> None:
    if ChatPresence is None:
        return
    try:
        state = ChatPresence.CHAT_PRESENCE_COMPOSING if on else ChatPresence.CHAT_PRESENCE_PAUSED
        wa.send_chat_presence(jid, state, ChatPresenceMedia.CHAT_PRESENCE_MEDIA_TEXT)
    except Exception:
        log.debug("typing indicator failed", exc_info=True)


def _send_reply(wa: NewClient, jid, text: str) -> None:
    parts = [p.strip() for p in text.splitlines() if p.strip()] if SPLIT_MESSAGES else [text]
    for i, part in enumerate(parts[: max(1, MAX_BUBBLES)]):
        if i:
            _set_typing(wa, jid, True)
            time.sleep(random.uniform(1.0, 3.0))
        wa.send_message(jid, part)
    _set_typing(wa, jid, False)


def _process_chat(wa: NewClient, key: str) -> None:
    chat = _get_chat(key)
    finished = False
    try:
        while True:
            # Random human-like delay BEFORE replying. Messages that arrive during the
            # wait are merged into one reply instead of triggering one reply each.
            delay = random.uniform(MIN_DELAY, MAX_DELAY)
            log.info("[%s] waiting %.1fs before replying", key, delay)
            time.sleep(delay)

            with chat.lock:
                if not chat.pending:
                    chat.busy = False
                    finished = True
                    return
                batch, chat.pending = chat.pending, []
                jid = chat.jid
                allowed = _under_hourly_cap(chat)

            incoming = "\n".join(batch)
            if not allowed:
                log.warning("[%s] hourly reply cap (%d) reached - not replying", key, MAX_REPLIES_PER_HOUR)
                continue

            _set_typing(wa, jid, True)
            reply = generate_reply(list(chat.history), incoming)
            if not reply:
                _set_typing(wa, jid, False)
                chat.history.append(("user", incoming))
                continue

            _send_reply(wa, jid, reply)
            chat.history.append(("user", incoming))
            chat.history.append(("model", reply))
            chat.sent_at.append(time.time())
            log.info("[%s] reply sent (%d chars)", key, len(reply))
    except Exception:
        log.exception("[%s] failed while processing chat", key)
    finally:
        if not finished:
            with chat.lock:
                chat.busy = False


def _enqueue(wa: NewClient, key: str, jid, text: str) -> None:
    chat = _get_chat(key)
    with chat.lock:
        chat.pending.append(text)
        chat.jid = jid
        if chat.busy:
            return
        chat.busy = True
    _executor.submit(_process_chat, wa, key)


# ============================================================================
# 5. WHATSAPP (neonize)
# ============================================================================

_state_lock = threading.Lock()
_state = {
    "connected": False,
    "paired": False,  # True once a login has happened in this process
    "logged_out": False,
    "qr": None,  # latest QR payload (str)
    "qr_at": 0.0,
    "started": time.time(),
    "down_since": time.time(),  # when we last stopped being connected
}


def make_client() -> NewClient:
    """Create the WhatsApp client, keeping the login session in Postgres if configured."""
    accepts_database_kwarg = "database" in inspect.signature(NewClient.__init__).parameters
    location = DATABASE_URL or SQLITE_PATH
    if accepts_database_kwarg:  # newer neonize
        log.info("Session storage: %s", "Postgres" if DATABASE_URL else SQLITE_PATH)
        return NewClient(SESSION_NAME, database=location)
    # Released neonize: the first argument doubles as the database location. A string
    # starting with "postgres" is treated as a Postgres URL, anything else as a SQLite file.
    log.info("Session storage: %s", "Postgres" if DATABASE_URL else SQLITE_PATH)
    return NewClient(location)


wa_client = make_client()


def _extract_text(message) -> str:
    try:
        return (message.conversation or message.extendedTextMessage.text or "").strip()
    except AttributeError:
        return ""


def _candidate_ids(source) -> set[str]:
    """Phone number and/or LID (WhatsApp's privacy id) of the other person."""
    ids = set()
    for jid in (source.Sender, getattr(source, "SenderAlt", None), source.Chat):
        user = _digits(getattr(jid, "User", ""))
        if user:
            ids.add(user)
    return ids


def _set_state(**changes) -> None:
    with _state_lock:
        _state.update(changes)


def _on_qr(_: NewClient, data_qr: bytes) -> None:
    payload = data_qr.decode("utf-8", "ignore") if isinstance(data_qr, (bytes, bytearray)) else str(data_qr)
    _set_state(qr=payload, qr_at=time.time())
    log.info("New QR code generated - open the web page to scan it")


wa_client.event.qr(_on_qr)


@wa_client.event(ConnectedEv)
def _on_connected(_: NewClient, __: ConnectedEv) -> None:
    _set_state(connected=True, paired=True, logged_out=False, qr=None)
    log.info("WhatsApp connected")


@wa_client.event(PairStatusEv)
def _on_pair(_: NewClient, __: PairStatusEv) -> None:
    _set_state(paired=True, qr=None)
    log.info("WhatsApp account linked")


@wa_client.event(DisconnectedEv)
def _on_disconnected(_: NewClient, __: DisconnectedEv) -> None:
    _set_state(connected=False, down_since=time.time())
    log.warning("WhatsApp disconnected (it will try to reconnect by itself)")


@wa_client.event(LoggedOutEv)
def _on_logged_out(_: NewClient, __: LoggedOutEv) -> None:
    _set_state(connected=False, paired=False, logged_out=True, qr=None)
    log.error("Logged out from the phone. Restarting in 5s to show a fresh QR code.")
    threading.Timer(5.0, lambda: os._exit(4)).start()  # Render restarts the service


@wa_client.event(MessageEv)
def _on_message(wa: NewClient, event: MessageEv) -> None:
    try:
        source = event.Info.MessageSource
        if source.IsFromMe or source.IsGroup:
            return  # my own messages, group chats
        if source.Chat.Server not in ("s.whatsapp.net", "lid"):
            return  # status updates, broadcast lists, channels

        text = _extract_text(event.Message)
        if not text:
            return  # images, stickers, voice notes, reactions...

        ids = _candidate_ids(source)
        matched = ids & WHITELIST
        if not matched:
            log.info("Ignored message from non-whitelisted sender (ids seen: %s)", sorted(ids))
            return

        key = sorted(matched)[0]
        log.info("[%s] incoming message (%d chars)", key, len(text))
        _enqueue(wa, key, source.Chat, text)
    except Exception:
        log.exception("Error in message handler")


def _run_whatsapp() -> None:
    while True:
        try:
            wa_client.connect()
            # connect() either blocks for the life of the connection or returns right
            # after starting it. Either way, never call it twice: park this thread and
            # let the watchdog below restart the whole process if the link dies.
            log.info("WhatsApp client started")
            threading.Event().wait()
        except Exception:
            log.exception("WhatsApp client failed to start; retrying in 10s")
            time.sleep(10)


def _watchdog() -> None:
    """Self-healing: restart the process (Render restarts it automatically) when
    - nobody scanned the QR in time, so a fresh QR gets generated, or
    - a previously working connection has been down for too long."""
    while True:
        time.sleep(15)
        now = time.time()
        with _state_lock:
            if _state["connected"]:
                continue
            if not _state["paired"]:
                stale = now - (_state["qr_at"] or _state["started"]) > QR_STALE_SECONDS
                reason = f"no successful link after {QR_STALE_SECONDS}s (fresh QR needed)"
            else:
                stale = now - _state["down_since"] > RECONNECT_GRACE_SECONDS
                reason = f"connection down for more than {RECONNECT_GRACE_SECONDS}s"
        if stale:
            log.warning("Watchdog: %s - restarting the process", reason)
            os._exit(3)


# ============================================================================
# 6. WEB INTERFACE (Flask)
# ============================================================================

app = Flask(__name__)

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{refresh}">
<title>WhatsApp clone bot</title>
<style>
  :root {{ --bg:#f5f6f4; --card:#fff; --fg:#17201a; --muted:#5d6b61; --ok:#12805c; --warn:#a15c00; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#101512; --card:#1a211c; --fg:#e8efe9; --muted:#9aa89e; --ok:#3ddc97; --warn:#ffb454; }}
  }}
  body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:var(--bg);
         color:var(--fg); font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }}
  main {{ background:var(--card); border-radius:16px; padding:28px 24px; margin:16px;
          width:min(92vw,380px); text-align:center; box-shadow:0 2px 20px rgba(0,0,0,.08); }}
  h1 {{ font-size:1.25rem; margin:0 0 6px; }}
  h1.ok {{ color:var(--ok); }} h1.warn {{ color:var(--warn); }}
  p {{ color:var(--muted); margin:6px 0; font-size:.92rem; }}
  img {{ width:100%; height:auto; margin-top:14px; border-radius:8px; background:#fff; }}
</style>
</head>
<body><main>
{body}
</main></body></html>"""


def _page(body: str, refresh: int) -> Response:
    resp = Response(PAGE.format(body=body, refresh=refresh), mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/")
def index():
    with _state_lock:
        state = dict(_state)

    if state["connected"]:
        return _page('<h1 class="ok">Status: Connected</h1><p>The bot is linked and replying.</p>', 60)

    if state["logged_out"]:
        return _page(
            '<h1 class="warn">Status: Logged out</h1><p>Restarting - a new QR code will appear in a moment.</p>',
            5,
        )

    qr_visible = not PAGE_KEY or hmac.compare_digest(request.args.get("key", ""), PAGE_KEY)
    if state["qr"] and not state["paired"]:
        if not qr_visible:
            return _page(
                '<h1 class="warn">Status: Waiting for link</h1><p>Open this page with ?key=YOUR_PAGE_KEY to see the QR code.</p>',
                10,
            )
        qr = segno.make_qr(state["qr"], error="l")
        img = qr.svg_data_uri(scale=8, border=3, dark="#000000", light="#ffffff")
        return _page(
            '<h1 class="warn">Status: Waiting for QR scan</h1>'
            f'<img alt="WhatsApp login QR code" src="{html.escape(img, quote=True)}">'
            "<p>WhatsApp &rarr; Settings &rarr; Linked devices &rarr; Link a device.</p>"
            "<p>This page refreshes automatically.</p>",
            5,
        )

    return _page(
        '<h1 class="warn">Status: Starting&hellip;</h1><p>Connecting to WhatsApp. This page refreshes automatically.</p>',
        4,
    )


@app.get("/health")
def health():
    # Always 200 so UptimeRobot keeps the service awake even while not linked.
    with _state_lock:
        return jsonify(ok=True, connected=_state["connected"])


# ============================================================================
# 7. ENTRYPOINT
# ============================================================================


def main() -> None:
    validate_config()
    log.info(
        "Starting: model=%s whitelist=%d number(s) delay=%.0f-%.0fs",
        GEMINI_MODEL, len(WHITELIST), MIN_DELAY, MAX_DELAY,
    )
    threading.Thread(target=_run_whatsapp, name="whatsapp", daemon=True).start()
    threading.Thread(target=_watchdog, name="watchdog", daemon=True).start()

    from waitress import serve  # imported here so tests can import this module without it

    serve(app, host="0.0.0.0", port=PORT, threads=4)


if __name__ == "__main__":
    main()
