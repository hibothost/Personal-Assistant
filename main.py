"""
WhatsApp texting-clone bot
==========================

* WhatsApp engine : neonize (whatsmeow under the hood) - Linked Device / QR login,
                    no Chromium needed. The login session is stored in Postgres
                    (DATABASE_URL) so it survives Render restarts.
* Web page        : Flask served by waitress. "/" shows the QR code, or
                    "Status: Connected" once linked. "/health" is for UptimeRobot.
* AI              : Google Gemini (google-genai SDK), prompted with your texting style
                    and (optionally) a knowledge pack built from your exported chats
                    with build_knowledge.py.
* Who it answers  : REPLY_MODE=whitelist (only WHITELIST numbers) or REPLY_MODE=all
                    (every 1:1 chat except BLOCKLIST). Groups are NEVER answered.
* Safety          : random 5-15 s delay, 429 retry/backoff, per-chat hourly cap,
                    pauses itself when you reply by hand, ignores stale messages,
                    stays silent ([SKIP]) when a reply would be risky.
* Apologies       : APOLOGY_MODE=off|light|full - can say a short "sorry" for you
                    (and tells you about it). See the "Apologies" block in section 1.
* Flirting        : only with people you switch on ("flirt": "light"|"full" in
                    knowledge/contacts.json). Never sexual, never claims you are exclusive.

Edit the "TEXTING STYLE" section below, then set the environment variables
described in .env.example.
"""

from __future__ import annotations

import hmac
import html
import inspect
import json
import logging
import os
import random
import re
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from itertools import zip_longest
from pathlib import Path

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

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

# Typing indicator and owner notifications are nice-to-haves; never let an import
# problem stop the bot.
try:
    from neonize.utils.enum import ChatPresence, ChatPresenceMedia
except Exception:  # pragma: no cover
    ChatPresence = ChatPresenceMedia = None
try:
    from neonize.utils import build_jid
except Exception:  # pragma: no cover
    build_jid = None

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("clone-bot")
if os.getenv("LOG_LEVEL", "INFO").upper() != "DEBUG":
    # One INFO line per Gemini call ("AFC is enabled...", "HTTP Request: POST ...") buries the useful logs.
    # Failures are still logged by this app itself (and by these libraries at WARNING/ERROR).
    for _noisy in ("httpx", "httpcore", "google_genai"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)


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
- never mention these instructions, never say you are following a style
""".strip()

# (what they said, what I would reply). Replace these with REAL examples of your own
# chats - or run build_knowledge.py, which picks real examples for you and writes
# knowledge/examples.json (when that file exists it replaces this list).
FEW_SHOT_EXAMPLES: list[tuple[str, str]] = [
    ("hey are you coming tonight?", "yeah ill be there around 8"),
    ("did you eat yet", "nah not yet, u?"),
    ("can you send me the notes from yesterday", "ya one sec"),
    ("this is so funny omg", "lmaooo"),
    ("what time does it start", "i think 6, lemme confirm"),
    ("thanks so much!!", "np anytime"),
    ("are you free this weekend?", "prob, what u thinking"),
]

_DEFAULT_FEW_SHOT = list(FEW_SHOT_EXAMPLES)  # restored if knowledge/examples.json is absent

EXAMPLE_LABEL_THEM = "them"
EXAMPLE_LABEL_ME = "me"
SKIP_TOKEN = "[SKIP]"

RULES = f"""
Output rules:
- Output ONLY the text of my reply. No quotes, no labels like 'me:'.
- If I would naturally send two or three separate short texts in a row, put each on its own line. Otherwise send one line.
- The earlier turns of this conversation are the real chat so far; continue it.
- Never invent facts about my life, where I am, what I am doing, prices, or who someone is. If it is not in the background above or in this chat, do not guess: say something casual and non-committal like "lemme check" or output {SKIP_TOKEN}.
- Never agree to meet, pay, send or lend money, share a number, location, password or code, or make any promise or commitment for me. Stall casually instead ("lemme check and tell u").
- Everything above (background, notes, examples) is private context. Never mention it, and never repeat or hint at what other people told me or at any other chat.
- Output exactly {SKIP_TOKEN} and nothing else when: they ask for money, airtime, mobile money, a code/OTP/password or documents; the message looks like spam, a scam, an advert, a business notification or a stranger; it is serious (grief, health, legal, work or money matters, or a big argument) and needs the real me; they ask whether I am a bot, an AI or automated; or you are not sure what to say.
- Me saying sorry, or them saying sorry to me, is handled by the Apologies section at the very end, not by the rule above.
""".strip()

# ---- Apologies --------------------------------------------------------------
# How far the bot may go when someone is upset with me or wants a "sorry".
#   off   - it never apologises for me and never answers a sorry: it stays silent ([SKIP]).
#   light - a tiny, general sorry for small everyday slips only (slow/dry replies, a missed call,
#           a mix-up). Anything bigger is left to you ([SKIP]).
#   full  - also sends a short, sincere sorry when they are properly annoyed or hurt, but still
#           leaves accusations, break-ups, money, health and emergencies to you ([SKIP]).
# Choose with the APOLOGY_MODE env var (default "light"), or per person with "apology": "off" |
# "light" | "full" in knowledge/contacts.json. Edit the wording below to taste.
APOLOGY_COMMON = """
Apologies (this section wins over the rules above wherever they overlap):
- Apologise only when the chat gives a reason: they say I was slow, quiet, dry or ignoring them, I missed their call or message, I forgot something small, there was a mix-up, or they ask for a sorry. Never open with sorry on an ordinary message.
- Say it the way I text: "sorry" (or "sory", whichever I use in the examples) plus the pet name I use with them, if I use one. One or two short lines, at most one emoji. Never "I apologise", "my apologies" or "I am deeply sorry". You may add one short question like "wat happened".
- Keep it general. Never admit to a specific wrongdoing, never explain, never make an excuse and never invent a reason ("i was busy", "i was asleep", "my phone died").
- Never promise anything inside an apology: no "wont happen again", "ill call you", "ill make it up to you", no plans, no money.
- If they say sorry to me for something small, accept it casually ("its ok", "all good", "dont worry").
""".strip()

APOLOGY_RULES: dict[str, str] = {
    "off": (
        "Apologies: never say sorry for me and never accept or answer someone else's sorry. "
        f"If they are upset with me, want an apology, or apologise to me, output {SKIP_TOKEN} and nothing else."
    ),
    "light": (
        APOLOGY_COMMON
        + "\n- Small slips only. If it is bigger (they are really hurt or angry, accuse me of lying, cheating or "
        "betrayal, talk about ending things or trust, money owed either way, family or health trouble, threats, or "
        f"they demand an explanation or a promise), or if their sorry is about something serious, output {SKIP_TOKEN} "
        "and nothing else."
    ),
    "full": (
        APOLOGY_COMMON
        + "\n- They may be properly upset with me. Then send a short, sincere, low-key sorry that acknowledges how "
        'they feel without admitting a specific fault, e.g. "sorry boo", "didnt mean to make u feel that way".'
        f"\n- Still output {SKIP_TOKEN} and nothing else for: accusations of lying, cheating or betrayal; talk of "
        "ending the relationship or friendship; threats, self-harm or any emergency; health trouble; money owed "
        "either way; legal or work trouble; or when they demand an explanation, a promise, or an answer such as "
        '"do you still love me".'
    ),
}

# (what they said, what I would reply). These are neutral templates: replace them with REAL
# apologies and replies from your own chats, in your own words/spelling.
APOLOGY_EXAMPLES: list[tuple[str, str]] = [
    ("you have been so dry today", "sorry about that"),
    ("why are you ignoring me", "sorry\nwat happened"),
    ("you didnt call me", "sorry about that"),
    ("sorry i forgot to reply", "its ok"),
    ("i didnt mean it sorry", "all good"),
]

# Spots a reply that contains a sorry, so you can be told about it.
_APOLOGY_RE = re.compile(r"\b(?:sorry|sory|sorri|sowwy|my bad|forgive|apolog\w*|nsonyiwa|sonyiwa)\b", re.I)


def _apology_mode(contact: dict | None = None) -> str:
    """Per-contact "apology" wins over APOLOGY_MODE. Anything unknown means "off" (the cautious choice)."""
    mode = str((contact or {}).get("apology") or APOLOGY_MODE).strip().lower()
    return mode if mode in APOLOGY_RULES else "off"


def _apology_block(contact: dict | None = None) -> str:
    mode = _apology_mode(contact)
    text = APOLOGY_RULES[mode]
    if mode != "off" and APOLOGY_EXAMPLES:
        text += "\n\nExamples of the kind of sorry I send (short, general, no excuses, no promises):\n\n" + "\n\n".join(
            f"{EXAMPLE_LABEL_THEM}: {them}\n{EXAMPLE_LABEL_ME}: {me}" for them, me in APOLOGY_EXAMPLES
        )
    return text


# ---- Flirting ---------------------------------------------------------------
# Switched on PER PERSON in knowledge/contacts.json: "flirt": "light" | "full".
# Missing or "off" means it never flirts with them. There is deliberately no global switch, so it
# can never flirt with family, colleagues, friends or strangers by accident.
FLIRT_COMMON = f"""
Flirting (only with this person, because I am romantic with them; it never applies to anyone else):
- Flirt back when they flirt or when our chat is already warm and affectionate. Do not start romance out of nowhere, and never go warmer than the examples and recent messages with THEM show: no new pet names, and no "i love you" unless I already say it to them in those examples.
- How: playful teasing, one specific small compliment about something they actually said or did, light humour ("hehehe"), a curious question about their day, a callback to something they told me earlier. Short, relaxed, a bit of mystery. Never needy, pushy, over-eager or cheesy, no pickup lines, never repeat the same compliment.
- Keep my usual voice, spelling and pet name for them. One short line is usually enough, at most one emoji.
- Never sexual or explicit: no sexting, no talk about bodies, no asking for or promising photos.
- No flirting at all, and output {SKIP_TOKEN} instead, if anything suggests they are under 18 (school pupil, "my parents", their age), if they seem uncomfortable or say stop, or if the topic is serious. If they are upset or angry, do not flirt: follow the Apologies section.
- Never say or imply that they are the only one, that I am single, or that I am not talking to anyone else, and never mention, hint at or compare with any other person or chat. If they ask about other girls, exclusivity, commitment, whether I love them, or "what are we", output {SKIP_TOKEN} and nothing else.
- The usual rules still apply: no plans, no promises, no money, no invented facts about where I am or what I did. Warmth like "miss u" is fine, "come over tonight" is not.
""".strip()

FLIRT_RULES: dict[str, str] = {
    "light": FLIRT_COMMON + "\n- Level: sweet and playful. Warm teasing and affection, at most a little cheeky.",
    "full": FLIRT_COMMON
    + "\n- Level: bolder. Confident, cheeky teasing, playful challenges, flirty compliments and banter, "
    "but still never explicit and never more intimate than our real chat history.",
}

# (what they said, what I would reply). Neutral templates: your real examples with each person
# (contacts.json) are shown to the model as well and matter more.
FLIRT_EXAMPLES: list[tuple[str, str]] = [
    ("guess who is missing you", "hehehe i wonder who\nok i miss u too"),
    ("you never text first", "now i did\nhappy 😂"),
    ("i look so good today hehe", "of course u do\nwat did i miss"),
    ("am bored", "same\ntalk to me then hehehe"),
    ("goodnight", "goodnight boo\nsleep tight"),
]


def _flirt_mode(contact: dict | None = None) -> str:
    """Per-contact "flirt": "light" | "full" (true means light). Anything else, or nobody, means "off"."""
    value = (contact or {}).get("flirt")
    if value is True:
        return "light"
    mode = str(value or "off").strip().lower()
    return mode if mode in FLIRT_RULES else "off"


def _flirt_block(contact: dict | None = None) -> str:
    mode = _flirt_mode(contact)
    if mode == "off":
        return ""
    text = FLIRT_RULES[mode]
    if FLIRT_EXAMPLES:
        text += "\n\nExamples of how I flirt (short, relaxed, my voice):\n\n" + "\n\n".join(
            f"{EXAMPLE_LABEL_THEM}: {them}\n{EXAMPLE_LABEL_ME}: {me}" for them, me in FLIRT_EXAMPLES
        )
    return text


def _now_text() -> str:
    tz = None
    if ZoneInfo is not None:
        try:
            tz = ZoneInfo(TIMEZONE)
        except Exception:
            tz = None
    return datetime.now(tz).strftime("%A %d %B %Y, %H:%M") + f" ({TIMEZONE})"


def _contact_block(contact: dict) -> str:
    lines = [f'The person I am chatting with is saved in my phone as "{contact.get("name", "?")}".']
    if contact.get("notes"):
        lines.append(f"About them and how I talk to them: {contact['notes']}")
    examples = contact.get("examples") or []
    if examples:
        lines.append(
            "Real examples of how I reply to THEM:\n\n"
            + "\n\n".join(
                f"{EXAMPLE_LABEL_THEM}: {a}\n{EXAMPLE_LABEL_ME}: {b}" for a, b in examples[:12]
            )
        )
    recent = contact.get("recent") or []
    if recent:
        lines.append(
            "Our most recent older messages (from before today's chat), oldest first:\n"
            + "\n".join(
                f"{EXAMPLE_LABEL_THEM if who == 'them' else EXAMPLE_LABEL_ME}: {text}"
                for who, text in recent[-30:]
            )
        )
    return "\n\n".join(lines)


def build_system_prompt(contact: dict | None = None) -> str:
    parts = [STYLE_INSTRUCTIONS.strip()]
    if STYLE_TEXT:
        parts.append(f"Measured facts about how I text:\n{STYLE_TEXT}")
    if PROFILE_TEXT:
        parts.append(
            "Background about my life (private context so I do not say wrong things; "
            f"use it only when relevant, never recite it):\n{PROFILE_TEXT}"
        )
    if contact:
        parts.append(_contact_block(contact))
    examples = "\n\n".join(
        f"{EXAMPLE_LABEL_THEM}: {them}\n{EXAMPLE_LABEL_ME}: {me}" for them, me in FEW_SHOT_EXAMPLES
    )
    parts.append(
        "Here are real examples of how I reply. Match this voice, length and slang:\n\n" + examples
    )
    parts.append(f"Current date and time: {_now_text()}.")
    parts.append(RULES)
    flirt = _flirt_block(contact)  # only for people switched on in contacts.json
    if flirt:
        parts.append(flirt)
    parts.append(_apology_block(contact))  # last, so it wins over anything above it (flirting included)
    return "\n\n".join(parts)


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


def _number_set(env_name: str) -> set[str]:
    return {d for d in (_digits(x) for x in os.getenv(env_name, "").split(",")) if d}


GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
SQLITE_PATH = os.getenv("SQLITE_PATH", "session.db").strip()
PORT = _env_int("PORT", 10000)  # Render injects PORT
PAGE_KEY = os.getenv("PAGE_KEY", "").strip()  # optional: hides the QR unless ?key=...

# Who the bot answers. Groups are never answered, in any mode.
REPLY_MODE = os.getenv("REPLY_MODE", "whitelist").strip().lower()  # "whitelist" | "all"
# Phone numbers in international format, digits only, no "+": 256701234567
WHITELIST: set[str] = _number_set("WHITELIST")  # used when REPLY_MODE=whitelist
BLOCKLIST: set[str] = _number_set("BLOCKLIST")  # never answered, in any mode
ONLY_KNOWN_CONTACTS = _env_bool("ONLY_KNOWN_CONTACTS", False)  # all-mode: only people in knowledge/contacts.json

MIN_DELAY = _env_float("MIN_DELAY_SECONDS", 5.0)
MAX_DELAY = _env_float("MAX_DELAY_SECONDS", 15.0)
if MIN_DELAY > MAX_DELAY:
    MIN_DELAY, MAX_DELAY = MAX_DELAY, MIN_DELAY

MAX_HISTORY = _env_int("MAX_HISTORY_MESSAGES", 12)  # turns remembered per contact
MAX_REPLIES_PER_HOUR = _env_int("MAX_REPLIES_PER_HOUR", 30)  # per contact, loop guard
HANDOFF_MINUTES = _env_int("HANDOFF_MINUTES", 60)  # pause a chat this long after YOU reply by hand
MAX_MESSAGE_AGE = _env_int("MAX_MESSAGE_AGE_SECONDS", 900)  # ignore older (e.g. delivered after a restart)
NOTIFY_NUMBER = _digits(os.getenv("NOTIFY_NUMBER", ""))  # your own number: get a note when the bot skips
APOLOGY_MODE = os.getenv("APOLOGY_MODE", "light").strip().lower()  # off | light | full (see "Apologies" above)
APOLOGY_NOTIFY = _env_bool("APOLOGY_NOTIFY", True)  # tell NOTIFY_NUMBER whenever the bot says sorry for you
TIMEZONE = os.getenv("TIMEZONE", "Africa/Kampala").strip()
SPLIT_MESSAGES = _env_bool("SPLIT_MESSAGES", True)  # one WhatsApp bubble per line
MAX_BUBBLES = _env_int("MAX_BUBBLES", 3)
GEMINI_MAX_RETRIES = _env_int("GEMINI_MAX_RETRIES", 5)
GEMINI_MAX_WAIT = _env_float("GEMINI_MAX_WAIT_SECONDS", 75.0)  # longest single back-off
QR_STALE_SECONDS = _env_int("QR_STALE_SECONDS", 180)  # unlinked: refresh QR after this
RECONNECT_GRACE_SECONDS = _env_int("RECONNECT_GRACE_SECONDS", 300)  # linked but offline
SESSION_NAME = os.getenv("SESSION_NAME", "clone-bot").strip()
KNOWLEDGE_DIR = Path(os.getenv("KNOWLEDGE_DIR") or Path(__file__).resolve().parent / "knowledge")
SECRETS_DIR = Path(os.getenv("SECRETS_DIR", "/etc/secrets"))  # where Render mounts Secret Files


def validate_config() -> None:
    problems = []
    if not GEMINI_API_KEY:
        problems.append("GEMINI_API_KEY is not set")
    if REPLY_MODE not in ("whitelist", "all"):
        problems.append(f"REPLY_MODE must be 'whitelist' or 'all' (got {REPLY_MODE!r})")
    if APOLOGY_MODE not in APOLOGY_RULES:
        problems.append(f"APOLOGY_MODE must be 'off', 'light' or 'full' (got {APOLOGY_MODE!r})")
    if APOLOGY_MODE != "off" and APOLOGY_NOTIFY and not NOTIFY_NUMBER:
        log.warning(
            "APOLOGY_MODE=%s but NOTIFY_NUMBER is empty: the bot will say sorry for you without telling you. "
            "Set NOTIFY_NUMBER to your own number to get a note each time.",
            APOLOGY_MODE,
        )
    if REPLY_MODE == "whitelist" and not WHITELIST:
        problems.append(
            "WHITELIST is empty - the bot will not reply to anyone "
            "(example: WHITELIST=256701234567,256772345678, or set REPLY_MODE=all)"
        )
    if REPLY_MODE == "all":
        log.warning(
            "REPLY_MODE=all: the bot answers EVERY 1:1 chat except BLOCKLIST (%d blocked). "
            "Groups are still ignored.",
            len(BLOCKLIST),
        )
    for number in WHITELIST | BLOCKLIST:
        if number.startswith("0"):
            log.warning(
                "Number %r starts with 0. Use the international format "
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
    if not GEMINI_API_KEY or REPLY_MODE not in ("whitelist", "all") or APOLOGY_MODE not in APOLOGY_RULES:
        raise SystemExit("Invalid configuration, see the CONFIG errors above")


# ============================================================================
# 2b. KNOWLEDGE PACK (built by build_knowledge.py from your exported chats)
# ============================================================================

PROFILE_TEXT = ""  # knowledge/profile.md  - facts about your life (editable by hand)
STYLE_TEXT = ""  # knowledge/style.md    - measured texting habits
CONTACTS: list[dict] = []  # knowledge/contacts.json
CONTACTS_BY_NUMBER: dict[str, dict] = {}
CONTACTS_BY_NAME: dict[str, dict] = {}


def _norm_name(name: str | None) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _find_knowledge_file(filename: str) -> Path | None:
    """knowledge/<file> in the repo/image first, then Render 'Secret Files' (/etc/secrets/<file>).
    Secret Files keep your private chat-derived data out of GitHub."""
    for folder in (KNOWLEDGE_DIR, SECRETS_DIR):
        candidate = folder / filename
        if candidate.is_file():
            return candidate
    return None


def _read_text(filename: str, limit: int) -> str:
    path = _find_knowledge_file(filename)
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()[:limit]
    except Exception:
        log.exception("Could not read %s", path)
        return ""


def _read_json(filename: str):
    path = _find_knowledge_file(filename)
    if path is None:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.exception("Could not read %s", path)
        return None


def _int_or_zero(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _merge_group(group: list[dict]) -> dict:
    """Merge contacts.json entries that share a phone number into one entry."""
    if len(group) == 1:
        return group[0]
    primary = max(group, key=lambda e: _int_or_zero(e.get("messages")))  # biggest chat; ties -> first
    others = [e for e in group if e is not primary]
    merged = dict(primary)

    # The other chats' names (and any aliases they had) become aliases of the merged entry.
    names = {_norm_name(primary["name"])}
    aliases: list[str] = []
    for entry in (primary, *others):
        candidates = [str(a) for a in entry.get("aliases") or []]
        if entry is not primary:
            candidates.insert(0, entry["name"])
        for name in candidates:
            if _norm_name(name) and _norm_name(name) not in names:
                names.add(_norm_name(name))
                aliases.append(name)
    if aliases:
        merged["aliases"] = aliases

    numbers: list = []
    seen_digits: set[str] = set()
    for entry in group:
        for number in entry.get("numbers") or []:
            if _digits(str(number)) not in seen_digits:
                seen_digits.add(_digits(str(number)))
                numbers.append(number)
    merged["numbers"] = numbers
    merged["messages"] = sum(_int_or_zero(e.get("messages")) for e in group)

    examples: list = []
    seen_pairs: set[tuple[str, str]] = set()
    for row in zip_longest(*[e.get("examples") or [] for e in (primary, *others)]):  # take turns, biggest chat first
        for pair in row:
            if isinstance(pair, (list, tuple)) and len(pair) == 2 and (str(pair[0]), str(pair[1])) not in seen_pairs:
                seen_pairs.add((str(pair[0]), str(pair[1])))
                examples.append(pair)
    merged["examples"] = examples  # "recent" stays the biggest chat's own

    notes: list[str] = []
    for entry in (primary, *others):
        note = str(entry.get("notes") or "").strip()
        if note and note not in notes:
            notes.append(note)
    merged["notes"] = " ".join(notes)
    merged["apology"] = next((e["apology"] for e in (primary, *others) if e.get("apology")), "")
    merged["flirt"] = next((e["flirt"] for e in (primary, *others) if e.get("flirt")), "")
    return merged


def _merge_same_number(entries: list[dict]) -> list[dict]:
    """Entries that share a phone number are one WhatsApp account (e.g. the same person exported
    twice under two saved names). Merge them so neither chat's context is silently lost."""
    groups: list[list[dict]] = []
    group_of: dict[str, list[dict]] = {}
    for entry in entries:
        digits = [d for d in (_digits(str(n)) for n in entry.get("numbers") or []) if d]
        group = next((group_of[d] for d in digits if d in group_of), None)
        if group is None:
            group = []
            groups.append(group)
        group.append(entry)
        for d in digits:
            group_of[d] = group
    result = []
    for group in groups:
        merged = _merge_group(group)
        if len(group) > 1:
            log.warning(
                "contacts.json: %s share a phone number, so they are treated as one person (merged as %r)",
                ", ".join(repr(e["name"]) for e in group), merged["name"],
            )
        result.append(merged)
    return result


def load_knowledge() -> None:
    global PROFILE_TEXT, STYLE_TEXT, FEW_SHOT_EXAMPLES
    PROFILE_TEXT = _read_text("profile.md", 12000)
    STYLE_TEXT = _read_text("style.md", 3000)

    FEW_SHOT_EXAMPLES = list(_DEFAULT_FEW_SHOT)
    examples = _read_json("examples.json")
    if isinstance(examples, list):
        cleaned = [
            (str(p[0]), str(p[1])) for p in examples if isinstance(p, (list, tuple)) and len(p) == 2
        ]
        if cleaned:
            FEW_SHOT_EXAMPLES = cleaned[:40]

    data = _read_json("contacts.json")
    entries = data.get("contacts", []) if isinstance(data, dict) else (data or [])
    entries = [e for e in entries if isinstance(e, dict) and e.get("name")]
    CONTACTS.clear()
    CONTACTS_BY_NUMBER.clear()
    CONTACTS_BY_NAME.clear()
    for entry in _merge_same_number(entries):
        CONTACTS.append(entry)
        CONTACTS_BY_NAME[_norm_name(entry["name"])] = entry
        for alias in entry.get("aliases") or []:  # other names the same person is saved under
            if _norm_name(str(alias)):
                CONTACTS_BY_NAME.setdefault(_norm_name(str(alias)), entry)
        if entry.get("apology") and str(entry["apology"]).strip().lower() not in APOLOGY_RULES:
            log.warning(
                "contacts.json: %r has apology=%r (use off, light or full); the bot will not apologise to them",
                entry["name"], entry["apology"],
            )
        flirt = entry.get("flirt")
        if flirt and flirt is not True and str(flirt).strip().lower() not in ("off", *FLIRT_RULES):
            log.warning(
                "contacts.json: %r has flirt=%r (use off, light or full); the bot will not flirt with them",
                entry["name"], flirt,
            )
        for number in entry.get("numbers") or []:
            if _digits(str(number)):
                CONTACTS_BY_NUMBER[_digits(str(number))] = entry
        as_number = _digits(entry["name"])
        if len(as_number) >= 7 and len(as_number) >= len(entry["name"]) - 4:  # unsaved contact: name is a number
            CONTACTS_BY_NUMBER.setdefault(as_number, entry)


def _find_contact(ids: set[str], pushname: str | None) -> dict | None:
    for ident in ids:
        if ident in CONTACTS_BY_NUMBER:
            return CONTACTS_BY_NUMBER[ident]
    return CONTACTS_BY_NAME.get(_norm_name(pushname)) if pushname else None


load_knowledge()


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


_thinking_rejected = False  # set if the API refuses our thinking setting; we then stop sending it


def _thinking_config() -> types.ThinkingConfig | None:
    """Casual texts do not need long reasoning, and thinking tokens eat the output budget.
    Gemini 2.5 can switch thinking off (thinking_budget=0). Gemini 3.x cannot: it has levels
    (thinking_level) instead, so we ask for the low one. Other models: Google's default."""
    if _thinking_rejected:
        return None
    model = GEMINI_MODEL.lower()
    try:
        if "gemini-2.5" in model:
            return types.ThinkingConfig(thinking_budget=0)
        if "gemini-3" in model:
            return types.ThinkingConfig(thinking_level="low")
    except Exception:  # an older google-genai without thinking_level
        log.warning("This google-genai version does not support the thinking setting for %s; using the default", model)
    return None


def _generation_config(contact: dict | None = None) -> types.GenerateContentConfig:
    thinking = _thinking_config()
    # With thinking OFF a short cap is fine. Otherwise the thinking tokens count against the cap too,
    # and 200 could leave nothing for the reply itself.
    max_tokens = 200 if thinking is not None and getattr(thinking, "thinking_budget", None) == 0 else 1024
    kwargs = {"thinking_config": thinking} if thinking is not None else {}
    return types.GenerateContentConfig(
        system_instruction=build_system_prompt(contact),
        temperature=0.9,
        max_output_tokens=max_tokens,
        **kwargs,
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


def generate_reply(
    history: list[tuple[str, str]], incoming: str, contact: dict | None = None
) -> str | None:
    """Ask Gemini for a reply. Returns None if it should stay silent, or SKIP_TOKEN
    if the model decided a human (you) should handle it."""
    global _thinking_rejected
    contents = build_contents(history, incoming)
    config = _generation_config(contact)

    for attempt in range(1, GEMINI_MAX_RETRIES + 1):
        try:
            response = get_gemini().models.generate_content(
                model=GEMINI_MODEL, contents=contents, config=config
            )
            text = (response.text or "").strip()
            if SKIP_TOKEN in text.upper():
                return SKIP_TOKEN
            text = re.sub(r"^\s*me\s*:\s*", "", text, flags=re.I)
            if not text:
                log.warning("Gemini returned no text (blocked or empty); staying silent")
                return None
            return text

        except genai_errors.APIError as err:
            code = getattr(err, "code", None)
            if code == 400 and "thinking" in str(err).lower() and not _thinking_rejected:
                # The model does not accept our thinking setting (e.g. a new Gemini version): drop it, try again.
                _thinking_rejected = True
                log.warning("Gemini rejected the thinking setting for %s; retrying without it", GEMINI_MODEL)
                config = _generation_config(contact)
                continue
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
        self.contact: dict | None = None
        self.display = ""  # name (if known) for logs / notifications
        self.paused_until = 0.0  # you replied by hand: stay quiet until then
        self.last_bot_send = 0.0
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


def _send_reply(wa: NewClient, chat: ChatState, text: str) -> None:
    parts = [p.strip() for p in text.splitlines() if p.strip()] if SPLIT_MESSAGES else [text]
    for part in parts[: max(1, MAX_BUBBLES)]:
        _set_typing(wa, chat.jid, True)
        time.sleep(min(len(part) * 0.07, 4.0) + random.uniform(0.4, 1.2))  # "typing" time
        wa.send_message(chat.jid, part)
        chat.last_bot_send = time.time()
    _set_typing(wa, chat.jid, False)


def _notify_owner(wa: NewClient, chat: ChatState, incoming: str) -> None:
    """Tell you (a WhatsApp message to your own number) that the bot stayed silent."""
    if not NOTIFY_NUMBER or build_jid is None:
        return
    try:
        snippet = incoming if len(incoming) <= 300 else incoming[:300] + "..."
        wa.send_message(build_jid(NOTIFY_NUMBER), f"needs you - {chat.display}:\n{snippet}")
    except Exception:
        log.debug("owner notification failed", exc_info=True)


def _notify_apology(wa: NewClient, chat: ChatState, incoming: str, reply: str) -> None:
    """Tell you (a WhatsApp message to your own number) that the bot said sorry in your name,
    so you can follow up yourself if it matters."""
    if not (APOLOGY_NOTIFY and NOTIFY_NUMBER and build_jid is not None):
        return
    if not _APOLOGY_RE.search(reply):
        return
    try:
        snippet = incoming if len(incoming) <= 300 else incoming[:300] + "..."
        wa.send_message(
            build_jid(NOTIFY_NUMBER),
            f"i said sorry for you - {chat.display}:\nthem: {snippet}\nme: {reply}",
        )
    except Exception:
        log.debug("apology notification failed", exc_info=True)


def _note_owner_activity(chat_id: str) -> None:
    """You typed in this chat yourself -> the bot stays out of it for a while."""
    if HANDOFF_MINUTES <= 0:
        return
    chat = _get_chat(chat_id)
    if time.time() - chat.last_bot_send < 20:
        return  # that was (an echo of) the bot's own message
    chat.paused_until = time.time() + HANDOFF_MINUTES * 60
    log.info("[%s] you replied yourself - bot paused here for %d min", chat.display or chat_id, HANDOFF_MINUTES)


def _process_chat(wa: NewClient, key: str) -> None:
    chat = _get_chat(key)
    finished = False
    try:
        while True:
            # Random human-like delay BEFORE replying. Messages that arrive during the
            # wait are merged into one reply instead of triggering one reply each.
            delay = random.uniform(MIN_DELAY, MAX_DELAY)
            log.info("[%s] waiting %.1fs before replying", chat.display or key, delay)
            time.sleep(delay)

            with chat.lock:
                if time.time() < chat.paused_until:  # you took over while we waited
                    chat.pending = []
                if not chat.pending:
                    chat.busy = False
                    finished = True
                    return
                batch, chat.pending = chat.pending, []
                allowed = _under_hourly_cap(chat)

            incoming = "\n".join(batch)
            if not allowed:
                log.warning("[%s] hourly reply cap (%d) reached - not replying", chat.display or key, MAX_REPLIES_PER_HOUR)
                continue

            reply = generate_reply(list(chat.history), incoming, chat.contact)
            if not reply:
                chat.history.append(("user", incoming))
                continue
            if reply == SKIP_TOKEN:
                log.info("[%s] model chose to stay silent (%s)", chat.display or key, SKIP_TOKEN)
                chat.history.append(("user", incoming))
                _notify_owner(wa, chat, incoming)
                continue

            _send_reply(wa, chat, reply)
            chat.history.append(("user", incoming))
            chat.history.append(("model", reply))
            chat.sent_at.append(time.time())
            log.info("[%s] reply sent (%d chars)", chat.display or key, len(reply))
            if _apology_mode(chat.contact) != "off" and _APOLOGY_RE.search(reply):
                log.info("[%s] said sorry for me (apology mode: %s)", chat.display or key, _apology_mode(chat.contact))
                _notify_apology(wa, chat, incoming, reply)
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


def _message_age_seconds(info) -> float | None:
    """Age of a message, or None if the timestamp is missing/unknown."""
    try:
        ts = float(getattr(info, "Timestamp", 0) or 0)
        if not ts:
            return None
        if ts > 1e11:  # milliseconds
            ts /= 1000.0
        return time.time() - ts
    except (TypeError, ValueError):
        return None


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
        info = event.Info
        source = info.MessageSource
        if source.IsGroup:
            return  # never answer groups
        if source.Chat.Server not in ("s.whatsapp.net", "lid"):
            return  # status updates, broadcast lists, channels
        chat_id = _digits(source.Chat.User)
        if not chat_id:
            return

        age = _message_age_seconds(info)
        if MAX_MESSAGE_AGE > 0 and age is not None and age > MAX_MESSAGE_AGE:
            log.info("Ignored stale message (%.0f min old)", age / 60)
            return

        text = _extract_text(event.Message)
        if source.IsFromMe:
            if text:
                _note_owner_activity(chat_id)  # human takeover
            return
        if not text:
            return  # images, stickers, voice notes, reactions...

        ids = _candidate_ids(source)
        if ids & BLOCKLIST:
            log.info("Ignored message from a blocklisted sender")
            return

        contact = _find_contact(ids, getattr(info, "Pushname", ""))
        if REPLY_MODE == "all":
            if ONLY_KNOWN_CONTACTS and contact is None:
                log.info("Ignored message from an unknown contact (ids seen: %s)", sorted(ids))
                return
        elif not (ids & WHITELIST):
            log.info("Ignored message from non-whitelisted sender (ids seen: %s)", sorted(ids))
            return

        chat = _get_chat(chat_id)
        with chat.lock:
            chat.contact = contact
            chat.display = contact["name"] if contact else chat_id
            paused = time.time() < chat.paused_until
        if paused:
            log.info("[%s] bot paused here because you replied yourself", chat.display)
            return

        log.info("[%s] incoming message (%d chars)", chat.display, len(text))
        _enqueue(wa, chat_id, source.Chat, text)
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
        "Starting: model=%s mode=%s whitelist=%d blocklist=%d delay=%.0f-%.0fs apologies=%s",
        GEMINI_MODEL, REPLY_MODE, len(WHITELIST), len(BLOCKLIST), MIN_DELAY, MAX_DELAY, APOLOGY_MODE,
    )
    log.info(
        "Knowledge pack (%s or %s): profile=%d chars, %d contact(s), %d example(s)%s",
        KNOWLEDGE_DIR, SECRETS_DIR, len(PROFILE_TEXT), len(CONTACTS), len(FEW_SHOT_EXAMPLES),
        "" if PROFILE_TEXT or CONTACTS else "  [empty: run build_knowledge.py to teach it your life]",
    )
    flirty = [c["name"] for c in CONTACTS if _flirt_mode(c) != "off"]
    if flirty:
        log.info("Flirting is switched on for %d contact(s)", len(flirty))
    threading.Thread(target=_run_whatsapp, name="whatsapp", daemon=True).start()
    threading.Thread(target=_watchdog, name="watchdog", daemon=True).start()

    from waitress import serve  # imported here so tests can import this module without it

    serve(app, host="0.0.0.0", port=PORT, threads=4)


if __name__ == "__main__":
    main()
