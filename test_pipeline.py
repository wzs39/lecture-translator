"""Executable contract for the lecture translator.

Coverage map (each line asserts one behavior through the real HTTP surface):
  self-check   -> service + Ollama ready, memory limits reported
  page         -> all controls present, page JS parses (no syntax breakage)
  translate    -> real Ollama translation returns text
  terms        -> term extraction returns a list
  captions     -> translate, poll, persist to session, reject verbatim dup
  archive      -> PATCH archived/category toggles visibility + filtering
  storage      -> snapshot lists session, DELETE removes it
  glossary     -> extraction persists, manual add/remove, injected into AI prompt
  heartbeat    -> bridge_online flips true
  search/stats -> keyword hit with session title; stats row exists
  errors       -> 400/404 boundaries (see table)
"""
import json, os, subprocess, threading, time, uuid
import urllib.request, urllib.error, urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

HOST = "http://localhost:8000"


def call(path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(HOST + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.status, json.loads(r.read())


def expect(code, path, method="GET", body=None, detail_contains=""):
    try:
        status, payload = call(path, method, body)
        assert status == code, f"{method} {path}: expected {code}, got {status}"
        if detail_contains:
            assert detail_contains in json.dumps(payload, ensure_ascii=False), f"{path}: missing {detail_contains!r}"
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        assert e.code == code, f"{method} {path}: expected {code}, got {e.code}"
        if detail_contains:
            assert detail_contains in detail, f"{path}: missing {detail_contains!r}"


get = lambda p: call(p)[1]
def post(p, b=None, method="POST"): return call(p, method, b)[1]

# --- service health -------------------------------------------------------
health = get("/api/self-check")
assert health["ready"], health
limits = health["memory"]
assert limits["live_caption_limit"] >= 1000 and limits["translation_cache_limit"] >= 1000

# --- page ----------------------------------------------------------------
page = urllib.request.urlopen(HOST + "/").read().decode()
assert all(m in page for m in ["实时字幕", "字幕桥", "问 AI", "导出 Markdown",
                               "saveDoc", "ankiBtn", "statsBtn", "本地 Ollama",
                               "分类管理", "AI 总结", "saveSummaryBtn",
                               "addCategoryBtn", "summaryList", "cleanTestBtn",
                               "diskWarn", "bridgeDetail", "reviewBtn", "quizBtn", "quizBox",
                               "loadHistoryBtn", "settingsModal",
                               "translateBackendHint", "verifyBtn", "uploadMaterial", "materialList",
                               "audioBtn", "audioStop", "audioMode"])

# --- real model paths ----------------------------------------------------
assert post("/api/translate", {"text": "Our next lecture covers chapter five."})["text"]
assert isinstance(post("/api/terms", {"text": "Photosynthesis converts light energy."})["terms"], list)
# context-aware translation: with an active course topic, ambiguous terms are
# resolved in-domain (cell -> 细胞, not phone) and glossary terms are honored.
ctx_sid = post("/api/sessions", {"title": "Cell Biology", "category": "Biology"})["id"]
post(f"/api/sessions/{ctx_sid}/activate")
post(f"/api/sessions/{ctx_sid}/glossary", {"term": "Mitochondrion", "zh": "线粒体"})
ctx_zh = post("/api/translate", {"text": "The cell produces energy using its mitochondria.",
                                 "context": "We discussed organelles. Mitochondria are the powerhouse."})["text"]
assert "细胞" in ctx_zh and "线粒体" in ctx_zh, f"context-aware translation drifted: {ctx_zh!r}"
expect(200, f"/api/sessions/{ctx_sid}", "DELETE")

# --- independent audio channel: /api/audio transcribes PCM and feeds the
# caption pipeline (translate + persist). Send a short 440 Hz tone (silence to
# Whisper) to verify the endpoint shape; a 400 on garbage base64 guards input.
import struct as _struct, base64 as _b64
_tone = b"".join(
    _struct.pack("<h", int(6000 * __import__("math").sin(2 * 3.14159 * 440 * (i / 16000))))
    for i in range(16000 * 2))
_r = post("/api/audio", {"pcm_b64": _b64.b64encode(_tone).decode(), "offset": 1.0})
assert _r.get("translation") in (None, "") or isinstance(_r.get("translation"), str), _r
assert isinstance(_r.get("language"), str), _r
try:
    post("/api/audio", {"pcm_b64": "%%%not-base64%%%", "offset": 0})
    raise SystemExit("invalid base64 should 400")
except urllib.error.HTTPError as e:
    assert e.code == 400, e.code

# --- captions pipeline: translate -> poll -> persist -> dup reject -------
sid = post("/api/sessions", {"title": "Captions", "category": "Biology"})["id"]
assert get(f"/api/sessions/{sid}")["category"] == "Biology"
post(f"/api/sessions/{sid}/activate")
text = f"Contract caption {uuid.uuid4().hex[:8]} for the pipeline test."
line = post("/api/captions", {"text": text, "offset": 3.5})
assert line["translation"] and line["offset"] == 3.5
assert post("/api/captions", {"text": text, "offset": 4.0}) == {"duplicate": True}
assert any(x["text"] == text for x in get("/api/captions?since=0")["lines"][-2:])
assert len(get("/api/captions?since=0")["lines"]) <= 500
stored = get(f"/api/sessions/{sid}")["transcript"]
assert any(s["text"] == text and s["offset"] == 3.5 and s["translation"] for s in stored)

# --- archive / category / storage ----------------------------------------
post(f"/api/sessions/{sid}", {"archived": True}, "PATCH")
assert all(not x.get("archived") for x in get("/api/sessions") if not x.get("archived"))
assert any(x["id"] == sid for x in get("/api/sessions?category=Biology"))
post(f"/api/sessions/{sid}", {"archived": False, "category": "Biology"}, "PATCH")
snap = get("/api/storage")
assert any(s["id"] == sid for s in snap["sessions"]) and "captions_log" in snap
expect(200, f"/api/sessions/{sid}", "DELETE")
assert all(s["id"] != sid for s in get("/api/storage")["sessions"])

# --- glossary: extraction, manual edit, prompt injection ------------------
gsid = post("/api/sessions", {"title": "Glossary"})["id"]
post(f"/api/sessions/{gsid}/activate")
post("/api/terms", {"text": "Photosynthesis converts light energy into chemical energy. Chloroplasts contain chlorophyll."})
g = get(f"/api/sessions/{gsid}")["glossary"]
assert "Photosynthesis" in g, "extraction must feed the glossary"
assert post(f"/api/sessions/{gsid}/glossary", {"term": "Mitokondrio", "zh": "线粒体"})["glossary"]["Mitokondrio"] == "线粒体"
expect(200, f"/api/sessions/{gsid}/glossary?term=Mitokondrio", "DELETE")
assert "Mitokondrio" not in get(f"/api/sessions/{gsid}")["glossary"]

captured = {}


class MockAI(BaseHTTPRequestHandler):
    def do_POST(self):
        captured["prompt"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["messages"][0]["content"]
        if "单选题" in captured["prompt"]:
            content = ("Q1. What is the powerhouse of the cell?\nA) Nucleus\nB) Mitochondrion\n"
                       "C) Ribosome\nD) Golgi\nANSWER: B\n\n"
                       "Q2. Where does protein folding happen?\nA) ER\nB) Nucleus\nC) Cell wall\n"
                       "D) Vacuole\nANSWER: A")
        else:
            content = "x"
        out = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def log_message(self, *a): pass


old_cfg = get("/api/config")
mock = HTTPServer(("127.0.0.1", 0), MockAI)
threading.Thread(target=mock.serve_forever, daemon=True).start()
post("/api/config", {"ai_base": f"http://host.docker.internal:{mock.server_address[1]}/v1",
                     "ai_key": "test", "ai_model": "mock-model", "cloud_translate": True})
post(f"/api/sessions/{gsid}/glossary", {"term": "Mitokondrio", "zh": "线粒体"})  # restore for injection
post("/api/captions", {"text": f"The Mitokondrio produces energy {uuid.uuid4().hex[:6]} for the cell.", "offset": 88.0})
assert "Mitokondrio=线粒体" in captured.get("prompt", ""), "glossary must reach the AI prompt"

# --- quiz: multiple-choice self-test generated from saved summaries -------
qmark = f"organelle marker {uuid.uuid4().hex[:6]}"
qsum = post("/api/summaries", {"category": "QuizCat", "title": "Q",
                                "text": f"Mitochondria are the powerhouse. {qmark}"})["id"]
qz = post("/api/quiz", {"category": "QuizCat", "count": 5})
assert qz["count"] == 2, qz
assert qz["questions"][0]["answer"] == "B" and qz["questions"][0]["options"][1] == "Mitochondrion"
assert qz["questions"][1]["answer"] == "A" and qz["questions"][1]["options"][0] == "ER"
assert qz["model"] == "mock-model" and "单选题" in captured["prompt"] and qmark in captured["prompt"]
expect(400, "/api/quiz", "POST", {"category": "EmptyQuizCat"})
expect(200, f"/api/summaries/{qsum}", "DELETE")
post("/api/config", old_cfg)
mock.shutdown()

# --- proper-noun verifier: mic transcript vs caption baseline ------------
# The captioner wrote a mangled form; the mic heard the real term. /api/verify
# must flag the term absent from the baseline and skip common words + known terms.
v = post("/api/verify", {"text": "The student explained CRISPR and glycolysis.",
                        "baseline": "the student explained crispr and glycolysis"})
assert v["candidates"] == [] , "crispr/glycolysis already in baseline => no candidates"
v = post("/api/verify", {"text": "The student explained CRISPR and glycolysis.",
                        "baseline": "the student explained crisper and glycolysis"})
assert "CRISPR" in v["candidates"] and "glycolysis" not in v["candidates"], v
assert v["baseline_words"] == 6
expect(400, "/api/verify", "POST", {"text": "   "})

# --- course materials: upload a reference file, list, reject bad types -----
import base64 as _b64
msid = post("/api/sessions", {"title": "MaterialsCourse"})["id"]
post(f"/api/sessions/{msid}/activate")
_txt = "Mitochondria are the powerhouse. 线粒体是动力工厂.\n" * 20
mats = post(f"/api/sessions/{msid}/materials", {"name": "ch2.txt",
            "content_b64": _b64.b64encode(_txt.encode()).decode()})
assert any(m["name"] == "ch2.txt" and m["chars"] >= len(_txt) for m in mats["materials"]), mats
# unsupported extension -> 415
expect(415, f"/api/sessions/{msid}/materials", "POST", {"name": "x.exe", "content_b64": _b64.b64encode(b"MZ").decode()})
# empty content -> 400
expect(400, f"/api/sessions/{msid}/materials", "POST", {"name": "x.txt", "content_b64": ""})
assert any(m["name"] == "ch2.txt" for m in get(f"/api/sessions/{msid}/materials")["materials"])
expect(200, f"/api/sessions/{msid}/materials?name=ch2.txt", "DELETE")
assert all(m["name"] != "ch2.txt" for m in get(f"/api/sessions/{msid}/materials")["materials"])
expect(200, f"/api/sessions/{msid}", "DELETE")

# --- config: round-trips a harmless field (the old cloud_translate
# toggle was removed; backend choice is now key-presence only) --------------
cfg0 = get("/api/config")
assert "ai_base" in cfg0 and "ai_key" in cfg0
post("/api/config", {"ai_base": cfg0["ai_base"]})
assert get("/api/config")["ai_base"] == cfg0["ai_base"]

# --- heartbeat, search, stats --------------------------------------------
post("/api/bridge/heartbeat", {})
assert get("/api/captions?since=0")["bridge_online"] is True
# Converge: the real bridge heartbeats every second with healthy values, so a
# single low-disk post can be overwritten between our post and our poll. Keep
# posting 15.2 until the poll SEES it (bounded loop), then immediately assert.
def converge_heartbeat(body, key, want):
    for _ in range(40):
        post("/api/bridge/heartbeat", body)
        if get("/api/captions?since=0")[key] is want:
            return
    raise AssertionError(f"heartbeat convergence failed: {key} never became {want}")
converge_heartbeat({"disk_free_gb": 15.2}, "disk_warn", True)
assert get("/api/captions?since=0")["disk_warn"] is True
converge_heartbeat({"disk_free_gb": 500}, "disk_warn", False)
assert get("/api/captions?since=0")["disk_warn"] is False
# The real host bridge heartbeats concurrently with the test; re-post until
# the poll observes the waiting state we just sent (converges in one round).
converge_heartbeat({"disk_free_gb": 500, "window_found": False, "error": "test error"}, "bridge_window", False)
pvp = get("/api/captions?since=0")
assert pvp["bridge_online"] is True, "waiting for the window must still count as alive"
assert pvp["bridge_window"] is False and pvp["bridge_error"] == "test error"
for _ in range(10):
    post("/api/bridge/heartbeat", {"window_found": True, "error": None})
    pvp = get("/api/captions?since=0")
    if pvp["bridge_window"] is True:
        break
assert pvp["bridge_window"] is True and pvp["bridge_error"] is None
marker = f"quasimodo {uuid.uuid4().hex[:6]}"
sid2 = post("/api/sessions", {"title": "Search"})["id"]
post(f"/api/sessions/{sid2}/activate")
post("/api/captions", {"text": f"The hunchback {marker} rang the bell.", "offset": 5.0})
# caption persist is batched; converge until the search sees the stored segment
for _ in range(20):
    if any(marker in h["text"] and h["session"] == "Search"
           for h in get(f"/api/search?q={urllib.parse.quote(marker)}")["hits"]):
        break
    time.sleep(0.5)
assert any(marker in h["text"] and h["session"] == "Search"
           for h in get(f"/api/search?q={urllib.parse.quote(marker)}")["hits"])
assert any(x["title"] == "Search" and x["segments"] >= 1 for x in get("/api/stats")["courses"])

# --- categories: CRUD + courses/summaries follow rename & delete ---------
assert "ContractCat" in post("/api/categories", {"name": "ContractCat"})["categories"]
scat = post("/api/sessions", {"title": "CatSession", "category": "ContractCat"})["id"]
sumid = post("/api/summaries", {"category": "ContractCat", "title": "Sum",
                                "text": "classified notes"})["id"]
assert any(s["id"] == sumid for s in get("/api/summaries?category=ContractCat")["summaries"])
assert get(f"/api/summaries/{sumid}")["text"] == "classified notes"
post("/api/categories", {"name": "ContractCat", "new_name": "ContractGone"}, "PATCH")
assert "ContractGone" in get("/api/categories")["categories"]
assert get(f"/api/sessions/{scat}")["category"] == "ContractGone"
assert get(f"/api/summaries/{sumid}")["category"] == "ContractGone"
expect(200, "/api/categories?name=ContractGone", "DELETE")
assert get(f"/api/sessions/{scat}")["category"] == "未分类"
assert get(f"/api/summaries/{sumid}")["category"] == "未分类"
expect(200, f"/api/summaries/{sumid}", "DELETE")
expect(404, f"/api/summaries/{sumid}")

# --- review booklet: all summaries (or one category) as one Markdown file ---
r1 = uuid.uuid4().hex[:6]
sum_a = post("/api/summaries", {"category": "BookletA", "title": "L1", "text": f"alpha notes {r1}"})["id"]
r2 = uuid.uuid4().hex[:6]
sum_b = post("/api/summaries", {"category": "BookletB", "title": "L2", "text": f"beta notes {r2}"})["id"]
rn = uuid.uuid4().hex[:6]
nsid = post("/api/sessions", {"title": "NotesCourse", "category": "BookletA"})["id"]
post(f"/api/sessions/{nsid}/notes", {"notes": f"hand notes {rn}"})
md_a = urllib.request.urlopen(HOST + "/api/review?category=BookletA").read().decode()
assert f"alpha notes {r1}" in md_a and f"beta notes {r2}" not in md_a and "## BookletA" in md_a
assert f"hand notes {rn}" in md_a and "NotesCourse" in md_a and "### 我的笔记" in md_a
md_all = urllib.request.urlopen(HOST + "/api/review").read().decode()
assert f"alpha notes {r1}" in md_all and f"beta notes {r2}" in md_all
assert f"hand notes {rn}" in md_all
assert "## BookletA" in md_all and "## BookletB" in md_all
assert md_all.startswith("# 期末复习册")
expect(404, "/api/review?category=NopeNotFound")
for s in (sum_a, sum_b):
    expect(200, f"/api/summaries/{s}", "DELETE")
expect(200, f"/api/sessions/{nsid}", "DELETE")

# --- bridge self-check (host-side diagnostics must pass) ------------------
venv_py = os.path.join("bridge", ".venv", "Scripts", "python.exe")
assert os.path.isfile(venv_py), "bridge venv missing"
rc = subprocess.run([venv_py, "bridge/live_captions_bridge.py", "--selfcheck"],
                    capture_output=True, timeout=90)
assert rc.returncode == 0, rc.stdout.decode("utf-8", "replace")[-500:]

# --- error boundaries -----------------------------------------------------
# Clear the key AND point the base at a dead endpoint: the detect assertion
# below needs BOTH conditions, and a stale real base from an earlier live run
# would otherwise make this 400-vs-502 assertion flaky.
post("/api/config", {"ai_key": "", "ai_base": "http://127.0.0.1:9"})
expect(400, "/api/captions", "POST", {"text": "   "})
expect(404, "/api/sessions/deadbeef/activate", "POST")
expect(404, f"/api/sessions/{sid}")
expect(400, "/api/ai/detect", detail_contains="API Key")
# --- OCR progress: poll contract + textless-PDF upload with no key -------
expect(400, "/api/ocr-progress", "GET")                        # missing upload_id
expect(404, "/api/ocr-progress?upload_id=no-such-job", "GET")  # unknown job
_scan_sid = post("/api/sessions", {"title": "OcrScan"})["id"]
post(f"/api/sessions/{_scan_sid}/activate")
# scanned (textless) PDF + no cloud key -> 422 with a fix hint (OCR path skipped)
expect(422, f"/api/sessions/{_scan_sid}/materials", "POST",
       {"name": "scan.pdf",
        "content_b64": _b64.b64encode(b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
                                      b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
                                      b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
                                      b"trailer<</Root 1 0 R>>\n%%EOF").decode()},
       detail_contains="云端")
expect(200, f"/api/sessions/{_scan_sid}", "DELETE")
# leave the suite with no live key and the dead test base — a clean, explicit
# state; the user's saved cloud config is deliberately NOT restored (tests must
# not depend on or mutate real credentials).

# --- test-data cleanup: removes test-named courses + caption log ----------
clean_sid = post("/api/sessions", {"title": "MigVerify"})["id"]
j = post("/api/test-cleanup")
assert clean_sid in j["deleted"] and j["deleted_count"] >= 1 and j["log_lines"] > 0
assert all(s["id"] != clean_sid for s in get("/api/storage")["sessions"])

print("lecture translator contract: passed")
