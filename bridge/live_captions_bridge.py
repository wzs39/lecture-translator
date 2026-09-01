"""
Windows Live Captions bridge — runs on the host (not in Docker).

Reads the Windows 11 Live Captions window text via UI Automation,
stabilizes it into complete sentences, and POSTs each sentence to the
translator backend (/api/captions), which translates and stores it.

Usage:
    python live_captions_bridge.py [--url http://localhost:8000]

Requirements: pip install -r requirements.txt  (uiautomation, requests)
Turn Live Captions on first: Win+Ctrl+L.
"""
import argparse
import time

import requests
import uiautomation as uia

# Window titles seen across Windows versions/locales.
TITLE_KEYWORDS = ["live captions", "实时字幕", "livecaptions", "live-captions", "字幕"]

STABLE_MS = 900        # commit fragment after this much no-change time
MIN_CHARS = 4          # don't commit tiny fragments
MAX_CHARS = 300        # or unbounded run-ons
POLL_SEC = 0.25


def find_captions_window():
    root = uia.GetRootControl()
    for win in root.GetChildren():
        try:
            name = (win.Name or "").strip().lower()
            if any(k in name for k in TITLE_KEYWORDS):
                return win
        except Exception:
            continue
    return None


def caption_text(win):
    """Join all Text descendants in tree order — the captions window keeps
    a few lines; appending order is bottom-up so the diff gives the tail."""
    return " ".join(p for p in walk(win) if p).strip()


def walk(control):
    stack = [control]
    while stack:
        c = stack.pop(0)
        try:
            if c.ControlTypeName == "TextControl" and c.Name:
                yield c.Name.strip()
            stack.extend(c.GetChildren())
        except Exception:
            continue


def tail_after(prev, cur):
    """New text appended to the caption window since the last snapshot."""
    if not cur:
        return ""
    if cur == prev:
        return ""
    if prev and cur.startswith(prev):
        return cur[len(prev):].strip()
    if prev and prev in cur:
        return cur.split(prev, 1)[1].strip()
    return cur  # window cleared or rewrote itself: treat everything as new


class Stabilizer:
    """Accumulates caption fragments and commits complete sentences:
    on sentence punctuation, on a pause in updates, or at MAX_CHARS."""

    def __init__(self):
        self.buf = ""
        self.last_change = time.monotonic()

    def feed(self, fragment):
        now = time.monotonic()
        sentence = None
        if fragment:
            self.buf += (" " if self.buf and not self.buf.endswith(("-", "'")) else "") + fragment
            self.last_change = now
        stable_for = (now - self.last_change) * 1000
        if self.buf and (self.buf[-1] in ".!?。！？" or stable_for >= STABLE_MS or len(self.buf) >= MAX_CHARS):
            if len(self.buf) >= MIN_CHARS or self.buf[-1] in ".!?。！？":
                sentence = self.buf.strip()
                self.buf = ""
        return sentence

    def flush(self):
        s, self.buf = self.buf.strip(), ""
        return s or None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    args = ap.parse_args()

    print(f"bridge: backend {args.url}; looking for the Live Captions window...")
    win = None
    while win is None:
        win = find_captions_window()
        if win is None:
            print("  Live Captions not found. Press Win+Ctrl+L to open it, then leave it on screen.")
            time.sleep(3)
    print(f"  found: '{win.Name}' — capturing (Ctrl+C to stop)")

    offset0 = time.monotonic()
    stab = Stabilizer()
    prev = caption_text(win)
    while True:
        time.sleep(POLL_SEC)
        try:
            cur = caption_text(win)
        except Exception as e:
            print(f"  window read failed ({e}); retrying...")
            win = find_captions_window()
            if win is None:
                continue
            prev = caption_text(win)
            continue
        fragment = tail_after(prev, cur)
        prev = cur
        sentence = stab.feed(fragment)
        if not sentence:
            continue
        offset = round(time.monotonic() - offset0, 2)
        try:
            r = requests.post(f"{args.url}/api/captions",
                              json={"text": sentence, "offset": offset}, timeout=90)
            ok = r.ok
            line = r.json() if ok else {}
        except Exception as e:
            ok, line = False, {}
            print(f"  backend unreachable: {e}")
        shown = line.get("translation", "")
        print(f"[{int(offset//60):02d}:{int(offset%60):02d}] {'OK' if ok else 'FAILED'} {sentence}")
        if shown:
            print(f"        -> {shown}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
