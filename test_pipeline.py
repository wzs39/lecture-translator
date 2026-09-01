import json, struct, math, threading, urllib.request, urllib.error, urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

HOST = "http://localhost:8000"

def post_json(path, value={}):
    req = urllib.request.Request(HOST + path, data=json.dumps(value).encode(), headers={"Content-Type": "application/json"}, method="POST")
    return json.load(urllib.request.urlopen(req, timeout=180))

def get_json(path):
    return json.load(urllib.request.urlopen(HOST + path, timeout=180))

assert get_json("/api/self-check")["ready"]
page = urllib.request.urlopen(HOST + "/").read().decode()
assert all(marker in page for marker in
           ["实时字幕", "字幕桥", "问 AI", "AI整理（保留原文）", "导出 Markdown", "saveDoc", "ankiBtn", "statsBtn"])
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

# ---- glossary: extraction persists, manual add/remove, prompt injection ----
old_cfg = get_json("/api/config")
gsid = post_json("/api/sessions", {"title": "Glossary"})["id"]
post_json(f"/api/sessions/{gsid}/activate")
post_json("/api/terms", {"text": "Photosynthesis converts light energy into chemical energy. Chloroplasts contain chlorophyll."})
glossary = get_json(f"/api/sessions/{gsid}")["glossary"]
assert "Photosynthesis" in glossary, "extraction must feed the glossary"
g2 = post_json(f"/api/sessions/{gsid}/glossary", {"term": "Mitokondrio", "zh": "线粒体"})["glossary"]
assert g2["Mitokondrio"] == "线粒体"
req = urllib.request.Request(HOST + f"/api/sessions/{gsid}/glossary?term=Mitokondrio", method="DELETE")
assert "Mitokondrio" not in json.load(urllib.request.urlopen(req, timeout=30))["glossary"]
post_json(f"/api/sessions/{gsid}/glossary", {"term": "Mitokondrio", "zh": "线粒体"})  # restore for injection check

# prompt injection, verified against a local mock OpenAI-compatible server
captured = {}
class MockAI(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        captured["prompt"] = json.loads(body)["messages"][0]["content"]
        out = json.dumps({"choices": [{"message": {"content": "线粒体是细胞的能量工厂。"}}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def log_message(self, *a): pass
mock = HTTPServer(("127.0.0.1", 0), MockAI)
threading.Thread(target=mock.serve_forever, daemon=True).start()
mock_port = mock.server_address[1]
post_json("/api/config", {"ai_base": f"http://host.docker.internal:{mock_port}/v1", "ai_key": "test", "ai_model": "mock-model"})
post_json("/api/captions", {"text": f"The Mitokondrio produces energy {uuid.uuid4().hex[:6]} for the cell.", "offset": 88.0})
assert "Mitokondrio=线粒体" in captured.get("prompt", ""), f"glossary not injected: {captured.get('prompt','')[:200]}"
post_json("/api/config", {"ai_base": old_cfg["ai_base"], "ai_key": old_cfg["ai_key"], "ai_model": old_cfg["ai_model"]})

# heartbeat + cross-course search
post_json("/api/bridge/heartbeat", {})
assert get_json("/api/captions?since=0")["bridge_online"] is True
marker = f"quasimodo {uuid.uuid4().hex[:6]}"
sid2 = post_json("/api/sessions", {"title": "Search"})["id"]
post_json(f"/api/sessions/{sid2}/activate")
post_json("/api/captions", {"text": f"The hunchback {marker} rang the bell.", "offset": 5.0})
hits = get_json(f"/api/search?q={urllib.parse.quote(marker)}")["hits"]
assert any(h["text"] == f"The hunchback {marker} rang the bell." and h["session"] == "Search" for h in hits)
# Study statistics is a real read path for persisted courses.
stats = get_json("/api/stats")
assert any(x["title"] == "Search" and x["segments"] >= 1 for x in stats["courses"])

# Model detection: missing key is a client error, invalid credentials are auth errors.
post_json("/api/config", {"ai_key": "", "ai_base": "http://127.0.0.1:9"})
try:
    get_json("/api/ai/detect")
    raise SystemExit("missing API key should 400")
except urllib.error.HTTPError as e:
    assert e.code == 400 and "API Key" in e.read().decode()

print("lecture translator contract: passed")
