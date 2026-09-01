# Lecture Translator

Real-time lecture translation built on **Windows 实时字幕 (Live Captions)**.
Windows does the speech recognition; this project adds translation, course
sessions, classroom AI tools, and a persistent transcript.

```
Windows Live Captions (Win+Ctrl+L, high-quality on-device ASR)
   └─> bridge/live_captions_bridge.py (host, UI Automation)
         └─> app container:  /api/captions -> Ollama Qwen translation (GPU)
               └─> subtitle page + session transcript (Docker volume)
```

## One-click install as a desktop app (recommended)

1. Double-click `install.bat`: copies the whole app to
   `%LOCALAPPDATA%\LectureTranslator` and creates a desktop shortcut
   “Lecture Translator” (no admin rights needed).
2. Double-click the desktop shortcut → press **启动**. The launcher starts
   Docker Desktop if needed, brings up the stack, launches the caption bridge
   minimized, and opens the browser. **停止** shuts the stack and bridge down.
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

## What you get

- Live subtitles: original caption + Chinese translation, with timestamps.
- Course sessions: every committed sentence is persisted to the session's
  transcript (`GET /api/sessions/{id}`) for replay and export.
- Classroom tools: grounded Q&A ("问 AI"), term extraction, transcript-
  preserving AI notes, Markdown export with `[mm:ss]` timestamps.
- Self-check: `GET /api/self-check` (`ready: true` means Ollama + model + data dir OK).

## Notes

- Only the app (FastAPI) and Ollama run in Docker; the bridge needs a tiny
  host-side venv because UI Automation cannot reach Windows from a container.
- GPU: only the Ollama container reserves the NVIDIA GPU.
- Live Captions source language is chosen in Windows caption settings.
- Remove all stored data with `docker compose down -v`.
