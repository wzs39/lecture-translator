@echo off
setlocal
cd /d "%~dp0"

echo Starting Lecture Translator...
docker info >nul 2>&1
if errorlevel 1 (
  echo Docker Desktop is not running. Please start it and run this file again.
  pause
  exit /b 1
)

docker compose up -d --build
if errorlevel 1 (
  echo Failed to start the containers.
  pause
  exit /b 1
)

echo Waiting for the service...
for /l %%i in (1,1,30) do (
  curl.exe -fsS http://localhost:8000/api/self-check >nul 2>&1 && goto ready
  timeout /t 2 /nobreak >nul
)

echo Service did not become ready. Check: docker compose logs
pause
exit /b 1

:ready
echo Lecture Translator is ready: http://localhost:8000
start "" http://localhost:8000

rem Wake the WhisperLiveKit container if it was stopped by stop.bat
rem (start is instant if it is already running; large-v3 needs ~1 min to load)
docker start whisperlivekit >nul 2>&1

rem Also launch the Live Captions bridge in a minimized window (if set up)
if exist "%~dp0bridge\.venv\Scripts\python.exe" (
  start "LectureTranslator-Bridge" /min cmd /c "%~dp0start-captions.bat"
  echo Live Captions bridge launched in a minimized window.
)
endlocal
