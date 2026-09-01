import json, struct, math, threading, urllib.request, urllib.error

HOST = "http://localhost:8000"

def post_json(path, value={}):
    req = urllib.request.Request(HOST + path, data=json.dumps(value).encode(), headers={"Content-Type": "application/json"}, method="POST")
    return json.load(urllib.request.urlopen(req, timeout=180))

def get_json(path):
    return json.load(urllib.request.urlopen(HOST + path, timeout=180))

assert get_json("/api/self-check")["ready"]
assert all(marker in urllib.request.urlopen(HOST + "/").read().decode() for marker in
           ["实时字幕", "字幕桥", "问 AI", "AI整理（保留原文）", "导出 Markdown"])
assert post_json("/api/translate", {"text": "Our next lecture covers chapter five."})["text"]
assert isinstance(post_json("/api/terms", {"text": "Photosynthesis converts light energy."})["terms"], list)

# captions pipeline: push a sentence like the bridge does, poll it back, and
# confirm it lands in the active session's persisted transcript.
before = len(get_json("/api/captions?since=0")["lines"])
sid = post_json("/api/sessions", {"title": "Captions", "language": "auto"})["id"]
assert post_json(f"/api/sessions/{sid}/activate") == {"active": sid}
import uuid
cap_text = f"Contract caption {uuid.uuid4().hex[:8]} for the pipeline test."
line = post_json("/api/captions", {"text": cap_text, "offset": 3.5})
assert line["translation"], "caption translation should be non-empty"
dup = post_json("/api/captions", {"text": cap_text, "offset": 4.0})
assert dup == {"duplicate": True}, "verbatim repeat must be rejected"
polled = get_json("/api/captions?since=0")
assert len(polled["lines"]) == before + 1 and polled["lines"][-1]["text"] == line["text"]
stored = get_json(f"/api/sessions/{sid}")["transcript"]
assert any(s["text"] == line["text"] and s["offset"] == 3.5 and s["translation"] for s in stored)
try:
    post_json("/api/captions", {"text": "   "})
    raise SystemExit("empty caption should 400")
except urllib.error.HTTPError as e:
    assert e.code == 400
try:
    post_json("/api/sessions/deadbeef/activate")
    raise SystemExit("unknown session should 404")
except urllib.error.HTTPError as e:
    assert e.code == 404

# storage management: snapshot lists the session; deletion removes it
snap = get_json("/api/storage")
assert any(s["id"] == sid for s in snap["sessions"]), "created session must appear in storage"
assert "captions_log" in snap and "total_size" in snap
req = urllib.request.Request(HOST + f"/api/sessions/{sid}", method="DELETE")
assert json.load(urllib.request.urlopen(req, timeout=60)) == {"deleted": sid}
snap = get_json("/api/storage")
assert not any(s["id"] == sid for s in snap["sessions"]), "deleted session must vanish"
try:
    get_json(f"/api/sessions/{sid}")
    raise SystemExit("deleted session should 404")
except urllib.error.HTTPError as e:
    assert e.code == 404
print("lecture translator contract: passed")
