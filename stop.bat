@echo off
setlocal
cd /d "%~dp0"

echo Stopping the Live Captions bridge...
rem The bridge holds a singleton lock on 127.0.0.1:49190; kill that listener
rem (plus the minimized launcher window if present). The OS frees the socket
rem on kill, so the next start is never blocked by a stale lock.
for /f "tokens=5" %%p in ('netstat -aon ^| findstr :49190 ^| findstr LISTENING') do (
  taskkill /PID %%p /T /F >nul 2>&1
)
taskkill /FI "WINDOWTITLE eq LectureTranslator-Bridge*" /T /F >nul 2>&1

echo Stopping AI containers (unloads models, frees RAM and GPU memory)...
docker compose stop
rem whisperlivekit belongs to a separate compose project (xuexi) but holds
rem the single biggest idle cost (~4.3 GB VRAM for large-v3).
docker stop whisperlivekit >nul 2>&1

echo.
echo All stopped. Nothing AI-related runs in the background anymore.
echo Start again with start.bat (whisperlivekit reloads in about 1 minute).
docker info >nul 2>&1 || pause
endlocal
