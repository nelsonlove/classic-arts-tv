#!/usr/bin/env python3
"""Watch the Classic Arts Showcase live stream, read the credit card that opens and closes each clip, and serve what is playing.

Nothing here is specific to one machine: every setting is an environment variable, listed in CONFIG below.
"""

import base64
import io
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from PIL import Image

try:
    import anthropic
except ImportError:  # the correction step is optional
    anthropic = None

CONFIG = {
    "CAS_STREAM_URL": os.environ.get("CAS_STREAM_URL", "https://classicarts.akamaized.net/hls/live/1024257/CAS/master_360p30.m3u8"),
    "CAS_INTERVAL": float(os.environ.get("CAS_INTERVAL", "5")),      # seconds between sampled frames
    "CAS_WIDTH": int(os.environ.get("CAS_WIDTH", "640")),            # frame width handed to OCR
    "CAS_DATA": Path(os.environ.get("CAS_DATA", "/var/lib/cas-watcher")),
    "CAS_PORT": int(os.environ.get("CAS_PORT", "8080")),
    "CAS_BIND": os.environ.get("CAS_BIND", "0.0.0.0"),
    "CAS_PATH_PREFIX": os.environ.get("CAS_PATH_PREFIX", "").rstrip("/"),  # e.g. /classicarts when mounted under a path
    "CAS_NOW_WINDOW": int(os.environ.get("CAS_NOW_WINDOW", "900")),  # seconds a card still counts as "now playing"
    "CAS_MIN_LINES": int(os.environ.get("CAS_MIN_LINES", "3")),      # wordy lines that make a frame a credit card
    "CAS_MIN_FRAMES": int(os.environ.get("CAS_MIN_FRAMES", "2")),    # frames a card must persist before it is recorded
    # Optional correction of the OCR by a Claude model that also sees the frame. Off when CAS_LLM_URL is empty.
    "CAS_LLM_URL": os.environ.get("CAS_LLM_URL", ""),                # Anthropic-format endpoint base, e.g. https://host/cliproxyapi
    "CAS_LLM_KEY_FILE": os.environ.get("CAS_LLM_KEY_FILE", ""),      # file holding the API key or gateway client key
    "CAS_LLM_MODEL": os.environ.get("CAS_LLM_MODEL", "claude-haiku-5-5"),
    "CAS_LLM_EFFORT": os.environ.get("CAS_LLM_EFFORT", "low"),
    "CAS_REPO_URL": os.environ.get("CAS_REPO_URL", "https://github.com/nelsonlove/classic-arts-tv"),
}

DB_PATH = CONFIG["CAS_DATA"] / "cards.sqlite"
THUMBS = CONFIG["CAS_DATA"] / "thumbs"

# The channel's own notice cards (who pays for it, how to write to it), not clips. Matched on the channel's own wording only.
NOTICE = re.compile(r"brought to you by|lloyd e\.? ?rigler|classic arts showcase is|welcome your comments|casmail|classicartsshowcase\.org|public service channel|patron of the arts", re.I)


def card_kind(lines: list[str]) -> str:
    return "notice" if NOTICE.search(" ".join(lines)) else "clip"


# Words that mark the layout of a credit card, not a title in the picture.
CARD_HINTS = re.compile(r"\b(music by|conductor|orchestra|choreograph|performed|directed|produced|libretto|soprano|tenor|baritone|pianist|violin|ballet|opera|symphony|quartet|philharmonic|ltd|inc|records|films?)\b", re.I)


def no_proxy_env() -> dict:
    """The stream must not go through a proxy set for the model endpoint (HTTPS_PROXY on a tailnet-joined container)."""
    return {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}


def log(*parts):
    print(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), *parts, flush=True)


# ---------- frames ----------

def crop_to_picture(image: Image.Image, threshold: int = 24) -> Image.Image:
    """Cut the black bars off: the channel's picture sits inside the 16:9 frame with borders that vary per clip."""
    box = image.convert("L").point(lambda v: 255 if v > threshold else 0).getbbox()
    if not box:
        return image.copy()
    left, top, right, bottom = box
    w, h = image.size
    if (right - left) < w * 0.3 or (bottom - top) < h * 0.3:
        return image.copy()  # a mostly dark frame: keep it whole
    pad = 2
    return image.crop((max(0, left - pad), max(0, top - pad), min(w, right + pad), min(h, bottom + pad)))


# ---------- OCR ----------

def ocr(image: Image.Image) -> list[str]:
    """Run tesseract on a frame and return its non-empty lines."""
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    proc = subprocess.run(
        ["tesseract", "stdin", "stdout", "--psm", "11", "-l", "eng"],
        input=buf.getvalue(), capture_output=True, timeout=30,
    )
    text = proc.stdout.decode("utf-8", "replace")
    return [line.strip() for line in text.splitlines() if line.strip()]


WORD = re.compile(r"[A-Za-z]{3,}")


def wordy(line: str) -> bool:
    """True for a line that reads like a credit, not like OCR noise from the picture."""
    if len(line) < 8:
        return False
    letters = sum(ch.isalpha() for ch in line)
    allowed = sum(ch.isalnum() or ch in " .,'\"()-/:;&’‘“”" for ch in line)
    if allowed / len(line) < 0.85 or letters / len(line) < 0.5:
        return False
    if len(WORD.findall(line)) < 2:
        return False
    if line.strip().upper() in {"ARTS", "CLASSIC ARTS SHOWCASE"}:
        return False
    return True


# The channel's category label, shown small in the top-left corner at the start of a clip: "recital", "ballet", "music video".
CATEGORIES = {
    "music video", "classic film", "ballet", "soloist/orchestral", "solo instrumental", "solo vocal", "recital", "film short",
    "opera", "orchestral", "musical short", "musical theater", "musical theatre", "animation", "choral", "documentary", "dance",
    "modern dance", "chamber music", "jazz", "operetta", "concert", "visual arts", "architecture", "interview", "classic", "film",
}
CATEGORY_SHAPE = re.compile(r"^[a-z][a-z /&-]{2,27}$")


def split_category(lines: list[str]) -> tuple[str | None, list[str]]:
    """Take the category label off the front of a card's lines, if the first line is one."""
    if not lines:
        return None, lines
    first = lines[0].strip().rstrip(":").strip()
    if first in CATEGORIES or (CATEGORY_SHAPE.match(first) and len(first.split()) <= 3 and first.lower() == first):
        return first, lines[1:]
    return None, lines


def card_lines(lines: list[str]) -> list[str]:
    kept = [ln for ln in lines if wordy(ln)]
    if len(kept) < CONFIG["CAS_MIN_LINES"]:
        return []
    return kept


def similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


# ---------- storage ----------

def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS cards (
        id INTEGER PRIMARY KEY,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        frames INTEGER NOT NULL,
        text TEXT NOT NULL,
        lines TEXT NOT NULL
    )""")
    con.execute("CREATE INDEX IF NOT EXISTS cards_last_seen ON cards(last_seen)")
    for column in ("clean TEXT", "kind TEXT NOT NULL DEFAULT 'clip'", "category TEXT"):
        try:
            con.execute(f"ALTER TABLE cards ADD COLUMN {column}")
        except sqlite3.OperationalError:
            pass  # column already there
    con.execute("CREATE INDEX IF NOT EXISTS cards_kind ON cards(kind, id)")
    con.commit()
    return con


def row_to_dict(r: sqlite3.Row) -> dict:
    clean = json.loads(r["clean"]) if r["clean"] else None
    ocr_category, lines = split_category(json.loads(r["lines"]))
    clean_category, clean_lines = split_category((clean or {}).get("lines") or [])
    category = r["category"] or (clean or {}).get("category") or clean_category or ocr_category
    thumb = THUMBS / f"{r['id']}.jpg"
    version = int(thumb.stat().st_mtime) if thumb.exists() else 0
    return {
        "id": r["id"],
        "first_seen": r["first_seen"],
        "last_seen": r["last_seen"],
        "frames": r["frames"],
        "kind": r["kind"],
        "category": category or None,
        "lines": lines,
        "clean": clean,
        "clean_lines": clean_lines,
        "thumb": f"{CONFIG['CAS_PATH_PREFIX']}/thumbs/{r['id']}.jpg?v={version}",
    }


def display_lines(c: dict) -> list[str]:
    return c["clean_lines"] or c["lines"]


def merge_duplicates(con: sqlite3.Connection, window: int = 1800) -> dict[int, int]:
    """Merge cards whose cleaned text matches the previous card of the same kind within `window` seconds.
    The credits show at the start and the end of a clip, and a fade can be read as a new card; the earlier row survives.
    Returns {merged id: surviving id}. Call under the store lock."""
    merged: dict[int, int] = {}
    rows = con.execute("SELECT id, kind, first_seen, last_seen, frames, clean FROM cards WHERE clean IS NOT NULL ORDER BY id").fetchall()
    last_by_kind: dict[str, sqlite3.Row] = {}
    survivors: dict[int, dict] = {}
    for r in rows:
        text = "\n".join((json.loads(r["clean"]) or {}).get("lines") or [])
        prev = last_by_kind.get(r["kind"])
        if prev is not None and text:
            prev_state = survivors[prev["id"]]
            gap = (datetime.fromisoformat(r["first_seen"]) - datetime.fromisoformat(prev_state["last_seen"])).total_seconds()
            if 0 <= gap <= window and similar(text, prev_state["text"]) >= 0.85:
                prev_state["last_seen"] = max(prev_state["last_seen"], r["last_seen"])
                prev_state["frames"] += r["frames"]
                merged[r["id"]] = prev["id"]
                continue
        survivors[r["id"]] = {"text": text, "last_seen": r["last_seen"], "frames": r["frames"]}
        last_by_kind[r["kind"]] = r
    for gone, kept in merged.items():
        st = survivors[kept]
        con.execute("UPDATE cards SET last_seen=?, frames=? WHERE id=?", (st["last_seen"], st["frames"], kept))
        con.execute("DELETE FROM cards WHERE id=?", (gone,))
        try:
            (THUMBS / f"{gone}.jpg").unlink()
        except FileNotFoundError:
            pass
    if merged:
        con.commit()
        log("merged duplicate cards: " + ", ".join(f"#{g}->#{k}" for g, k in merged.items()))
    return merged


# ---------- correction by a model that sees the frame ----------

# The whole ask goes in the user turn, with the image, so it reaches the model intact even through a gateway that rewrites or drops the system prompt.
CORRECT_ASK = """This frame is from the Classic Arts Showcase TV channel, which shows a credit card at the start and end of each clip (title, composer, performers, label). My OCR read the lines between the ocr tags from it; they may have mistakes.

<ocr>
{ocr}
</ocr>

Please read the card in the image and give me the corrected text as one JSON object and nothing else, no prose, no code fence:
{{"category": "", "lines": [...], "title": "", "composer": "", "performers": "", "label": ""}}
"lines" is the card's text, one entry per line as printed, in the card's order, with the misread characters fixed from the image and the OCR noise lines dropped. A small lowercase label in the top-left corner of the picture, such as "recital", "ballet" or "music video", is the channel's category for the clip: put it in "category" and never in "lines". Keep the card's own wording, including quotes and dates, and add no line the card does not show. The other fields are your reading of the card; leave one empty when the card does not give it. The frame may be small, or the card cut off or fading: give the lines you can read, and if the frame shows no credit text at all answer {{"lines": []}}."""


def parse_reply(text: str) -> dict:
    """Parse the model's JSON, tolerating a code fence, prose around the object, and raw line breaks inside strings."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"no JSON object in the reply: {text[:200]!r}")
    body = text[start:end + 1]
    try:
        return json.loads(body, strict=False)
    except json.JSONDecodeError:
        # A line break inside a string: join it with a space and try once more.
        return json.loads(re.sub(r"(?<=[^,{\[\]}\s\"])\s*\n\s*(?=[^\s,\]}\"])", " ", body), strict=False)


class Corrector:
    """Sends each new card and its frame to a Claude model through an Anthropic-format endpoint and stores the cleaned reading."""

    def __init__(self, con: sqlite3.Connection, lock: threading.Lock, on_merge=None):
        self.con = con
        self.lock = lock
        self.on_merge = on_merge  # called with {merged id: surviving id} after a merge
        self.client = None
        self.enabled = bool(CONFIG["CAS_LLM_URL"]) and anthropic is not None
        if CONFIG["CAS_LLM_URL"] and anthropic is None:
            log("correction off: the anthropic package is not installed")
        if self.enabled:
            key = Path(CONFIG["CAS_LLM_KEY_FILE"]).read_text().strip() if CONFIG["CAS_LLM_KEY_FILE"] else None
            self.client = anthropic.Anthropic(base_url=CONFIG["CAS_LLM_URL"], api_key=key, max_retries=2, timeout=90.0)
            log(f"correction on: {CONFIG['CAS_LLM_MODEL']} at {CONFIG['CAS_LLM_URL']}")

    def submit(self, card_id: int, lines: list[str], image: Image.Image):
        if not self.enabled:
            return
        threading.Thread(target=self._run, args=(card_id, lines, image), daemon=True, name=f"correct-{card_id}").start()

    def backfill(self):
        """Correct cards recorded before correction was on, from their thumbnails."""
        if not self.enabled:
            return
        with self.lock:
            rows = self.con.execute("SELECT id, lines FROM cards WHERE clean IS NULL ORDER BY id DESC LIMIT 300").fetchall()
        for r in rows:
            thumb = THUMBS / f"{r['id']}.jpg"
            if thumb.exists():
                self._run(r["id"], json.loads(r["lines"]), Image.open(thumb).convert("RGB"))

    def _run(self, card_id: int, lines: list[str], image: Image.Image):
        for attempt in (1, 2):
            try:
                clean = self._ask(lines, image)
                category, _ = split_category(clean.get("lines") or [])
                category = (clean.get("category") or "").strip().lower() or category
                with self.lock:
                    self.con.execute("UPDATE cards SET clean=?, category=COALESCE(?, category) WHERE id=?", (json.dumps(clean), category, card_id))
                    self.con.commit()
                    merged = merge_duplicates(self.con)
                log(f"card #{card_id} corrected: " + " | ".join(clean.get("lines") or []))
                if merged and self.on_merge:
                    self.on_merge(merged)
                return
            except (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.InternalServerError) as e:
                log(f"card #{card_id} correction attempt {attempt} failed, will retry: {e!r}")
                time.sleep(30)
            except anthropic.APIStatusError as e:
                log(f"card #{card_id} correction refused by the endpoint: {e.status_code} {e.message}")
                return
            except Exception as e:  # noqa: BLE001
                log(f"card #{card_id} correction error: {e!r}")
                return

    def _ask(self, lines: list[str], image: Image.Image) -> dict:
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=85)
        data = base64.standard_b64encode(buf.getvalue()).decode("ascii")
        response = self.client.messages.create(
            model=CONFIG["CAS_LLM_MODEL"],
            max_tokens=4096,
            thinking={"type": "disabled"},  # a transcription task; thinking tokens would count against max_tokens
            output_config={"effort": CONFIG["CAS_LLM_EFFORT"]},
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}},
                    {"type": "text", "text": CORRECT_ASK.format(ocr="\n".join(lines) or "(no lines)")},
                ],
            }],
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("the model declined the request")
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if response.stop_reason == "max_tokens":
            log("correction reply was cut at max_tokens")
        clean = parse_reply(text)
        if not isinstance(clean, dict) or not isinstance(clean.get("lines"), list):
            raise ValueError(f"the reply is not the expected JSON object: {text[:200]!r}")
        clean["lines"] = [str(x).strip() for x in clean["lines"] if str(x).strip()]
        clean["model"] = CONFIG["CAS_LLM_MODEL"]
        return clean


# ---------- the watcher loop ----------

class Watcher(threading.Thread):
    def __init__(self, con: sqlite3.Connection, corrector: "Corrector | None" = None):
        super().__init__(daemon=True, name="watcher")
        self.con = con
        self.lock = threading.Lock()
        self.corrector = corrector
        self.stop = threading.Event()
        self.proc = None
        self.pending = None        # (text, lines, image, count) for a card seen but not yet persisted
        self.current_id = None     # id of the card row being extended
        self.current_text = ""
        self.frames_seen = 0
        self.last_frame_at = None

    def follow_merge(self, merged: dict[int, int]):
        if self.current_id in merged:
            self.current_id = merged[self.current_id]

    def ffmpeg_cmd(self):
        w = CONFIG["CAS_WIDTH"]
        return [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10",
            "-i", CONFIG["CAS_STREAM_URL"],
            "-vf", f"fps=1/{CONFIG['CAS_INTERVAL']},scale={w}:-2",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ]

    def run(self):
        while not self.stop.is_set():
            try:
                self.run_once()
            except Exception as e:  # noqa: BLE001
                log("watcher error:", repr(e))
            if not self.stop.is_set():
                time.sleep(5)

    def frame_size(self):
        # Probe once so the height is known for the chosen width.
        w = CONFIG["CAS_WIDTH"]
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
             "-of", "csv=p=0", CONFIG["CAS_STREAM_URL"]],
            capture_output=True, text=True, timeout=60, env=no_proxy_env(),
        )
        first = next(line for line in probe.stdout.splitlines() if line.strip())
        sw, sh = (int(x) for x in first.strip().split(",")[:2])
        h = (sh * w // sw) // 2 * 2
        return w, h

    def run_once(self):
        w, h = self.frame_size()
        nbytes = w * h * 3
        log(f"ffmpeg starting: {w}x{h}, one frame every {CONFIG['CAS_INTERVAL']} s")
        self.proc = subprocess.Popen(self.ffmpeg_cmd(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=no_proxy_env())
        try:
            while not self.stop.is_set():
                data = self.proc.stdout.read(nbytes)
                if len(data) < nbytes:
                    break
                self.handle(Image.frombytes("RGB", (w, h), data))
        finally:
            err = b""
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            if self.proc.stderr:
                err = self.proc.stderr.read()
            log("ffmpeg ended", self.proc.returncode, err.decode("utf-8", "replace")[-300:])
            self.proc = None

    def handle(self, image: Image.Image):
        self.frames_seen += 1
        self.last_frame_at = datetime.now(timezone.utc)
        lines = card_lines(ocr(image))
        now = self.last_frame_at.isoformat(timespec="seconds")
        if not lines:
            self.pending = None
            return
        text = "\n".join(lines)
        if self.current_id is not None and similar(text, self.current_text) >= 0.5:
            # Same card still on screen: extend it.
            with self.lock:
                self.con.execute("UPDATE cards SET last_seen=?, frames=frames+1 WHERE id=?", (now, self.current_id))
                self.con.commit()
            return
        if self.pending and similar(text, self.pending[0]) >= 0.5:
            count = self.pending[3] + 1
            self.pending = (self.pending[0], self.pending[1], self.pending[2], count)
        else:
            self.pending = (text, lines, image, 1)
            count = 1
        if count >= CONFIG["CAS_MIN_FRAMES"]:
            text, lines, first_image, _ = self.pending
            with self.lock:
                category, _ = split_category(lines)
                cur = self.con.execute(
                    "INSERT INTO cards(first_seen, last_seen, frames, text, lines, kind, category) VALUES (?,?,?,?,?,?,?)",
                    (now, now, count, text, json.dumps(lines), card_kind(lines), category),
                )
                self.con.commit()
                self.current_id = cur.lastrowid
                self.current_text = text
            THUMBS.mkdir(parents=True, exist_ok=True)
            thumb = crop_to_picture(first_image)
            thumb.thumbnail((320, 320))
            thumb.save(THUMBS / f"{self.current_id}.jpg", quality=80)
            self.pending = None
            log(f"card #{self.current_id}: " + " | ".join(lines))
            if self.corrector:
                self.corrector.submit(self.current_id, lines, first_image.copy())


# ---------- HTTP ----------

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="30"><title>Classic Arts Showcase: what's playing</title>
<style>
:root{color-scheme:light dark;--fg:#111;--bg:#fff;--muted:#666;--line:#ddd}
@media(prefers-color-scheme:dark){:root{--fg:#eee;--bg:#111;--muted:#999;--line:#333}}
body{font:16px/1.45 -apple-system,system-ui,sans-serif;margin:0;padding:16px;max-width:720px;margin-inline:auto;color:var(--fg);background:var(--bg)}
h1{font-size:20px;margin:0 0 4px}h2{font-size:14px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:24px 0 8px}
.card{display:flex;gap:12px;padding:12px 0;border-top:1px solid var(--line)}.card img{width:120px;height:auto;border-radius:4px;flex:none}
.card .t{white-space:pre-line}.card time{display:block;color:var(--muted);font-size:13px;margin-top:4px}.now .t{font-size:18px}
.empty{color:var(--muted)}
</style></head><body><h1>Classic Arts Showcase: what's playing</h1><div class="empty"><a href="__P__/watch">Watch here</a> · <a href="https://www.classicartsshowcase.org/watch-classic-arts-showcase/">Watch on the official site</a> · <a href="__P__/about">About this page</a>. Read off the screen credits by a watcher and tidied by a model; refreshes every 30 s.</div>
<h2>Now playing</h2>__NOW__<h2>Recently played</h2>__RECENT__</body></html>"""


STYLE = PAGE[PAGE.index("<style>"):PAGE.index("</style>") + 8]

WATCH = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Classic Arts Showcase: watch</title>""" + STYLE + """<style>body{max-width:1100px}video{width:100%;background:#000;border-radius:6px}#now{white-space:pre-line;margin-top:12px}</style></head>
<body><h1>Classic Arts Showcase</h1><div class="empty"><a href="__P__/">What's playing</a> · <a href="https://www.classicartsshowcase.org/watch-classic-arts-showcase/">Official site</a> · <a href="__P__/about">About</a></div>
<video id="v" controls autoplay playsinline muted></video>
<div class="empty">The stream starts muted so the browser allows autoplay; press the speaker. <a href="__STREAM__">HLS playlist</a> for any player.</div>
<h2>Now playing</h2><div id="now" class="empty">…</div>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.6.5/dist/hls.min.js"></script>
<script>
const src="__STREAM__", v=document.getElementById("v");
if (v.canPlayType("application/vnd.apple.mpegurl")) { v.src = src; }
else if (window.Hls && Hls.isSupported()) { const h = new Hls({liveSyncDurationCount: 3}); h.loadSource(src); h.attachMedia(v); }
else { document.getElementById("now").textContent = "This browser cannot play HLS; use the playlist link in another player."; }
async function now(){ try { const r = await fetch("__P__/api/now"); const j = await r.json(); const c = j.now; document.getElementById("now").textContent = c ? (c.clean_lines.length ? c.clean_lines : c.lines).join("\n") : "Between credits."; } catch (e) {} }
now(); setInterval(now, 30000);
</script></body></html>"""

ABOUT = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>About this page</title>""" + STYLE + """</head><body><h1>About this page</h1>
<div class="empty"><a href="__P__/">What's playing</a> · <a href="__P__/watch">Watch</a></div>
<p><a href="https://www.classicartsshowcase.org/">Classic Arts Showcase</a> is a free, non-commercial 24-hour channel of short clips of ballet, opera, concerts, dance and film, paid for by the Lloyd E. Rigler - Lawrence E. Deutsch Foundation and carried by public-service channels. It publishes no playlist: the only record of a clip is the credit card the channel shows at its start and end.</p>
<p>This page is not the channel's. A small watcher reads the channel's public stream, decodes one frame every few seconds, reads the credit cards with OCR, has a language model tidy the text while looking at the frame, and keeps one entry per card with a thumbnail. The channel's own notices (who pays for it, how to write to it) are kept out of the list. The stream itself is played straight from the channel's servers; nothing here re-streams it.</p>
<h2>Links</h2>
<ul><li><a href="https://www.classicartsshowcase.org/watch-classic-arts-showcase/">The official web player</a></li>
<li><a href="__STREAM__">The HLS playlist</a>, which plays in VLC, IINA, any IPTV app, or Safari</li>
<li><a href="__P__/api/now">/api/now</a> and <a href="__P__/api/recent?limit=50">/api/recent</a>: the same data as JSON (add <code>&amp;notices=1</code> to include the channel's notices)</li>
<li><a href="__REPO__">The source</a>: the watcher and an iPhone, Apple TV and Mac app that plays the stream with these credits</li></ul>
</body></html>"""


def render(template: str) -> str:
    stream = CONFIG["CAS_STREAM_URL"].rsplit("/", 1)[0] + "/master.m3u8"
    return template.replace("__P__", CONFIG["CAS_PATH_PREFIX"]).replace("__STREAM__", stream).replace("__REPO__", CONFIG["CAS_REPO_URL"])


def fmt_card(c: dict, cls: str = "") -> str:
    when = datetime.fromisoformat(c["first_seen"]).astimezone().strftime("%a %H:%M")
    text = "\n".join(display_lines(c)).replace("&", "&amp;").replace("<", "&lt;")
    cat = f'{c["category"]} · ' if c.get("category") else ""
    return f'<div class="card {cls}"><img src="{c["thumb"]}" alt=""><div><div class="t">{text}</div><time>{cat}{when}</time></div></div>'


class Handler(BaseHTTPRequestHandler):
    watcher: Watcher = None
    con: sqlite3.Connection = None

    def log_message(self, *_):
        pass

    def send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store" if ctype.startswith(("application", "text")) else "max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def json(self, obj, code=200):
        self.send(code, json.dumps(obj).encode(), "application/json")

    def recent(self, limit: int, notices: bool = False) -> list[dict]:
        where = "" if notices else "WHERE kind = 'clip'"
        with self.watcher.lock:
            rows = self.con.execute(f"SELECT * FROM cards {where} ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [row_to_dict(r) for r in rows]

    @staticmethod
    def within_window(card: dict) -> bool:
        last = datetime.fromisoformat(card["last_seen"])
        return datetime.now(timezone.utc) - last <= timedelta(seconds=CONFIG["CAS_NOW_WINDOW"])

    def now_playing(self):
        cards = self.recent(1)
        return cards[0] if cards and self.within_window(cards[0]) else None

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        prefix = CONFIG["CAS_PATH_PREFIX"]
        if prefix and path.startswith(prefix):
            path = path[len(prefix):] or "/"
        q = parse_qs(url.query)
        if path == "/api/now":
            return self.json({"now": self.now_playing(), "watcher": self.status()})
        if path == "/api/recent":
            limit = max(1, min(500, int(q.get("limit", ["50"])[0])))
            notices = q.get("notices", ["0"])[0] in ("1", "true", "yes")
            return self.json({"cards": self.recent(limit, notices)})
        if path == "/health":
            return self.json(self.status())
        if path.startswith("/thumbs/") and path.endswith(".jpg") and path[8:-4].isdigit():
            f = THUMBS / f"{int(path[8:-4])}.jpg"
            if f.exists():
                return self.send(200, f.read_bytes(), "image/jpeg")
            return self.send(404, b"no thumb", "text/plain")
        if path == "/watch":
            return self.send(200, render(WATCH).encode(), "text/html; charset=utf-8")
        if path == "/about":
            return self.send(200, render(ABOUT).encode(), "text/html; charset=utf-8")
        if path == "/":
            # One query: the latest card is the current one (as in the app), the rest is the history.
            cards = self.recent(51)
            current, rest = (cards[0], cards[1:]) if cards else (None, [])
            if current:
                note = "" if self.within_window(current) else '<div class="empty">The last credits read; the next card comes at the end of the clip.</div>'
                now_html = fmt_card(current, "now") + note
            else:
                now_html = '<div class="empty">Nothing read yet.</div>'
            page = render(PAGE).replace("__NOW__", now_html)
            page = page.replace("__RECENT__", "".join(fmt_card(c) for c in rest) or '<div class="empty">Nothing else yet.</div>')
            return self.send(200, page.encode(), "text/html; charset=utf-8")
        self.send(404, b"not found", "text/plain")

    def status(self):
        w = self.watcher
        return {
            "frames_seen": w.frames_seen,
            "last_frame_at": w.last_frame_at.isoformat(timespec="seconds") if w.last_frame_at else None,
            "ffmpeg_running": w.proc is not None and w.proc.poll() is None,
            "stream": CONFIG["CAS_STREAM_URL"],
        }


def main():
    CONFIG["CAS_DATA"].mkdir(parents=True, exist_ok=True)
    con = db()
    lock = threading.Lock()
    watcher = Watcher(con)
    corrector = Corrector(con, lock, on_merge=watcher.follow_merge)
    watcher.corrector = corrector
    watcher.lock = lock
    with lock:  # classify cards recorded before the kind column existed
        for r in con.execute("SELECT id, lines, clean FROM cards WHERE kind = 'clip'").fetchall():
            lines = json.loads(r["lines"]) + ((json.loads(r["clean"]) or {}).get("lines", []) if r["clean"] else [])
            if card_kind(lines) == "notice":
                con.execute("UPDATE cards SET kind='notice' WHERE id=?", (r["id"],))
        con.commit()
        merge_duplicates(con)
    threading.Thread(target=corrector.backfill, daemon=True, name="backfill").start()
    Handler.watcher = watcher
    Handler.con = con
    server = ThreadingHTTPServer((CONFIG["CAS_BIND"], CONFIG["CAS_PORT"]), Handler)

    def shutdown(*_):
        log("stopping")
        watcher.stop.set()
        if watcher.proc:
            watcher.proc.terminate()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    watcher.start()
    log(f"serving on {CONFIG['CAS_BIND']}:{CONFIG['CAS_PORT']} prefix='{CONFIG['CAS_PATH_PREFIX']}' data={CONFIG['CAS_DATA']}")
    server.serve_forever()


if __name__ == "__main__":
    main()
