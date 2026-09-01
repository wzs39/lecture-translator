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
import asyncio
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
CONFIG = {"url": OLLAMA_URL, "model": OLLAMA_MODEL,
          "ai_base": "https://api.deepseek.com", "ai_key": "", "ai_model": "deepseek-chat",
          "access_token": os.environ.get("ACCESS_TOKEN", "")}


app = FastAPI(title="lecture-translator")


@app.middleware("http")
async def access_control(request, call_next):
    """When an access token is configured (set ACCESS_TOKEN env or via
    /api/config), every /api/* request must carry it — header or query —
    so the page can be shared beyond localhost without exposing it."""
    token = CONFIG["access_token"]
    if token and request.url.path.startswith("/api/"):
        supplied = request.headers.get("x-access-token") or request.query_params.get("token")
        if supplied != token:
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)
DATA_DIR = Path(os.environ.get("DATA_DIR", "/srv/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Live-captions text source: rolling buffer the page polls, plus the session
# new segments are persisted into.
CAPTIONS = []  # [{text, translation, offset, at}]
ACTIVE_SESSION = {"id": None}
BRIDGE_LAST_SEEN = 0.0  # monotonic timestamp of the bridge's last heartbeat
CAPTIONS_MAX = 500  # live polling only; persisted transcripts are never pruned
TRANSLATION_CACHE_MAX = 500


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
    category: str = "未分类"


class ConfigReq(BaseModel):
    url: str = ""
    model: str = ""
    ai_base: str = ""
    ai_key: str | None = None  # None = unchanged; "" = clear
    ai_model: str = ""
    deepseek_key: str = ""  # legacy field name, merged into ai_key
    access_token: str | None = None  # None = unchanged; "" = clear


@app.get("/api/config")
def get_config():
    return {k: CONFIG[k] for k in ("url", "model", "ai_base", "ai_key", "ai_model")}


@app.get("/api/stats")
def stats():
    """Per-course study statistics."""
    out = []
    for p in DATA_DIR.glob("session-*.json"):
        try:
            s = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        tr = s.get("transcript", [])
        out.append({"title": s.get("title", ""), "created": s.get("created_at", ""),
                    "segments": len(tr),
                    "words": sum(len(t.get("text", "").split()) for t in tr),
                    "minutes": round(max((t.get("offset") or 0) for t in tr), 1) / 60 if tr else 0,
                    "terms": len(s.get("glossary", {}) or {}),
                    "notes": len(s.get("notes") or "")})
    return {"courses": sorted(out, key=lambda x: x["created"], reverse=True)}


@app.post("/api/config")
def set_config(req: ConfigReq):
    if req.url.strip():
        CONFIG["url"] = req.url.strip()
    if req.model.strip():
        CONFIG["model"] = req.model.strip()
    if req.ai_base.strip():
        CONFIG["ai_base"] = req.ai_base.strip().rstrip("/")
    if req.ai_key is not None:  # explicit empty string clears the key
        CONFIG["ai_key"] = req.ai_key.strip()
    if req.ai_model.strip():
        CONFIG["ai_model"] = req.ai_model.strip()
    if req.deepseek_key.strip():  # legacy clients
        CONFIG["ai_key"] = req.deepseek_key.strip()
    if req.access_token is not None:  # explicit empty string clears it
        CONFIG["access_token"] = req.access_token.strip()
    return {k: CONFIG[k] for k in ("url", "model", "ai_base", "ai_key", "ai_model")}


# ---- token economy for the cloud AI ----
ASK_CONTEXT_CHARS = 4000     # window of lecture text sent with questions
LONGTEXT_HEAD = 2000         # organize/terms: keep the head...
LONGTEXT_TAIL = 10000        # ...and the tail of over-long input
TRANSLATE_CONTEXT_CHARS = 400

_translation_cache = {}  # norm(text)+target+ctx-hash -> (translation, backend)


def _cache_key(text, target, context):
    norm = "".join(ch for ch in text.lower() if ch.isalnum())
    ctx = str(hash("".join(ch for ch in (context or "").lower() if ch.isalnum())) % 100000)
    return f"{norm}|{target}|{ctx}"


def cache_get(key):
    hit = _translation_cache.get(key)
    if hit is not None:  # refresh LRU order
        _translation_cache[key] = hit
    return hit


def cache_put(key, value):
    _translation_cache[key] = value
    while len(_translation_cache) > TRANSLATION_CACHE_MAX:
        _translation_cache.pop(next(iter(_translation_cache)))


def cap_long_text(text):
    """Bound token usage for whole-lecture inputs: keep head+tail."""
    if len(text) <= LONGTEXT_HEAD + LONGTEXT_TAIL:
        return text
    return text[:LONGTEXT_HEAD] + "\n[...中间内容省略...]\n" + text[-LONGTEXT_TAIL:]


@app.get("/api/ai/detect")
async def detect_models():
    """Auto-detect available models from the configured cloud AI service."""
    if not CONFIG["ai_key"].strip():
        raise HTTPException(status_code=400, detail="请先填写云端 AI API Key")
    if not CONFIG["ai_base"].strip():
        raise HTTPException(status_code=400, detail="请先填写云端 AI 接口地址")
    try:
        async with httpx.AsyncClient(timeout=10) as cx:
            r = await cx.get(f"{CONFIG['ai_base']}/models",
                             headers={"Authorization": f"Bearer {CONFIG['ai_key']}"})
            r.raise_for_status()
            ids = [m.get("id") for m in r.json().get("data", []) if m.get("id")]
    except httpx.HTTPStatusError as e:
        detail = f"模型列表请求失败：HTTP {e.response.status_code}"
        raise HTTPException(status_code=401 if e.response.status_code in (401, 403) else 502, detail=detail)
    except (httpx.RequestError, ValueError, KeyError) as e:
        raise HTTPException(status_code=502, detail=f"识别失败（检查接口地址和 Key）：{e}")
    prefer = ("chat", "flash", "mini", "turbo", "air")
    suggested = next((i for i in ids if any(k in i.lower() for k in prefer)), ids[0] if ids else "")
    return {"models": ids, "suggested": suggested, "count": len(ids)}


class NotesReq(BaseModel):
    notes: str = ""


class GlossaryReq(BaseModel):
    term: str
    zh: str


@app.post("/api/sessions/{sid}/glossary")
def add_glossary_entry(sid: str, req: GlossaryReq):
    if not req.term.strip() or not req.zh.strip():
        raise HTTPException(400, "term and zh are required")
    path = _session_path(sid)
    session = json.loads(path.read_text(encoding="utf-8"))
    glossary = session.setdefault("glossary", {})
    glossary[req.term.strip()] = req.zh.strip()
    path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"glossary": glossary}


@app.delete("/api/sessions/{sid}/glossary")
def remove_glossary_entry(sid: str, term: str = ""):
    path = _session_path(sid)
    session = json.loads(path.read_text(encoding="utf-8"))
    session.setdefault("glossary", {}).pop(term, None)
    path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"glossary": session.get("glossary", {})}


def _active_glossary() -> dict:
    sid = ACTIVE_SESSION["id"]
    if not sid:
        return {}
    try:
        s = json.loads((DATA_DIR / f"session-{sid}.json").read_text(encoding="utf-8"))
        g = s.get("glossary", {})
        return g if isinstance(g, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _glossary_block() -> str:
    """Short prompt fragment (≤50 lines) forcing consistent term renderings."""
    g = _active_glossary()
    if not g:
        return ""
    lines = "\n".join(f"{t}={zh}" for t, zh in list(g.items())[:50])
    return f"\nCourse glossary (use these EXACT renderings):\n{lines}\n"


@app.post("/api/sessions/{sid}/notes")
def save_session_notes(sid: str, req: NotesReq):
    """Save the student's own notes; AI output never overwrites them."""
    path = _session_path(sid)
    session = json.loads(path.read_text(encoding="utf-8"))
    session["notes"] = req.notes
    path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"saved": True, "chars": len(req.notes)}


async def ai_complete(prompt: str, json_mode: bool = False, timeout_s: int = 120):
    """AI Q&A backend: any OpenAI-compatible web AI when an API key is
    configured (DeepSeek, GLM-4-Flash free tier, Groq, OpenRouter, ...),
    local Ollama otherwise. Returns (text, backend)."""
    key = CONFIG["ai_key"]
    if key:
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as cx:
                body = {"model": CONFIG["ai_model"],
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.3, "max_tokens": 512, "stream": False}
                if json_mode:
                    body["response_format"] = {"type": "json_object"}
                r = await cx.post(f"{CONFIG['ai_base']}/chat/completions",
                                  headers={"Authorization": f"Bearer {key}"}, json=body)
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"].strip(), CONFIG["ai_model"]
        except Exception as e:
            log.warning("cloud AI failed, falling back to ollama: %s", e)
    async with httpx.AsyncClient(timeout=120) as cx:
        body = {"model": CONFIG["model"], "prompt": prompt, "stream": False,
                "think": False, "options": {"temperature": 0.2, "num_predict": 1024}}
        if json_mode:
            body["format"] = "json"
        r = await cx.post(f"{CONFIG['url']}/api/generate", json=body)
        r.raise_for_status()
        # qwen3 may leak chain-of-thought before the answer
        return r.json().get("response", "").split("</think>")[-1].strip(), "ollama"


@app.post("/api/ask")
async def ask(req: AskReq):
    if not req.question.strip() or not req.context.strip():
        raise HTTPException(400, "question and context are required")
    context = req.context.strip()
    if len(context) > ASK_CONTEXT_CHARS:  # recent window, not the whole lecture
        context = "[...较早内容省略...]" + context[-ASK_CONTEXT_CHARS:]
    prompt = ("只根据下面的课堂原文回答问题。无法从原文确定时明确说不知道，禁止编造。"
              f"用{req.target}回答，并引用相关原文。\n问题：{req.question}\n课堂原文：{context}"
              + _glossary_block())
    try:
        text, backend = await ai_complete(prompt)
        return {"text": text, "model": backend}
    except Exception as e:
        raise HTTPException(502, f"question backend: {e}")


@app.post("/api/sessions")
def create_session(req: SessionReq):
    sid = uuid.uuid4().hex
    path = DATA_DIR / f"session-{sid}.json"
    path.write_text(json.dumps({"id": sid, "title": req.title.strip() or "Untitled lecture", "language": req.language, "category": req.category.strip() or "未分类", "archived": False, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "transcript": [], "notes": "", "glossary": {}, "terms": []}, ensure_ascii=False, indent=2), encoding="utf-8")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/api/sessions")
def sessions(category: str = ""):
    items = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(DATA_DIR.glob("session-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)]
    return [s for s in items if not category or s.get("category", "未分类") == category]


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


@app.post("/api/terms")
async def terms(req: OrganizeReq):
    if not req.text.strip():
        raise HTTPException(400, "text is required")
    # qwen in JSON mode stubbornly emits a single object, so ask for the
    # object shape {"terms": [...]} instead of a bare array
    prompt = (f"从以下讲座原文提取所有重要专业术语（最多20个）。"
              f"输出一个JSON对象：{{\"terms\": [{{\"term\": \"英文术语\", \"explanation\": \"用{req.target}给出的中文解释\"}}]}}。"
              f"每个术语都要包含，不要编造。\n原文：\n{cap_long_text(req.text)}")
    try:
        text, _ = await ai_complete(prompt, json_mode=True)
        value = json.loads(text)
        if isinstance(value, list):  # a bare array is fine too
            value = {"terms": value}
        terms = value.get("terms", []) if isinstance(value, dict) else []
        if isinstance(terms, dict):
            terms = [terms]
        terms = [t for t in terms if isinstance(t, dict) and t.get("term")]
        # extraction feeds the course glossary so future translations in
        # this course render the same terms identically
        sid = ACTIVE_SESSION["id"]
        if sid and terms:
            try:
                spath = _session_path(sid)
                session = json.loads(spath.read_text(encoding="utf-8"))
                glossary = session.setdefault("glossary", {})
                for t in terms:
                    glossary.setdefault(t["term"], (t.get("explanation") or "").strip())
                spath.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
            except (OSError, json.JSONDecodeError):
                log.warning("glossary merge failed for session %s", sid)
        return {"terms": terms}
    except Exception as e:
        raise HTTPException(502, f"terms backend: {e}")


# ---- storage management ----

def _storage_snapshot():
    sessions = []
    for p in DATA_DIR.glob("session-*.json"):
        try:
            s = json.loads(p.read_text(encoding="utf-8"))
            sessions.append({"id": s["id"], "title": s.get("title", ""),
                             "category": s.get("category", "未分类"),
                             "archived": bool(s.get("archived", False)),
                             "created": s.get("created_at", ""),
                             "segments": len(s.get("transcript", [])),
                             "size": p.stat().st_size})
        except (OSError, json.JSONDecodeError):
            continue
    log = DATA_DIR / "captions-log.jsonl"
    log_info = {"lines": 0, "size": 0}
    if log.is_file():
        log_info["size"] = log.stat().st_size
        with log.open(encoding="utf-8") as f:
            log_info["lines"] = sum(1 for _ in f)
    return {"sessions": sorted(sessions, key=lambda x: x["created"], reverse=True),
            "captions_log": log_info,
            "total_size": sum(s["size"] for s in sessions) + log_info["size"],
            "retention": {"live_caption_buffer_max": CAPTIONS_MAX,
                          "translation_cache_max": TRANSLATION_CACHE_MAX,
                          "persisted_transcripts": "unlimited"}}


@app.get("/api/storage")
def storage_info():
    return _storage_snapshot()


@app.delete("/api/sessions/{sid}")
def delete_session(sid: str):
    path = _session_path(sid)
    if ACTIVE_SESSION["id"] == sid:
        ACTIVE_SESSION["id"] = None
    path.unlink()
    return {"deleted": sid}


@app.delete("/api/captions-log")
def clear_captions_log():
    log = DATA_DIR / "captions-log.jsonl"
    if log.is_file():
        log.unlink()
    return {"cleared": True}


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
    checks["memory"] = {"live_captions": len(CAPTIONS), "live_caption_limit": CAPTIONS_MAX,
                        "translation_cache": len(_translation_cache), "translation_cache_limit": TRANSLATION_CACHE_MAX}
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
        + cap_long_text(req.text) + _glossary_block()
    )
    try:
        text, backend = await ai_complete(prompt)
        return {"text": text, "model": backend}
    except Exception as e:
        raise HTTPException(502, f"organization backend: {e}")





# Cloud translation (fast, natural long-sentence output) with local Qwen
# as the offline fallback. Google's free endpoint is tried first but is
# rate-limited (429) from some networks; MyMemory handles auto language
# detection (Finnish/English/Chinese) and works where Google is blocked.
CLOUD_TARGET = {"Chinese (Simplified)": "zh-CN", "English": "en", "Spanish": "es"}


async def google_translate(text: str, to: str) -> str:
    async with httpx.AsyncClient(timeout=8) as cx:
        r = await cx.get("https://translate.googleapis.com/translate_a/single",
                         params={"client": "gtx", "sl": "auto", "tl": to, "dt": "t", "q": text})
        r.raise_for_status()
        return "".join(part[0] for part in r.json()[0] if part and part[0]).strip()


async def mymemory_translate(text: str, to: str) -> str:
    async with httpx.AsyncClient(timeout=10) as cx:
        r = await cx.get("https://api.mymemory.translated.net/get",
                         params={"q": text[:480], "langpair": f"Autodetect|{to}"})
        r.raise_for_status()
        out = r.json().get("responseData", {}).get("translatedText", "")
        if "MYMEMORY WARNING" in out:  # quota notice leaks into the field
            raise RuntimeError(out[:80])
        return out.strip()


AI_TRANSLATE_GRACE = 2.5  # seconds machine translation waits for the AI version
RATE_LIMIT_COOLDOWN = 60.0  # skip a 429'd cloud source for this long
_cloud_cooldowns: dict[str, float] = {}  # source name -> monotonic retry-at


def _cloud_available(name: str) -> bool:
    return time.monotonic() >= _cloud_cooldowns.get(name, 0.0)


def _mark_rate_limited(name: str):
    _cloud_cooldowns[name] = time.monotonic() + RATE_LIMIT_COOLDOWN
    log.warning("%s rate-limited; cooling down for %ss", name, RATE_LIMIT_COOLDOWN)


async def translate_text(text: str, target: str, context: str = "") -> tuple[str, str]:
    """Returns (translation, backend_used).

    Speed strategy: machine translation (usually < 1.5 s) runs first; when
    an AI key is configured the AI interpreter version races it in a
    parallel task and wins if it lands within AI_TRANSLATE_GRACE seconds —
    latency is bounded by machine translation, quality by the AI. Ollama
    is the offline last resort. Results are cached: repeated sentences
    (lectures repeat a lot) cost zero tokens and zero latency."""
    context = (context or "")[-TRANSLATE_CONTEXT_CHARS:]
    key = _cache_key(text, target, context)
    hit = cache_get(key)
    if hit is not None:
        return hit[0], "cache"
    to = CLOUD_TARGET.get(target, "zh-CN")
    mt_text = None
    for name, cloud in (("google", google_translate), ("mymemory", mymemory_translate)):
        if not _cloud_available(name):
            continue
        try:
            out = await asyncio.wait_for(cloud(text, to), timeout=4)
            if out:
                mt_text = out
                break
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (429, 403):
                _mark_rate_limited(name)
            else:
                log.warning("%s failed: %s", cloud.__name__, e)
        except Exception as e:
            log.warning("%s failed: %s", cloud.__name__, e)

    if CONFIG["ai_key"]:
        prompt = (
            "You are a professional live interpreter for university lectures. "
            f"Translate the current chunk into {target}.\n"
            + (f"Previous chunk for context:\n{context}\n" if context else "")
            + f"Current chunk:\n{text}\n"
            "Rules: sound like a natural spoken lecture interpreter, never "
            "word-for-word; keep technical terms accurate and may keep the "
            "original term in parentheses on first mention; preserve names, "
            "numbers and formulas; omit nothing; output ONLY the translation."
            + _glossary_block()
        )
        ai_task = asyncio.create_task(ai_complete(prompt, timeout_s=10))
        if mt_text is None:  # nothing fast yet: give the AI a real chance
            try:
                out, backend = await asyncio.wait_for(ai_task, timeout=12)
                if out:
                    cache_put(key, (out, backend))
                    return out, backend
            except Exception as e:
                log.warning("AI translation failed: %s", e)
        else:
            done, _ = await asyncio.wait({ai_task}, timeout=AI_TRANSLATE_GRACE)
            if ai_task in done and not ai_task.exception() and ai_task.result()[0]:
                out, backend = ai_task.result()
                if out:
                    cache_put(key, (out, backend))
                    return out, backend
            ai_task.cancel()
    if mt_text:
        cache_put(key, (mt_text, "machine"))
        return mt_text, "machine"
    prompt = (
        "You are a professional live subtitle translator for university lectures. "
        f"Translate into {target}.\n"
        + (f"Previous context:\n{context}\n" if context else "")
        + f"Current sentence:\n{text}\n"
        "Rules: preserve names, numbers, formulas, citations, and technical terms; "
        "translate naturally as a professional lecture interpreter, not word-for-word; "
        "do not add explanations or omit information; output ONLY the translation."
        + _glossary_block()
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
            return r.json().get("response", "").split("</think>")[-1].strip(), "ollama"
    except Exception as e:
        log.exception("ollama failed")
        raise HTTPException(502, f"translation backend: {e}")


@app.post("/api/translate")
async def translate(req: TranslateReq):
    """Translate one sentence with context."""
    t0 = time.time()
    text, backend = await translate_text(req.text, req.target, req.context)
    out = {"text": text, "model": backend, "seconds": round(time.time() - t0, 2)}
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
    # Second line of defense (the bridge dedupes too): a bridge restart
    # re-flushes the caption window backlog verbatim — drop those repeats.
    norm = "".join(ch for ch in req.text.lower() if ch.isalnum())
    if any(norm == "".join(ch for ch in l["text"].lower() if ch.isalnum()) for l in CAPTIONS[-30:]):
        return {"duplicate": True}
    context = CAPTIONS[-1]["text"] if CAPTIONS else ""  # previous chunk keeps the interpreter coherent
    translation, backend = await translate_text(req.text.strip(), "Chinese (Simplified)", context)
    log.info("caption via %s", backend)
    line = {
        "text": req.text.strip(),
        "translation": translation,
        "offset": round(req.offset, 2),
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    CAPTIONS.append(line)
    del CAPTIONS[:-CAPTIONS_MAX]  # keep only the live poll buffer bounded
    try:  # durable cache: every incoming chunk survives even without a session
        with (DATA_DIR / "captions-log.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning("captions log write failed: %s", e)
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


@app.post("/api/bridge/heartbeat")
def bridge_heartbeat():
    global BRIDGE_LAST_SEEN
    BRIDGE_LAST_SEEN = time.monotonic()
    return {"ok": True}


@app.get("/api/captions")
def poll_captions(since: int = 0):
    since = max(0, min(since, len(CAPTIONS)))
    return {"lines": CAPTIONS[since:], "next": len(CAPTIONS), "active_session": ACTIVE_SESSION["id"],
            "bridge_online": (time.monotonic() - BRIDGE_LAST_SEEN) < 25}


@app.get("/api/search")
def search_sessions(q: str = ""):
    """Keyword search across every course's transcript and notes."""
    q = q.strip().lower()
    if not q:
        return {"hits": []}
    hits = []
    for p in sorted(DATA_DIR.glob("session-*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            s = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for seg in s.get("transcript", []):
            hay = (seg.get("text", "") + " " + seg.get("translation", "")).lower()
            if q in hay:
                hits.append({"session": s.get("title", ""), "sid": s.get("id"),
                             "offset": seg.get("offset"), "text": seg.get("text", ""),
                             "translation": seg.get("translation", "")})
        notes = s.get("notes")
        if isinstance(notes, str) and q in notes.lower():
            hits.append({"session": s.get("title", ""), "sid": s.get("id"),
                         "offset": None, "text": "[我的笔记] " + notes[:120], "translation": ""})
        if len(hits) >= 50:
            break
    return {"hits": hits[:50]}


class ArchiveReq(BaseModel):
    archived: bool
    category: str | None = None


@app.patch("/api/sessions/{sid}")
def update_session(sid: str, req: ArchiveReq):
    path = _session_path(sid)
    session = json.loads(path.read_text(encoding="utf-8"))
    session["archived"] = req.archived
    if req.category is not None and req.category.strip():
        session["category"] = req.category.strip()
    path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
    return session


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
    return {"source": "windows-live-captions", "ollama": ok_ollama, "model": CONFIG["model"], "self_check": "/api/self-check"}


@app.on_event("startup")
async def warm_ollama():
    """Preload the model at startup so the first real caption doesn't pay
    the cold-start load (8-10 s). Fire-and-forget: failures just log."""
    async def _warm():
        try:
            async with httpx.AsyncClient(timeout=300) as cx:
                await cx.post(f"{CONFIG['url']}/api/generate", json={
                    "model": CONFIG["model"], "prompt": "ok", "stream": False,
                    "think": False, "options": {"num_predict": 1}})
            log.info("ollama model %s warmed up", CONFIG["model"])
        except Exception as e:
            log.warning("ollama warmup skipped: %s", e)
    asyncio.create_task(_warm())


# ---- subtitle UI ----
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    # no-cache so UI updates show up without a hard refresh
    return FileResponse("static/index.html", headers={"Cache-Control": "no-cache"})

