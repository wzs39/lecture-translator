"""
Real-time lecture translator — isolated container pipeline.

Pipeline: browser mic/system-audio capture -> /api/transcribe (faster-whisper,
language auto-detect) -> /api/translate (Ollama Qwen) -> floating subtitle page.

All compute runs inside containers; the host only runs a browser tab.
"""
import os
import time
import logging
import threading
import json
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("lt")

WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:4b")
CONFIG = {"url": OLLAMA_URL, "model": OLLAMA_MODEL}
DATA_DIR = Path(os.environ.get("DATA_DIR", "/srv/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
app = FastAPI(title="lecture-translator")

# ---- models (loaded lazily so the API answers immediately) ----
_asr = None
_asr_lock = threading.Lock()  # ctranslate2 inference is not thread-safe


def asr():
    global _asr
    if _asr is None:
        from faster_whisper import WhisperModel
        t0 = time.time()
        log.info("loading whisper model %s on cuda ...", WHISPER_MODEL)
        _asr = WhisperModel(WHISPER_MODEL, device="cuda", compute_type="float16")
        log.info("whisper ready in %.1fs", time.time() - t0)
    return _asr


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


@app.post("/api/save")
async def save_recording(audio: UploadFile = File(...), transcript: str = Form(""), language: str = Form("auto")):
    """Persist audio and JSON metadata; original transcript is never overwritten."""
    rid = uuid.uuid4().hex
    suffix = ".wav" if (audio.filename or "").lower().endswith(".wav") else ".webm"
    audio_path = DATA_DIR / f"{rid}{suffix}"
    meta_path = DATA_DIR / f"{rid}.json"
    audio_data = await audio.read()
    if not audio_data:
        raise HTTPException(400, "empty audio")
    if len(audio_data) > 200 * 1024 * 1024:
        raise HTTPException(413, "recording too large")
    audio_path.write_bytes(audio_data)
    meta_path.write_text(json.dumps({"id": rid, "audio": audio_path.name,
        "transcript": transcript, "language": language,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"id": rid, "audio": audio_path.name, "metadata": meta_path.name}


@app.get("/api/records")
def records():
    result = []
    for p in sorted(DATA_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try: result.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError): pass
    return result[:100]


@app.get("/api/records/{record_id}/{kind}")
def record_file(record_id: str, kind: str):
    if kind not in {"audio", "json"}:
        raise HTTPException(400, "invalid file kind")
    if not record_id.isalnum():
        raise HTTPException(404, "record not found")
    if kind == "json":
        path = DATA_DIR / f"{record_id}.json"
    else:
        path = next((p for p in DATA_DIR.glob(f"{record_id}.*") if p.suffix in {".wav", ".webm"}), None)
    if not path or not path.is_file():
        raise HTTPException(404, "record not found")
    return FileResponse(path)


@app.post("/api/transcribe")
def transcribe(file: UploadFile = File(...), language: str = Form("")):
    """WAV 16kHz mono (webm/ogg also OK via ffmpeg decode). Returns text + lang.

    language: empty, "fi", or "en" ("auto" falls back to detection).
    Sync endpoint on purpose: FastAPI runs it in a thread pool, so several
    audio chunks can queue for ASR without blocking /api/translate or the
    event loop. Fast speech therefore buffers instead of being dropped.
    """
    if language not in {"", "auto", "fi", "en", "zh"}:
        raise HTTPException(400, "language must be auto, fi, en, or zh")
    data = file.file.read()
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(413, "audio chunk too large")
    if not data:
        raise HTTPException(400, "empty audio")
    t0 = time.time()
    with _asr_lock:
        try:
            segments, info = asr().transcribe(
                io.BytesIO(data),
                beam_size=5,
                language=language if language not in {"", "auto"} else None,
                condition_on_previous_text=False,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 300},
            )
            # segments is a lazy generator: consume it while holding the lock
            text = " ".join(s.text.strip() for s in segments).strip()
        except ValueError:
            # Whisper raises when a chunk is pure silence and no language
            # candidate survives VAD; that is silence, not an error.
            return {"text": "", "language": language or "auto", "audio_duration": round(len(data) / 32000, 2)}
    out = {
        "text": text,
        "language": info.language,
        "language_probability": round(float(info.language_probability), 3),
        "audio_duration": round(float(info.duration), 2),
        "asr_seconds": round(time.time() - t0, 2),
    }
    log.info("asr: %s", out)
    return out


@app.post("/api/translate")
async def translate(req: TranslateReq):
    """Translate one sentence with context via local Ollama."""
    prompt = (
        "You are a professional live subtitle translator for university lectures. "
        f"Translate into {req.target}.\n"
        + (f"Previous context:\n{req.context}\n" if req.context else "")
        + f"Current sentence:\n{req.text}\n"
        "Rules: preserve names, numbers, formulas, citations, and technical terms; "
        "translate naturally as a professional lecture interpreter, not word-for-word; "
        "do not add explanations or omit information; output ONLY the translation."
    )
    t0 = time.time()
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
            raw = r.json().get("response", "").strip()
            # qwen3 may leak chain-of-thought before the answer; take
            # everything after an explicit reasoning block if present.
            text = raw.split("</think>")[-1].strip()
    except Exception as e:
        log.exception("ollama failed")
        raise HTTPException(502, f"translation backend: {e}")
    out = {"text": text, "model": CONFIG["model"], "seconds": round(time.time() - t0, 2)}
    log.info("translate: %s", out)
    return out


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


@app.get("/audio-worklet.js")
async def audio_worklet():
    return FileResponse("static/audio-worklet.js", media_type="application/javascript")


import io  # noqa: E402  (used inside transcribe)

