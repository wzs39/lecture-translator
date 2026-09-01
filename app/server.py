"""
Real-time lecture translator — isolated container pipeline.

Pipeline: Windows Live Captions (host bridge, UIA) -> /api/captions
(translate via Ollama Qwen) -> subtitle page + session transcript.

Speech recognition is Windows' own Live Captions; all translation/AI compute
runs inside containers. A small host-side bridge (bridge/live_captions_bridge.py)
reads caption text via UI Automation and posts it here.
"""
import os
import time
import logging
import json
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("lt")

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:4b")
CONFIG = {"url": OLLAMA_URL, "model": OLLAMA_MODEL}
DATA_DIR = Path(os.environ.get("DATA_DIR", "/srv/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
app = FastAPI(title="lecture-translator")

# Live-captions text source: rolling buffer the page polls, plus the session
# new segments are persisted into.
CAPTIONS = []  # [{text, translation, offset, at}]
ACTIVE_SESSION = {"id": None}


class TranslateReq(BaseModel):
    text: str
    context: str = ""
    target: str = "Chinese (Simplified)"


class OrganizeReq(BaseModel):
    text: str
    target: str = "Chinese (Simplified)"


class AskReq(BaseModel):
    question: str
    context: str
    target: str = "Chinese (Simplified)"


class SessionReq(BaseModel):
    title: str = "Untitled lecture"
    language: str = "auto"


class ConfigReq(BaseModel):
    url: str = ""
    model: str = ""


@app.get("/api/config")
def get_config():
    return {"url": CONFIG["url"], "model": CONFIG["model"]}


@app.post("/api/config")
def set_config(req: ConfigReq):
    if req.url.strip():
        CONFIG["url"] = req.url.strip()
    if req.model.strip():
        CONFIG["model"] = req.model.strip()
    return {"url": CONFIG["url"], "model": CONFIG["model"]}


@app.post("/api/ask")
async def ask(req: AskReq):
    if not req.question.strip() or not req.context.strip():
        raise HTTPException(400, "question and context are required")
    prompt = ("只根据下面的课堂原文回答问题。无法从原文确定时明确说不知道，禁止编造。"
              f"用{req.target}回答，并引用相关原文。\n问题：{req.question}\n课堂原文：{req.context}")
    try:
        async with httpx.AsyncClient(timeout=120) as cx:
            r = await cx.post(f"{CONFIG['url']}/api/generate", json={"model": CONFIG["model"], "prompt": prompt, "stream": False, "think": False, "options": {"temperature": 0.1, "num_predict": 768}})
            r.raise_for_status()
            return {"text": r.json().get("response", "").strip(), "model": CONFIG["model"]}
    except Exception as e:
        raise HTTPException(502, f"question backend: {e}")


@app.post("/api/sessions")
def create_session(req: SessionReq):
    sid = uuid.uuid4().hex
    path = DATA_DIR / f"session-{sid}.json"
    path.write_text(json.dumps({"id": sid, "title": req.title.strip() or "Untitled lecture", "language": req.language, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "transcript": [], "notes": [], "terms": []}, ensure_ascii=False, indent=2), encoding="utf-8")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/api/sessions")
def sessions():
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(DATA_DIR.glob("session-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)]


def _session_path(sid: str) -> Path:
    if not sid.isalnum():
        raise HTTPException(404, "session not found")
    path = DATA_DIR / f"session-{sid}.json"
    if not path.is_file():
        raise HTTPException(404, "session not found")
    return path


@app.get("/api/sessions/{sid}")
def get_session(sid: str):
    return json.loads(_session_path(sid).read_text(encoding="utf-8"))


class SegmentReq(BaseModel):
    text: str
    offset: float = 0.0
    translation: str = ""


@app.post("/api/sessions/{sid}/segments")
def append_segment(sid: str, req: SegmentReq):
    """Append one committed transcription segment (original + translation)."""
    if not req.text.strip():
        raise HTTPException(400, "text is required")
    path = _session_path(sid)
    session = json.loads(path.read_text(encoding="utf-8"))
    session["transcript"].append({
        "text": req.text.strip(),
        "translation": req.translation.strip(),
        "offset": round(req.offset, 2),
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
    return session["transcript"][-1]


@app.post("/api/terms")
async def terms(req: OrganizeReq):
    if not req.text.strip():
        raise HTTPException(400, "text is required")
    prompt = f"从以下讲座原文提取最多20个重要专业术语，输出JSON数组，每项包含term和explanation，使用{req.target}解释，不要编造：\n{req.text}"
    try:
        async with httpx.AsyncClient(timeout=120) as cx:
            r = await cx.post(f"{CONFIG['url']}/api/generate", json={"model": CONFIG["model"], "prompt": prompt, "stream": False, "think": False, "format": "json", "options": {"temperature": 0.1, "num_predict": 1024}})
            r.raise_for_status()
            value = json.loads(r.json().get("response", "[]"))
            return {"terms": value if isinstance(value, list) else []}
    except Exception as e:
        raise HTTPException(502, f"terms backend: {e}")


@app.get("/api/self-check")
async def self_check():
    checks = {"service": "ok", "ollama": "down", "model": "unknown", "data_dir": DATA_DIR.is_dir()}
    try:
        async with httpx.AsyncClient(timeout=3) as cx:
            tags = await cx.get(f"{CONFIG['url']}/api/tags")
            checks["ollama"] = tags.status_code == 200
            if tags.is_success:
                names = {m.get("name") for m in tags.json().get("models", [])}
                checks["model"] = "ok" if CONFIG["model"] in names else "missing"
    except Exception as e:
        checks["ollama_error"] = str(e)
    checks["ready"] = checks["ollama"] is True and checks["model"] == "ok" and checks["data_dir"] is True
    return checks


@app.post("/api/organize")
async def organize(req: OrganizeReq):
    """Create notes without replacing the original transcript."""
    if not req.text.strip():
        raise HTTPException(400, "text is required")
    prompt = (
        f"整理以下讲座原文，输出{req.target}的结构化笔记。保留专业术语和关键事实，"
        "不要编造内容。输出：1.核心要点 2.术语 3.待确认问题。只输出整理结果。\n\n"
        + req.text
    )
    try:
        async with httpx.AsyncClient(timeout=120) as cx:
            r = await cx.post(f"{CONFIG['url']}/api/generate", json={
                "model": CONFIG["model"], "prompt": prompt, "stream": False,
                "think": False, "options": {"temperature": 0.2, "num_predict": 1024}
            })
            r.raise_for_status()
            return {"text": r.json().get("response", "").strip(), "model": CONFIG["model"]}
    except Exception as e:
        raise HTTPException(502, f"organization backend: {e}")





async def translate_text(text: str, target: str, context: str = "") -> str:
    prompt = (
        "You are a professional live subtitle translator for university lectures. "
        f"Translate into {target}.\n"
        + (f"Previous context:\n{context}\n" if context else "")
        + f"Current sentence:\n{text}\n"
        "Rules: preserve names, numbers, formulas, citations, and technical terms; "
        "translate naturally as a professional lecture interpreter, not word-for-word; "
        "do not add explanations or omit information; output ONLY the translation."
    )
    try:
        async with httpx.AsyncClient(timeout=60) as cx:
            r = await cx.post(
                f"{CONFIG['url']}/api/generate",
                json={
                    "model": CONFIG["model"],
                    "prompt": prompt,
                    "stream": False,
                    "think": False,
                    "options": {
                        "temperature": 0.2,
                        "num_predict": 512,
                        "stop": ["</think>", "\\n\\n"],
                    },
                },
            )
            r.raise_for_status()
            # qwen3 may leak chain-of-thought before the answer; take
            # everything after an explicit reasoning block if present.
            return r.json().get("response", "").split("</think>")[-1].strip()
    except Exception as e:
        log.exception("ollama failed")
        raise HTTPException(502, f"translation backend: {e}")


@app.post("/api/translate")
async def translate(req: TranslateReq):
    """Translate one sentence with context via local Ollama."""
    t0 = time.time()
    text = await translate_text(req.text, req.target, req.context)
    out = {"text": text, "model": CONFIG["model"], "seconds": round(time.time() - t0, 2)}
    log.info("translate: %s", out)
    return out


# ---- Windows Live Captions intake (bridge -> server -> page) ----

class CaptionReq(BaseModel):
    text: str
    offset: float = 0.0  # seconds since capture started, for the transcript timeline


@app.post("/api/captions")
async def push_caption(req: CaptionReq):
    """Receive one stabilized caption sentence from the host bridge,
    translate it, and store original + translation."""
    if not req.text.strip():
        raise HTTPException(400, "text is required")
    translation = await translate_text(req.text.strip(), "Chinese (Simplified)")
    line = {
        "text": req.text.strip(),
        "translation": translation,
        "offset": round(req.offset, 2),
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    CAPTIONS.append(line)
    del CAPTIONS[:-500]  # keep the poll buffer bounded
    sid = ACTIVE_SESSION["id"]
    if sid:
        try:
            session = json.loads((DATA_DIR / f"session-{sid}.json").read_text(encoding="utf-8"))
            session["transcript"].append(line)
            (DATA_DIR / f"session-{sid}.json").write_text(
                json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            log.warning("active session %s missing; caption kept in buffer only", sid)
    log.info("caption: %s", {k: line[k] for k in ("text", "translation", "offset")})
    return line


@app.get("/api/captions")
def poll_captions(since: int = 0):
    since = max(0, min(since, len(CAPTIONS)))
    return {"lines": CAPTIONS[since:], "next": len(CAPTIONS), "active_session": ACTIVE_SESSION["id"]}


@app.post("/api/sessions/{sid}/activate")
def activate_session(sid: str):
    _session_path(sid)  # 404 unless it exists
    ACTIVE_SESSION["id"] = sid
    return {"active": sid}


@app.get("/api/health")
async def health():
    ok_ollama = False
    try:
        async with httpx.AsyncClient(timeout=3) as cx:
            ok_ollama = (await cx.get(f"{CONFIG['url']}/api/tags")).status_code == 200
    except Exception:
        pass
    return {"asr_model": WHISPER_MODEL, "ollama": ok_ollama, "model": CONFIG["model"], "self_check": "/api/self-check"}


# ---- subtitle UI ----
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/index.html")

