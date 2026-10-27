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
}

DB_PATH = CONFIG["CAS_DATA"] / "cards.sqlite"
THUMBS = CONFIG["CAS_DATA"] / "thumbs"

# Words that mark the layout of a credit card, not a title in the picture.
CARD_HINTS = re.compile(r"\b(music by|conductor|orchestra|choreograph|performed|directed|produced|libretto|soprano|tenor|baritone|pianist|violin|ballet|opera|symphony|quartet|philharmonic|ltd|inc|records|films?)\b", re.I)


def log(*parts):
    print(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), *parts, flush=True)


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
    try:
        con.execute("ALTER TABLE cards ADD COLUMN clean TEXT")
    except sqlite3.OperationalError:
        pass  # column already there
    con.commit()
    return con


def row_to_dict(r: sqlite3.Row) -> dict:
    clean = json.loads(r["clean"]) if r["clean"] else None
    return {
        "id": r["id"],
        "first_seen": r["first_seen"],
        "last_seen": r["last_seen"],
        "frames": r["frames"],
        "lines": json.loads(r["lines"]),
        "clean": clean,
        "clean_lines": (clean or {}).get("lines") or [],
        "thumb": f"{CONFIG['CAS_PATH_PREFIX']}/thumbs/{r['id']}.jpg",
    }


def display_lines(c: dict) -> list[str]:
    return c["clean_lines"] or c["lines"]


# ---------- correction by a model that sees the frame ----------

CORRECT_SYSTEM = """You correct OCR of the credit card that the Classic Arts Showcase TV channel shows at the start and end of each clip. You get the OCR lines and the frame they were read from. Return only a JSON object, no prose and no code fence:
{"lines": [...], "title": "", "composer": "", "performers": "", "label": ""}
"lines" is the card's text, one entry per line as printed on the card, in the card's order, with misread characters fixed from the image and OCR noise lines dropped. Keep the card's own wording, including quotes and dates. Do not add lines the card does not show. The other fields are your reading of the card; leave one empty when the card does not give it."""


class Corrector:
    """Sends each new card and its frame to a Claude model through an Anthropic-format endpoint and stores the cleaned reading."""

    def __init__(self, con: sqlite3.Connection, lock: threading.Lock):
        self.con = con
        self.lock = lock
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
            rows = self.con.execute("SELECT id, lines FROM cards WHERE clean IS NULL ORDER BY id DESC LIMIT 50").fetchall()
        for r in rows:
            thumb = THUMBS / f"{r['id']}.jpg"
            if thumb.exists():
                self._run(r["id"], json.loads(r["lines"]), Image.open(thumb).convert("RGB"))

    def _run(self, card_id: int, lines: list[str], image: Image.Image):
        for attempt in (1, 2):
            try:
                clean = self._ask(lines, image)
                with self.lock:
                    self.con.execute("UPDATE cards SET clean=? WHERE id=?", (json.dumps(clean), card_id))
                    self.con.commit()
                log(f"card #{card_id} corrected: " + " | ".join(clean.get("lines") or []))
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
            max_tokens=1024,
            output_config={"effort": CONFIG["CAS_LLM_EFFORT"]},
            system=CORRECT_SYSTEM,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}},
                    {"type": "text", "text": "OCR lines:\n" + "\n".join(lines)},
                ],
            }],
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("the model declined the request")
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if text.startswith("```"):
            text = text.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
        clean = json.loads(text)
        if not isinstance(clean, dict) or not isinstance(clean.get("lines"), list):
            raise ValueError("the reply is not the expected JSON object")
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
            capture_output=True, text=True, timeout=60,
        )
        first = next(line for line in probe.stdout.splitlines() if line.strip())
        sw, sh = (int(x) for x in first.strip().split(",")[:2])
        h = (sh * w // sw) // 2 * 2
        return w, h

    def run_once(self):
        w, h = self.frame_size()
        nbytes = w * h * 3
        log(f"ffmpeg starting: {w}x{h}, one frame every {CONFIG['CAS_INTERVAL']} s")
        self.proc = subprocess.Popen(self.ffmpeg_cmd(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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
                cur = self.con.execute(
                    "INSERT INTO cards(first_seen, last_seen, frames, text, lines) VALUES (?,?,?,?,?)",
                    (now, now, count, text, json.dumps(lines)),
                )
                self.con.commit()
                self.current_id = cur.lastrowid
                self.current_text = text
            THUMBS.mkdir(parents=True, exist_ok=True)
            thumb = first_image.copy()
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
</style></head><body><h1>Classic Arts Showcase</h1><div class="empty">Read off the screen credits by a watcher and tidied by a model; the channel publishes no playlist. Refreshes every 30 s.</div>
<h2>Now playing</h2>__NOW__<h2>Recently played</h2>__RECENT__</body></html>"""


def fmt_card(c: dict, cls: str = "") -> str:
    when = datetime.fromisoformat(c["first_seen"]).astimezone().strftime("%a %H:%M")
    text = "\n".join(display_lines(c)).replace("&", "&amp;").replace("<", "&lt;")
    return f'<div class="card {cls}"><img src="{c["thumb"]}" alt=""><div><div class="t">{text}</div><time>{when}</time></div></div>'


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

    def recent(self, limit: int) -> list[dict]:
        with self.watcher.lock:
            rows = self.con.execute("SELECT * FROM cards ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [row_to_dict(r) for r in rows]

    def now_playing(self):
        cards = self.recent(1)
        if not cards:
            return None
        last = datetime.fromisoformat(cards[0]["last_seen"])
        if datetime.now(timezone.utc) - last > timedelta(seconds=CONFIG["CAS_NOW_WINDOW"]):
            return None
        return cards[0]

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
            return self.json({"cards": self.recent(limit)})
        if path == "/health":
            return self.json(self.status())
        if path.startswith("/thumbs/") and path.endswith(".jpg") and path[8:-4].isdigit():
            f = THUMBS / f"{int(path[8:-4])}.jpg"
            if f.exists():
                return self.send(200, f.read_bytes(), "image/jpeg")
            return self.send(404, b"no thumb", "text/plain")
        if path == "/":
            now = self.now_playing()
            page = PAGE.replace("__NOW__", fmt_card(now, "now") if now else '<div class="empty">Between credits; the last card is below.</div>')
            page = page.replace("__RECENT__", "".join(fmt_card(c) for c in self.recent(50)) or '<div class="empty">Nothing read yet.</div>')
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
    corrector = Corrector(con, lock)
    watcher = Watcher(con, corrector)
    watcher.lock = lock
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
