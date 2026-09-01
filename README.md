# Lecture Translator

Real-time lecture translation built on **Windows 实时字幕 (Live Captions)**.
Windows does the speech recognition; this project adds translation, course
sessions, classroom AI tools, and a persistent transcript.

```
Windows Live Captions (Win+Ctrl+L, high-quality on-device ASR)
   └─> bridge/live_captions_bridge.py (host, UI Automation)
         └─> app container: /api/captions -> local Ollama (GPU) or cloud AI API
               └─> subtitle page + course transcripts (Docker volume)
```

## One-click install as a desktop app (recommended)

1. Double-click `install.bat`: copies the whole app to
   `%LOCALAPPDATA%\LectureTranslator` and creates a desktop shortcut
   “Lecture Translator” (no admin rights needed).
2. Double-click the desktop shortcut → press **启动**. The launcher starts
   Docker Desktop if needed, brings up the stack, launches the caption bridge
   minimized, and opens the browser. **停止** shuts the stack and bridge down.
   It shows an update banner when a newer `version.txt` exists on GitHub, and
   supports `--version` / `--selftest`.
3. Rebuild the launcher after edits: `launcher\build.bat` (uses the .NET
   Framework csc.exe that ships with Windows; no installs).

## Run on Windows (manual)

1. Start Docker Desktop, then double-click `start.bat` → opens
   `http://localhost:8000`.
2. Open Live Captions: `Win+Ctrl+L` (keep the window visible on screen).
3. Double-click `start-captions.bat` — first run creates an isolated Python
   venv in `bridge/.venv` (only host-side install; it reads caption text via
   UI Automation, which must run outside Docker).
4. Captured sentences appear as subtitles and are saved into the active
   course session automatically.
5. To stop: close the bridge window, then `stop.bat`.

## Features

- **Live subtitles**: original caption + Chinese translation, with timestamps.
- **Courses**: create/switch courses; every committed sentence is persisted
  to the course transcript (`GET /api/sessions/{id}`) for replay and export.
- **Glossary 术语表**: extracted terms are saved into the course and injected
  into the AI prompt, so the same term translates consistently.
- **AI classroom tools**: grounded Q&A (问 AI), term extraction, notes that
  keep the original text (整理); AI summaries can be saved into categories.
- **Categories & storage ⑥**: create/rename/delete categories (courses and
  summaries follow), storage snapshot, one-click test-data cleanup, clear log.
- **Search & stats**: keyword search across all courses; per-course statistics.
- **Export**: Markdown with `[mm:ss]` timestamps, Word .doc, Anki/Quizlet CSV,
  SRT.
- **Resilient bridge**: single-instance lock, crash auto-restart, window
  re-discovery every 10 s, and heartbeats even while waiting for the caption
  window — the page shows 🟢 online / 🟡 waiting for window / 🔴 offline.
- **Disk warning**: page banner when the data drive drops below 20 GB free.
- **Browser speech fallback**: ③ 浏览器语音识别 (Web Speech API) for languages
  Live Captions lacks, e.g. Finnish.
- **Self-check**: `GET /api/self-check`, `bridge --selfcheck`, and the
  executable contract suite `python test_pipeline.py`.

## Notes

- Only the app (FastAPI) and Ollama run in Docker; the bridge needs a tiny
  host-side venv because UI Automation cannot reach Windows from a container.
- GPU: only the Ollama container reserves the NVIDIA GPU.
- Live Captions source language is chosen in Windows caption settings.
- Remove all stored data with `docker compose down -v`.