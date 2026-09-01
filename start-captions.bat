@echo off
rem One-click start for the Windows Live Captions bridge (host side).
rem Requires: Live Captions turned on (Win+Ctrl+L) and Docker stack running.
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
if not exist bridge\.venv (
  echo Creating bridge virtual environment...
  %PY% -m venv bridge\.venv || goto :err
  bridge\.venv\Scripts\pip install -r bridge\requirements.txt || goto :err
)
echo Starting Live Captions bridge... keep the Live Captions window open.
bridge\.venv\Scripts\python bridge\live_captions_bridge.py
goto :eof
:err
echo Failed to set up the bridge Python environment.
pause
