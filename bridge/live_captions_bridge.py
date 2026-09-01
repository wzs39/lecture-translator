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
import re
import time

import requests
import uiautomation as uia

WINDOW_CLASS = "LiveCaptionsDesktopWindow"
SENTENCE_RE = re.compile(r"[^.!?。！？]+[.!?。！？]+")
PRIVATE_USE_RE = re.compile(r"[\ue000-\uf8ff]")
POLL_SEC = 0.4


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
        self.seen = []    # normalized posted sentences (bounded)
        self.pending = ""  # incomplete tail of the last line

    @staticmethod
    def _norm(s):
        return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", s.lower())

    def _dup(self, n):
        """True if n was already covered: exact, a substring of a longer
        posted run, or a near-match of some window of one (recognition
        corrections rewrite whole runs with punctuation/case changes)."""
        for x in self.seen[-12:]:
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
        self.seen.append(n)
        del self.seen[:-40]
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

    tracker = SentenceTracker()
    offset0 = time.monotonic()
    prev_text = ""
    while True:
        time.sleep(POLL_SEC)
        try:
            text = caption_text(win)
        except Exception as e:
            print(f"  window read failed ({e}); retrying...")
            time.sleep(1)
            continue
        if text != prev_text:
            prev_text = text
            for sentence in tracker.feed(text):
                offset = round(time.monotonic() - offset0, 2)
                try:
                    r = requests.post(f"{args.url}/api/captions",
                                      json={"text": sentence, "offset": offset}, timeout=90)
                    ok, line = r.ok, (r.json() if r.ok else {})
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
