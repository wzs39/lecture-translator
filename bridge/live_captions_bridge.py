"""
Windows Live Captions bridge — runs on the host (not in Docker).

Reads the Windows 11 Live Captions window text via UI Automation, splits
the rolling caption buffer into complete sentences, and POSTs each new
sentence to the translator backend (/api/captions), which translates and
stores it.

Observed window behavior (Windows 11): the caption area is a single
TextControl whose Name holds the whole rolling buffer and is rewritten as
recognition corrects itself, so we track complete sentences by count
rather than diffing strings.

Usage:
    python live_captions_bridge.py [--url http://localhost:8000]

Requirements: pip install -r requirements.txt  (uiautomation, requests)
Turn Live Captions on first: Win+Ctrl+L.
"""
import argparse
import difflib
import json
import re
import socket
import sys
import threading
import time
from collections import deque
from pathlib import Path

import requests
import uiautomation as uia

WINDOW_CLASS = "LiveCaptionsDesktopWindow"
SENTENCE_RE = re.compile(r"[^.!?。！？]+[.!?。！？]+")
PRIVATE_USE_RE = re.compile(r"[\ue000-\uf8ff]")
POLL_SEC = 0.2  # fast speech can scroll lines out between polls; stay quick

# The caption window holds ~15-20 lines and recognition rewrites older ones;
# the seen-set must cover all of them or old sentences get re-posted forever.
SEEN_EXACT = 400   # exact-match lookback
SEEN_SIM = 20      # fuzzy (rewrite) lookback

# Long-sentence mode: merge 2-4 committed sentences into one chunk before
# translating, so subtitles read as continuous speech, not fragments.
CHUNK_SENTS = 3
CHUNK_CHARS = 280
CHUNK_PAUSE = 6.0   # flush a partial chunk after this many seconds idle
QUIET_FLUSH = 4.0   # buffer unchanged this long => speech stopped: emit the pending tail


def find_captions_window():
    root = uia.GetRootControl()
    for win in root.GetChildren():
        try:
            if win.ClassName == WINDOW_CLASS:
                return win
            name = (win.Name or "").strip().lower()
            if "live captions" in name or "实时字幕" in name:
                return win
        except Exception:
            continue
    return None


def caption_text(win):
    """Concatenate visible caption text, skipping UI glyphs and noise."""
    parts = []
    stack = [win]
    while stack:
        c = stack.pop(0)
        try:
            if c.ControlTypeName == "TextControl" and c.Name:
                t = c.Name.strip()
                if t and not PRIVATE_USE_RE.search(t):
                    parts.append(t)
            stack.extend(c.GetChildren())
        except Exception:
            continue
    return " ".join(parts).strip()


class SentenceTracker:
    """Emits each complete sentence in the rolling buffer exactly once.

    Real window behavior (measured): the caption area is one TextControl
    whose Name holds a few rolling LINES separated by newlines; lines are
    rewritten as recognition corrects itself, and the final line usually
    lacks a trailing period until it rolls away. So we split per line,
    dedupe by normalized text with a similarity guard (rewrites produce
    near-duplicates), and flush an incomplete tail when it disappears.
    """

    def __init__(self):
        self.exact = set()  # normalized posted sentences (exact lookback)
        self.order = []     # same, oldest-first, for the fuzzy lookback
        self.pending = ""   # incomplete tail of the last line

    @staticmethod
    def _norm(s):
        return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", s.lower())

    def _dup(self, n):
        if n in self.exact:
            return True
        # fuzzy pass only over recent entries: rewrites happen soon after
        for x in self.order[-SEEN_SIM:]:
            if n in x or (x in n and len(x) > 25):
                return True
            if len(x) >= len(n):
                step = max(1, (len(x) - len(n)) // 8 + 1)
                for i in range(0, len(x) - len(n) + 1, step):
                    if difflib.SequenceMatcher(None, n, x[i:i + len(n)]).ratio() > 0.88:
                        return True
            elif difflib.SequenceMatcher(None, n, x).ratio() > 0.88:
                return True
        return False

    def _emit(self, s, out):
        n = self._norm(s)
        if len(n) < 4 or self._dup(n):
            return
        self.exact.add(n)
        self.order.append(n)
        del self.order[:-SEEN_EXACT]
        out.append(s.strip())

    def feed(self, text):
        out = []
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        for i, line in enumerate(lines):
            for s in SENTENCE_RE.findall(line):
                self._emit(s, out)
            tail = SENTENCE_RE.split(line)[-1].strip()
            if not tail:
                continue
            if i < len(lines) - 1:
                self._emit(tail, out)  # rolled away without a period
            else:
                # growing tail of the live line: flush only if it vanished
                if self.pending and self._norm(self.pending) not in self._norm(text):
                    self._emit(self.pending, out)
                self.pending = tail
        if self.pending and not lines:
            self._emit(self.pending, out)
            self.pending = ""
        return out

    def flush_pending(self):
        """Emit the incomplete tail when speech stops: without new lines the
        buffer never rolls, so the last sentence would wait forever."""
        s, self.pending = self.pending, ""
        out = []
        if s:
            self._emit(s, out)
        return out


class Chunker:
    """Merges committed sentences into larger chunks for translation."""

    def __init__(self):
        self.sents = []
        self.offset = None
        self.last = 0.0

    def add(self, sentence, offset, now):
        if not self.sents:
            self.offset = offset
        self.sents.append(sentence)
        self.last = now
        return self.full()

    def full(self):
        text = " ".join(self.sents)
        return (len(self.sents) >= CHUNK_SENTS or len(text) >= CHUNK_CHARS), text, self.offset

    def due(self, now):
        return bool(self.sents) and now - self.last >= CHUNK_PAUSE

    def flush(self):
        text, off = " ".join(self.sents), self.offset
        self.sents, self.offset = [], None
        return text, off


# ---- loss-prevention cache: write-ahead journal + confirmed-sent marker ----
# Every chunk is appended to captions-journal.jsonl BEFORE sending. The count
# of contiguously confirmed entries lives in captions-journal.sent; entries
# past it are re-sent on startup (the server dedupes, so no double store).
JOURNAL_PATH = Path(__file__).resolve().parent / "captions-journal.jsonl"
SENT_PATH = JOURNAL_PATH.with_suffix(".sent")
RETRY_EVERY = 5.0  # seconds between delivery retries while the backend is down


def journal_append(text, offset):
    with JOURNAL_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"text": text, "offset": offset}, ensure_ascii=False) + "\n")


def journal_state():
    """Returns (total_entries, confirmed_count)."""
    total = 0
    if JOURNAL_PATH.exists():
        total = sum(1 for ln in JOURNAL_PATH.read_text(encoding="utf-8").splitlines() if ln.strip())
    confirmed = 0
    if SENT_PATH.exists():
        try:
            confirmed = min(total, int(SENT_PATH.read_text(encoding="utf-8").strip() or 0))
        except ValueError:
            confirmed = 0
    return total, confirmed


def mark_confirmed(n):
    SENT_PATH.write_text(str(n), encoding="utf-8")


class Delivery:
    """In-order, at-least-once delivery with a durable backlog."""

    def __init__(self, url):
        self.url = url
        self.unsent = deque()
        total, confirmed = journal_state()
        if JOURNAL_PATH.exists():
            entries = [json.loads(ln) for ln in
                       JOURNAL_PATH.read_text(encoding="utf-8").splitlines() if ln.strip()]
            self.unsent.extend(entries[confirmed:])
        self.last_try = 0.0

    def submit(self, text, offset):
        journal_append(text, offset)          # durable BEFORE sending
        self.unsent.append({"text": text, "offset": offset})
        self._try_deliver()

    def retry_due(self, now):
        return bool(self.unsent) and now - self.last_try >= RETRY_EVERY

    def retry(self, now):
        self.last_try = now
        self._try_deliver()

    def _try_deliver(self):
        while self.unsent:
            entry = self.unsent[0]
            try:
                r = requests.post(f"{self.url}/api/captions",
                                  json={"text": entry["text"], "offset": entry["offset"]},
                                  timeout=90)
                ok = r.ok
                line = r.json() if ok else {}
            except Exception as e:
                ok, line = False, {}
                print(f"  backend unreachable ({e}); cached, will retry")
            if not ok:
                break  # keep order: stop at the first failure
            shown = line.get("translation", "")
            print(f"[{int(entry['offset']//60):02d}:{int(entry['offset']%60):02d}] OK {entry['text']}")
            if shown:
                print(f"        -> {shown}")
            self.unsent.popleft()
            total, _ = journal_state()
            mark_confirmed(total - len(self.unsent))


def _host_disk_free_gb(drive):
    """Free GB on the given Windows drive; None when unavailable."""
    try:
        import ctypes
        free = ctypes.c_ulonglong(0)
        if ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                ctypes.c_wchar_p(drive), ctypes.byref(free),
                ctypes.c_ulonglong(0), ctypes.c_ulonglong(0)):
            return round(free.value / (1024 ** 3), 1)
    except Exception:
        pass
    return None


BRIDGE_STATE = {"window": None, "last_error": None}  # read by the heartbeat thread


def _heartbeat(url, drive):
    """Liveness ping + host disk space + window/error state for the page."""
    def loop():
        while True:
            try:
                requests.post(f"{url}/api/bridge/heartbeat",
                              json={"disk_free_gb": _host_disk_free_gb(drive),
                                    "window_found": BRIDGE_STATE["window"],
                                    "error": BRIDGE_STATE["last_error"]},
                              timeout=5)
            except Exception:
                pass
            time.sleep(10)
    threading.Thread(target=loop, daemon=True).start()


def _selfcheck(url, drive):
    """Diagnose the bridge + backend + window without starting to capture."""
    out = [f"selfcheck {time.strftime('%Y-%m-%d %H:%M:%S')}"]
    problems = []
    try:
        r = requests.get(f"{url}/api/self-check", timeout=8)
        ok = r.status_code == 200 and r.json().get("ready")
    except Exception as e:
        ok, e = False, e
    out.append(f"backend: {'OK' if ok else 'FAIL ' + repr(e)}")
    if not ok:
        problems.append("backend")
    free = _host_disk_free_gb(drive)
    out.append(f"disk {drive}: {free if free is not None else 'FAIL'} GB free")
    if free is None:
        problems.append("disk")
    win = find_captions_window()
    out.append(f"caption window: {'FOUND' if win else 'not open (Win+Ctrl+L)'}")
    log_path = Path(__file__).with_name("selfcheck.log")
    log_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    print("\n".join(out))
    print("RESULT: " + ("PROBLEMS: " + ", ".join(problems) if problems else "OK"))
    return 0 if not problems else 1


_LOCK_SOCKET = None  # module-level reference keeps the singleton bind alive


def _lock_singleton():
    """Refuse to start when another bridge instance is already running.
    Two bridges reading the same caption window double-post everything and
    fight each other; a stale one can also mask a healthy one. The bound
    socket must stay referenced for the process lifetime or the port is
    released and a second instance can start."""
    global _LOCK_SOCKET
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 49190))
        s.listen(1)
    except OSError:
        print("  another bridge instance is already running; exiting (code 3).")
        sys.exit(3)  # distinct from crashes so wrappers know not to restart
    _LOCK_SOCKET = s


def main():
    # Live Captions translations contain Chinese; never crash on a GBK console.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--disk", default="D:\\",
                    help="drive to watch for the low-disk-space page banner")
    ap.add_argument("--selfcheck", action="store_true",
                    help="diagnose backend/disk/window and exit without capturing")
    args = ap.parse_args()
    if args.selfcheck:
        sys.exit(_selfcheck(args.url, args.disk))
    _lock_singleton()  # exits(3) if another bridge is already running

    tracker = SentenceTracker()
    chunker = Chunker()
    delivery = Delivery(args.url)
    # Heartbeat from the very start: while the caption window is not open the
    # page should show "bridge alive, waiting for the window", not "bridge down".
    _heartbeat(args.url, args.disk)
    print(f"bridge: backend {args.url}; looking for the Live Captions window...")
    win = None
    while win is None:
        BRIDGE_STATE["window"] = False
        win = find_captions_window()
        if win is None:
            print("  Live Captions not found. Press Win+Ctrl+L to open it, then leave it on screen.")
            time.sleep(3)
    BRIDGE_STATE["window"] = True
    print(f"  found: '{win.Name}' — capturing (Ctrl+C to stop)")
    pending = len(delivery.unsent)
    if pending:
        print(f"  resending {pending} cached chunk(s) from a previous run...")
    offset0 = time.monotonic()
    prev_text = ""
    last_change = time.monotonic()
    last_rediscover = time.monotonic()
    try:
        while True:
            time.sleep(POLL_SEC)
            try:
                now = time.monotonic()
                # Refresh the window handle periodically: the Live Captions
                # UIA element can go stale (window closed/reopened, display
                # change) and then silently delivers empty text — the only
                # sign of death. Keep the refresh short so recovery from a
                # window restart is seconds, not a minute.
                if now - last_rediscover >= 10:
                    last_rediscover = now
                    fresh = find_captions_window()
                    if fresh is not None:
                        win = fresh
                text = caption_text(win)
                if text != prev_text:
                    prev_text = text
                    last_change = now
                    for sentence in tracker.feed(text):
                        ready, chunk, off = chunker.add(sentence, round(now - offset0, 2), now)
                        if ready:
                            ctext, coff = chunker.flush()
                            delivery.submit(ctext, coff)
                elif now - last_change >= QUIET_FLUSH:  # speech stopped: the
                    # tail sentence would otherwise sit in pending forever
                    for sentence in tracker.flush_pending():
                        ready, chunk, off = chunker.add(sentence, round(now - offset0, 2), now)
                        if ready:
                            ctext, coff = chunker.flush()
                            delivery.submit(ctext, coff)
                if chunker.due(now):  # speaker paused: flush the tail
                    ctext, coff = chunker.flush()
                    delivery.submit(ctext, coff)
                if delivery.retry_due(now):
                    delivery.retry(now)
            except Exception as e:
                # stale handle: re-discover the window and keep going
                BRIDGE_STATE["last_error"] = str(e)[:120]
                BRIDGE_STATE["window"] = False
                print(f"  poll error: {e}; re-discovering the caption window")
                win = None
                while win is None:
                    try:
                        win = find_captions_window()
                    except Exception:
                        win = None
                    if win is None:
                        print("  Live Captions window gone; waiting for it to reopen...")
                        time.sleep(3)
                BRIDGE_STATE["window"] = True
                BRIDGE_STATE["last_error"] = None
                print("  caption window reconnected")
                prev_text = ""
    finally:
        # Ctrl+C / window vanished: flush the half-built chunk so nothing is lost
        if chunker.sents:
            ctext, coff = chunker.flush()
            delivery.submit(ctext, coff)
        print("  stopped; journal kept at", JOURNAL_PATH.name)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
