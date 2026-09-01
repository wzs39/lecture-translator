# Lecture Translator (isolated)

Real-time lecture transcription + translation. **All compute runs inside Docker
containers** — nothing is installed on the host beyond Docker itself.

```
browser (mic or tab/system audio)
   └─> app container:  faster-whisper (GPU, auto language detect)
         └─> ollama container: Qwen translation (GPU)
               └─> floating subtitles in the browser
```

## Run on Windows

1. Start Docker Desktop.
2. Double-click `start.bat`.
3. The browser opens automatically at `http://localhost:8000`.
4. To stop, double-click `stop.bat`.

The first start downloads the image and models and may take several minutes.

## Run from a terminal

```bash
docker compose up -d --build
./pull-model.sh
open http://localhost:8000
```

Then click **🎙 Start microphone** (room lectures) or **🖥 Capture tab/system
audio** (online lectures — tick "share tab audio" in the picker).

## Notes

- GPU: both containers reserve the NVIDIA GPU via the compose device block.
- Host pollution: images, models, and volumes live inside Docker. Remove with
  `docker compose down -v`.
- Models: Whisper `small` (good balance); set `WHISPER_MODEL=large-v3` in
  docker-compose.yml for maximum accuracy (needs ~10 GB VRAM total with Qwen).
- Latency: ~1–2 s end-to-end with the default 4 s chunking.
