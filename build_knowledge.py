#!/usr/bin/env python3
"""
build_knowledge.py - teach the bot your life and your texting habits from exported chats.

HOW TO USE
  1. On your phone, export your chats WITHOUT media:
        WhatsApp > open a chat > ⋮ / contact name > Export chat > Without media
     (do this for the 1:1 chats that matter: family, close friends, work contacts...).
     Save the .txt (or .zip) files into a folder called  chats/  next to this script.
     Group exports are detected and skipped.
  2. Run:        python build_knowledge.py
     (needs GEMINI_API_KEY for the optional AI summaries; use --no-llm to skip them)
  3. Edit knowledge/profile.md to fix anything wrong, fill in the "numbers" of your
     contacts in knowledge/contacts.json, then commit the knowledge/ folder to a
     PRIVATE repo. main.py loads it automatically.

WHAT IT WRITES  (all in knowledge/)
  examples.json   real "they said -> I replied" pairs, picked locally (no AI involved)
  style.md        measured texting habits (length, capitals, punctuation, emoji, slang)
  contacts.json   per contact: notes on who they are + how you talk to them, recent messages,
                  examples. Fill in each person's "numbers" so the bot recognises them.
  profile.md      facts about your life (people, routine, places, plans...) written by Gemini

PRIVACY - READ THIS
  * The --no-llm part runs entirely on your computer.
  * The AI part sends (redacted: links, emails and long numbers are removed) excerpts of your
    chats to the Gemini API. On Gemini's FREE tier Google may use them to improve its products
    and humans may read them. Enable billing on the key's Google Cloud project to get the paid
    data terms, and think about whether your contacts would be OK with it.
  * knowledge/ contains other people's messages. Keep the repo PRIVATE. Never commit chats/.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import random
import re
import statistics
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# ----------------------------------------------------------------------------
# 1. Parsing WhatsApp exports (Android "12/03/2025, 14:05 - Name: text" and
#    iOS "[12/03/2025, 14:05:33] Name: text", 12h or 24h clocks)
# ----------------------------------------------------------------------------

_INVISIBLE = dict.fromkeys(map(ord, "‎‏‪‫‬‭‮﻿"), None)

HEADER = re.compile(
    r"^\[?(?P<d1>\d{1,4})[/.\-](?P<d2>\d{1,2})[/.\-](?P<d3>\d{1,4}),?\s+"
    r"(?P<h>\d{1,2})[:.](?P<m>\d{2})(?::(?P<s>\d{2}))?\s*(?P<ampm>[AaPp]\.?[Mm]\.?)?\]?\s*(?:-\s*)?(?P<rest>.*)$"
)
SENDER = re.compile(r"^(?P<name>[^:]{1,100}?):\s(?P<body>.*)$")

# Lines WhatsApp itself inserts (on iPhone exports they even carry the contact's name as "sender").
NOISE = re.compile(
    r"^(?:<?media omitted>?|<attached: .*>|.*\(file attached\)|"
    r"(?:image|video|audio|sticker|gif|document|contact card|voice message) omitted|"
    r"this message was deleted\.?|you deleted this message\.?|missed (?:voice|video) call.*|"
    r"(?:voice|video) call(?:,.*)?|null|location: .*|"
    r".*(?:updated the message timer|turned (?:on|off) disappearing messages|"
    r"changed (?:their|his|her) phone number|security code changed|"
    r"messages and calls are end-to-end encrypted|you (?:un)?blocked this contact|"
    r"waiting for this message|change timer).*)$",
    re.I,
)
EDITED = re.compile(r"\s*<this message was edited>\s*$", re.I)
NAME_PREFIX = re.compile(r"^(?:whatsapp chat(?: with| -|:)?\s*|chat (?:whatsapp )?(?:con|avec|mit)\s*)", re.I)


@dataclass
class Msg:
    when: datetime | None
    who: str  # sender name when parsed; "me" / "them" once resolved
    text: str


@dataclass
class Chat:
    name: str
    msgs: list[Msg]


def _clean_line(line: str) -> str:
    return line.translate(_INVISIBLE).replace(" ", " ").replace(" ", " ").rstrip("\r\n")


def _clean_text(text: str) -> str:
    text = EDITED.sub("", text).strip()
    return "" if NOISE.match(text) else text


def _date_order(rows: list[dict]) -> str:
    if any(len(r["d1"]) == 4 for r in rows):
        return "ymd"
    first_gt12 = any(int(r["d1"]) > 12 for r in rows)
    second_gt12 = any(int(r["d2"]) > 12 for r in rows)
    return "mdy" if second_gt12 and not first_gt12 else "dmy"


def _to_datetime(h: dict, order: str) -> datetime | None:
    try:
        a, b, c = int(h["d1"]), int(h["d2"]), int(h["d3"])
        if order == "ymd":
            year, month, day = a, b, c
        elif order == "mdy":
            month, day, year = a, b, c
        else:
            day, month, year = a, b, c
        if year < 100:
            year += 2000
        hour, minute, second = int(h["h"]), int(h["m"]), int(h["s"] or 0)
        ampm = (h["ampm"] or "").replace(".", "").lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
        return datetime(year, month, day, hour, minute, second)
    except (ValueError, TypeError):
        return None


def parse_chat(text: str) -> list[Msg]:
    rows: list[dict] = []
    current = None
    for raw in text.splitlines():
        line = _clean_line(raw)
        header = HEADER.match(line)
        if header:
            sender = SENDER.match(header["rest"])
            if sender:
                current = {**header.groupdict(), "who": sender["name"].strip(), "text": sender["body"]}
                rows.append(current)
            else:
                current = None  # system line ("Messages are end-to-end encrypted...")
        elif current is not None:
            current["text"] += "\n" + line
    if not rows:
        return []
    order = _date_order(rows)
    out = []
    for row in rows:
        body = _clean_text(row["text"])
        if body:
            out.append(Msg(_to_datetime(row, order), row["who"], body))
    return out


def _name_from_path(path: Path) -> str:
    stem = path.stem
    if stem.lower() in ("_chat", "chat"):
        stem = path.parent.name
    return NAME_PREFIX.sub("", stem).strip() or stem


def _read_export(path: Path) -> str | None:
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            names = sorted((n for n in archive.namelist() if n.lower().endswith(".txt")), key=len)
            return archive.read(names[0]).decode("utf-8-sig", "replace") if names else None
    return path.read_bytes().decode("utf-8-sig", "replace")


def load_chats(root: Path, me_arg: str | None) -> tuple[list[Chat], set[str]]:
    parsed: list[tuple[str, list[Msg]]] = []
    skipped_groups = 0
    for path in sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in {".txt", ".zip"}):
        try:
            text = _read_export(path)
        except Exception as exc:
            print(f"  ! could not read {path.name}: {exc}")
            continue
        msgs = parse_chat(text or "")
        if not msgs:
            print(f"  ! no messages found in {path.name} (unsupported format?) - skipped")
            continue
        if len({m.who for m in msgs}) > 2:
            skipped_groups += 1
            continue
        parsed.append((_name_from_path(path), msgs))
    if skipped_groups:
        print(f"  skipped {skipped_groups} group chat export(s)")

    if me_arg:
        me_names = {n.strip().lower() for n in me_arg.split(",") if n.strip()}
    else:
        counter: collections.Counter = collections.Counter()
        for _, msgs in parsed:
            counter.update({m.who for m in msgs})
        top = counter.most_common(2)
        if len(parsed) >= 2 and top and (len(top) == 1 or top[0][1] > top[1][1]):
            me_names = {top[0][0].lower()}
            print(f"  detected your name in the exports as: {top[0][0]!r} (override with --me)")
        else:
            names = ", ".join(sorted({m.who for _, msgs in parsed for m in msgs})[:12])
            raise SystemExit(f"Could not tell which sender is you. Re-run with --me \"Your Name\". Names seen: {names}")

    chats: dict[str, Chat] = {}
    seen: dict[str, set] = {}
    for file_name, msgs in parsed:
        others = [m.who for m in msgs if m.who.lower() not in me_names]
        name = collections.Counter(others).most_common(1)[0][0] if others else file_name
        chat = chats.setdefault(name, Chat(name, []))
        keys = seen.setdefault(name, set())
        for m in msgs:
            key = (m.when, m.who, m.text)
            if key in keys:
                continue
            keys.add(key)
            chat.msgs.append(Msg(m.when, "me" if m.who.lower() in me_names else "them", m.text))
    for chat in chats.values():
        chat.msgs.sort(key=lambda m: m.when or datetime.min)
    return sorted(chats.values(), key=lambda c: -len(c.msgs)), me_names


# ----------------------------------------------------------------------------
# 2. Local analysis (no AI): style stats, example pairs, recent messages
# ----------------------------------------------------------------------------

_URL = re.compile(r"https?://\S+|www\.\S+", re.I)
_EMAIL = re.compile(r"\S+@\S+\.\S+")
_LONG_DIGITS = re.compile(r"\d[\d\s\-]{5,}\d")
_SENSITIVE_WORDS = re.compile(
    r"\b(?:otp|password|passcode|pin|cvv|momo|mobile money|airtime|loan|account (?:number|no)|"
    r"send (?:me )?money|money|cash|ugx|shillings?|mtn|airtel|bank|salary|pay(?:ment)?|invoice|"
    # jokes or talk about crime: harmless between friends, an embarrassing blunder if the bot repeats them
    r"scam(?:s|ming)?|illegal|dark ?web|vpn|steal(?:ing)?|hack(?:ing|ed)?|confidential)\b",
    re.I,
)
_MONEY = re.compile(r"\b\d+(?:[.,]\d+)?\s?k\b", re.I)  # "50k", "2.5k"
# Never used as GLOBAL examples (those are shown in every chat): affectionate or health-related lines.
_INTIMATE = re.compile(
    r"\b(?:love|luv|boo|babe|baby|honey|sweetie|sweetheart|darling|dear|kiss|cuddle|miss you|mob)\b", re.I
)
_HEALTH = re.compile(
    r"\b(?:chest|sick|hospital|clinic|doctor|pain|malaria|medicine|pills|check-?up|headache|fever)\b", re.I
)
_MID_CAPS = re.compile(r"(?<=\s)[A-Z][a-z]{2,}")  # capitalised word mid-sentence: likely a name
_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿\U0001F000-\U0001F2FF]")
_STOP = set(
    "the a an and or but i you he she it we they me my your to of in on at for is are was were be been am do did "
    "does not no yes ok okay so if then that this with have has had will would can just like what when where how "
    "why who u ur im it's its dont don't i'm".split()
)


def redact(text: str) -> str:
    text = _URL.sub("[link]", text)
    text = _EMAIL.sub("[email]", text)
    return re.sub(r"\d{6,}", "[number]", text)


def is_sensitive(text: str) -> bool:
    return bool(
        _URL.search(text) or _EMAIL.search(text) or _LONG_DIGITS.search(text)
        or _SENSITIVE_WORDS.search(text) or _MONEY.search(text)
    )


def _ranges(hours: list[int]) -> str:
    hours = sorted(hours)
    out, start, prev = [], None, None
    for h in hours:
        if start is None:
            start = prev = h
        elif h == prev + 1:
            prev = h
        else:
            out.append((start, prev))
            start = prev = h
    if start is not None:
        out.append((start, prev))
    return ", ".join(f"{a:02d}:00-{b + 1:02d}:00" for a, b in out)


def style_markdown(my_msgs: list[Msg]) -> str:
    texts = [m.text for m in my_msgs]
    n = len(texts)
    if n < 20:
        return ""
    words = sorted(len(t.split()) for t in texts)
    no_caps = sum(t == t.lower() for t in texts) / n
    no_end_punct = sum(not re.search(r"[.!?]\s*$", t) for t in texts) / n
    lines = [
        f"- Typical message length: median {statistics.median(words):.0f} words (90% are {words[int(0.9 * (n - 1))]} words or fewer).",
        f"- {no_caps:.0%} of my messages contain no capital letters.",
        f"- {no_end_punct:.0%} of my messages do not end with punctuation.",
    ]
    emoji_share = sum(bool(_EMOJI.search(t)) for t in texts) / n
    top_emoji = collections.Counter(e for t in texts for e in _EMOJI.findall(t)).most_common(5)
    lines.append(
        f"- {emoji_share:.0%} of my messages contain an emoji"
        + (f"; favourites: {' '.join(e for e, _ in top_emoji)}" if top_emoji else "")
        + "."
    )
    word_counts = collections.Counter(
        w for t in texts for w in re.findall(r"[a-z']{2,}", t.lower()) if w not in _STOP
    )
    if word_counts:
        lines.append("- Words/slang I use a lot: " + ", ".join(w for w, _ in word_counts.most_common(30)) + ".")
    hours = collections.Counter(m.when.hour for m in my_msgs if m.when)
    if sum(hours.values()) >= 50:
        total = sum(hours.values())
        quiet = [h for h in range(24) if hours[h] / total < 0.01]
        busy = [h for h, _ in hours.most_common(4)]
        lines.append(f"- I text most around: {', '.join(f'{h:02d}:00' for h in sorted(busy))}.")
        if quiet:
            lines.append(f"- I almost never text during: {_ranges(quiet)} (asleep / away).")
    return "\n".join(lines)


def _pair_ok(them: str, me: str) -> bool:
    return (
        2 <= len(them) <= 160
        and 1 <= len(me) <= 160
        and not is_sensitive(them)
        and not is_sensitive(me)
    )


def build_pairs(chat: Chat, max_gap_minutes: int = 30) -> list[dict]:
    """(what they said, what I replied) pairs taken from real conversation."""
    msgs, pairs, i = chat.msgs, [], 0
    while i < len(msgs):
        if msgs[i].who != "them":
            i += 1
            continue
        j = i
        while j < len(msgs) and msgs[j].who == "them":
            j += 1
        k = j
        while k < len(msgs) and msgs[k].who == "me":
            k += 1
        them, mine = msgs[i:j][-3:], msgs[j:k][:3]
        if mine:
            gap_ok = True
            if them[-1].when and mine[0].when:
                gap_ok = (mine[0].when - them[-1].when).total_seconds() <= max_gap_minutes * 60
            them_text = "\n".join(m.text for m in them)
            me_text = "\n".join(m.text for m in mine)
            if gap_ok and _pair_ok(them_text, me_text):
                pairs.append({"chat": chat.name, "when": mine[0].when or datetime.min, "them": them_text, "me": me_text})
        i = max(k, j)
    return pairs


def pick_global_examples(all_pairs: list[dict], n: int, seed: int, per_contact: int = 4) -> list[list[str]]:
    """Short, name-free pairs from many different contacts (kept generic on purpose)."""
    rng = random.Random(seed)
    def generic(p: dict) -> bool:
        both = p["them"] + "\n" + p["me"]
        return not (_MID_CAPS.search(both) or _INTIMATE.search(both) or _HEALTH.search(both))

    pool = [p for p in all_pairs if len(p["them"]) + len(p["me"]) <= 110 and generic(p)]
    pool.sort(key=lambda p: p["when"], reverse=True)
    pool = pool[: max(n * 8, 200)]
    rng.shuffle(pool)
    out, counts = [], collections.Counter()
    for p in pool:
        if counts[p["chat"]] >= per_contact:
            continue
        counts[p["chat"]] += 1
        out.append([p["them"], p["me"]])
        if len(out) >= n:
            break
    return out


def recent_messages(chat: Chat, n: int) -> list[list[str]]:
    safe = [m for m in chat.msgs if not is_sensitive(m.text)]
    return [[m.who, m.text[:200]] for m in safe[-n:]] if n > 0 else []


# ----------------------------------------------------------------------------
# 3. Optional AI summaries (Gemini)
# ----------------------------------------------------------------------------

_RETRY_HINT = re.compile(r"retry(?:Delay)?\W{1,6}(?:in\W+)?(\d+(?:\.\d+)?)\s*s", re.I)

MAP_SYSTEM = (
    "You extract durable background facts about ONE person (called 'me') from excerpts of their private "
    "WhatsApp chats, so an assistant that texts on their behalf never makes mistakes about their life."
)
MAP_TASK = """Excerpts of my chats follow ("me" is me; other names are the people I chat with).
Extract facts about MY life that help me reply correctly. Use these headings (skip empty ones):
## People (who each person is to me: family, friends, classmates, colleagues, partner...)
## Daily routine & schedule
## Places (where I live, study/work, usual hangouts)
## Work, studies & projects
## Interests & preferences
## Ongoing or upcoming plans (with dates)
## Running jokes, nicknames, how I talk to different people
Rules: one short bullet per fact; only what the text supports; note dates for time-sensitive facts;
NEVER include passwords, codes, financial figures, health or medical details, romantic/sexual details, or
anything private about third parties that I would not want repeated to others. Do not quote long messages."""

REDUCE_SYSTEM = "You merge notes into a clean, accurate background profile of one person."
REDUCE_TASK = """Merge these batches of notes (all about me) into ONE profile in markdown, at most ~900 words.
Keep the same headings. Remove duplicates, prefer newer information when facts conflict, and drop anything
uncertain or sensitive (passwords, money, health, romance, other people's private matters).
Start with one line: "Profile generated on {today}; facts may be out of date."
Output only the profile."""

CONTACT_SYSTEM = "You describe one relationship from a chat excerpt, briefly and factually."
CONTACT_TASK = """Below are my recent messages with "{name}" ("me" is me). In at most 70 words, say:
who they are to me (only if clear from the text), how I talk to them (language, tone, nicknames, emoji),
their usual topics, and any subject I should avoid or be careful with. No quotes, no private details
(health, money, romance). Output plain text only."""


class Gemini:
    def __init__(self, api_key: str, model: str, pause: float) -> None:
        from google import genai  # imported lazily so --no-llm needs no SDK
        from google.genai import errors, types

        self.client = genai.Client(api_key=api_key)
        self.errors, self.types = errors, types
        self.model, self.pause = model, pause

    def ask(self, system: str, prompt: str, max_tokens: int = 2500) -> str:
        for attempt in range(1, 7):
            try:
                response = self.client.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config=self.types.GenerateContentConfig(
                        system_instruction=system,
                        temperature=0.2,
                        max_output_tokens=max_tokens,
                        thinking_config=self.types.ThinkingConfig(thinking_budget=0),
                    ),
                )
                time.sleep(self.pause)  # stay under the free tier's requests-per-minute limit
                return (response.text or "").strip()
            except self.errors.APIError as err:
                code = getattr(err, "code", None)
                if (code == 429 or (isinstance(code, int) and code >= 500)) and attempt < 6:
                    hint = _RETRY_HINT.search(f"{getattr(err, 'message', '')} {getattr(err, 'details', '')} {err}")
                    wait = float(hint.group(1)) + 2 if hint else min(5 * 2 ** attempt, 90)
                    if wait > 300:
                        raise SystemExit("Gemini quota exhausted for today. Re-run tomorrow, or enable billing.") from err
                    print(f"    rate limited ({code}); waiting {wait:.0f}s ...")
                    time.sleep(wait)
                    continue
                raise
        return ""


def render_chat(chat: Chat, last_n: int, per_msg_chars: int = 280) -> list[str]:
    lines = [f"=== chat with {chat.name} ==="]
    for m in chat.msgs[-last_n:]:
        stamp = m.when.strftime("%Y-%m-%d") if m.when else "????-??-??"
        who = "me" if m.who == "me" else chat.name
        lines.append(f"[{stamp}] {who}: {redact(m.text).replace(chr(10), ' / ')[:per_msg_chars]}")
    return lines


def make_chunks(blocks: list[list[str]], chunk_chars: int, max_chars: int) -> list[str]:
    chunks, current, size, total = [], [], 0, 0
    for lines in blocks:
        header = lines[0]
        for i, line in enumerate(lines):
            if total + len(line) > max_chars:
                if current:
                    chunks.append("\n".join(current))
                return chunks
            if current and size + len(line) + 1 > chunk_chars:
                chunks.append("\n".join(current))
                current, size = [], 0
                if i > 0:  # continuing a chat in a new chunk: repeat its header
                    current.append(header)
                    size += len(header) + 1
            current.append(line)
            size += len(line) + 1
            total += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


# ----------------------------------------------------------------------------
# 4. Main
# ----------------------------------------------------------------------------


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the bot's knowledge pack from exported WhatsApp chats.")
    ap.add_argument("--chats", default="chats", help="folder with exported .txt/.zip chats (default: chats)")
    ap.add_argument("--out", default="knowledge", help="output folder (default: knowledge)")
    ap.add_argument("--me", help='your name as it appears in the exports (comma-separate several). Auto-detected if omitted')
    ap.add_argument("--no-llm", action="store_true", help="skip the Gemini summaries; everything stays on your computer")
    ap.add_argument("--yes", action="store_true", help="do not ask for confirmation before sending excerpts to Gemini")
    ap.add_argument("--model", default=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))
    ap.add_argument("--api-key", default=os.getenv("GEMINI_API_KEY", ""))
    ap.add_argument("--max-chars", type=int, default=1_000_000, help="max characters of chat sent to Gemini in total")
    ap.add_argument("--chunk-chars", type=int, default=100_000, help="characters per Gemini request")
    ap.add_argument("--per-contact-msgs", type=int, default=500, help="most recent messages per contact used for the profile")
    ap.add_argument("--max-contacts", type=int, default=40, help="how many top contacts get an AI-written note")
    ap.add_argument("--min-messages", type=int, default=30, help="ignore contacts with fewer messages")
    ap.add_argument("--recent", type=int, default=20, help="recent messages per contact stored for context (0 = none)")
    ap.add_argument("--examples", type=int, default=30, help="number of global few-shot examples")
    ap.add_argument("--sleep", type=float, default=6.5, help="seconds to pause between Gemini calls (free tier: 10 requests/min)")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)

    root, out = Path(args.chats), Path(args.out)
    if not root.is_dir() or (not any(root.rglob("*.txt")) and not any(root.rglob("*.zip"))):
        print(f"No exported chats found in {root}/ .\nExport chats from WhatsApp (chat > Export chat > Without media) "
              "and put the .txt/.zip files there.")
        return 1

    print(f"Reading chats from {root}/ ...")
    chats, me_names = load_chats(root, args.me)
    chats = [c for c in chats if len(c.msgs) >= args.min_messages]
    if not chats:
        print(f"No 1:1 chat has at least {args.min_messages} messages (see --min-messages).")
        return 1
    my_msgs = [m for c in chats for m in c.msgs if m.who == "me"]
    print(f"  {len(chats)} one-to-one chats, {sum(len(c.msgs) for c in chats)} messages ({len(my_msgs)} from you)")
    out.mkdir(parents=True, exist_ok=True)

    # --- local, private part -------------------------------------------------
    all_pairs, per_chat_pairs = [], {}
    for chat in chats:
        pairs = build_pairs(chat)
        per_chat_pairs[chat.name] = pairs
        all_pairs.extend(pairs)
    examples = pick_global_examples(all_pairs, args.examples, args.seed)
    _write_json(out / "examples.json", examples)
    style = style_markdown(my_msgs)
    if style:
        (out / "style.md").write_text(style + "\n", encoding="utf-8")
    print(f"  wrote examples.json ({len(examples)} pairs)" + (" and style.md" if style else ""))

    # --- optional AI part ----------------------------------------------------
    profile, notes = "", {}
    if not args.no_llm:
        if not args.api_key:
            print("GEMINI_API_KEY is not set. Set it, or use --no-llm.")
            return 1
        blocks = [render_chat(c, args.per_contact_msgs) for c in chats]
        chunks = make_chunks(blocks, args.chunk_chars, args.max_chars)
        note_targets = chats[: args.max_contacts]
        calls = len(chunks) + 1 + len(note_targets)
        sent = sum(len(c) for c in chunks) + sum(min(len(c.msgs), 300) * 90 for c in note_targets)
        print(f"\nAI step: about {calls} Gemini requests, sending roughly {sent:,} characters of your chats\n"
              "(links, emails and long numbers removed). On Gemini's free tier Google may use this data to improve its\n"
              "products and humans may read it; enable billing on the key's project for the paid data terms.")
        if not args.yes:
            if not sys.stdin.isatty():
                print("Not an interactive terminal: pass --yes to confirm, or --no-llm.")
                return 1
            if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
                print("Skipping the AI step (examples.json and style.md were still written).")
                chunks, note_targets = [], []
        if chunks:
            gem = Gemini(args.api_key, args.model, args.sleep)
            batches = []
            for i, chunk in enumerate(chunks, 1):
                print(f"  extracting facts {i}/{len(chunks)} ...")
                batches.append(gem.ask(MAP_SYSTEM, f"{MAP_TASK}\n\n---\n{chunk}"))
            merged = "\n\n".join(f"### Batch {i}\n{b}" for i, b in enumerate(batches, 1))
            print("  merging into profile ...")
            profile = gem.ask(
                REDUCE_SYSTEM,
                REDUCE_TASK.format(today=datetime.now().strftime("%d %B %Y")) + "\n\n---\n" + merged,
                max_tokens=3500,
            )
            for i, chat in enumerate(note_targets, 1):
                print(f"  note for {chat.name} ({i}/{len(note_targets)}) ...")
                text = "\n".join(render_chat(chat, 300, 220))
                notes[chat.name] = gem.ask(
                    CONTACT_SYSTEM, CONTACT_TASK.format(name=chat.name) + "\n\n---\n" + text, max_tokens=300
                )[:700]

    if profile:
        target = out / "profile.md"
        if target.exists():
            target.replace(out / "profile.md.bak")
            print("  existing profile.md saved as profile.md.bak")
        target.write_text(profile + "\n", encoding="utf-8")
        print("  wrote profile.md  <- read it and fix anything wrong")

    # --- contacts.json (keeps numbers/notes from earlier runs) ----------------
    previous = _load_json(out / "contacts.json")
    old = {c["name"]: c for c in (previous.get("contacts", []) if isinstance(previous, dict) else []) if isinstance(c, dict) and "name" in c}
    entries = []
    for chat in chats:
        prev = old.get(chat.name, {})
        pairs = sorted(per_chat_pairs[chat.name], key=lambda p: p["when"], reverse=True)[:12][::-1]
        entry = {
            "name": chat.name,
            "numbers": prev.get("numbers", []),
            "messages": len(chat.msgs),
            "notes": notes.get(chat.name) or prev.get("notes", ""),
            "examples": [[p["them"], p["me"]] for p in pairs],
            "recent": recent_messages(chat, args.recent),
        }
        for kept in ("apology", "flirt", "aliases"):  # settings you add by hand survive a re-run
            if prev.get(kept):
                entry[kept] = prev[kept]
        entries.append(entry)
    _write_json(out / "contacts.json", {"contacts": entries})
    missing = sum(1 for e in entries if not e["numbers"] and len(re.sub(r"\D", "", e["name"])) < 7)
    print(f"  wrote contacts.json ({len(entries)} contacts; {missing} still need a phone number in \"numbers\")")

    print("\nDone. Next steps:\n"
          f"  1. Read {out}/profile.md and correct it.\n"
          f"  2. Open {out}/contacts.json and add each person's number(s), digits only, e.g. \"numbers\": [\"256701234567\"].\n"
          f"  3. Commit {out}/ to a PRIVATE repo (never commit {root}/) and redeploy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
