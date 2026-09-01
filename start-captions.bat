@echo off
rem One-click start for the Windows Live Captions bridge (host side).
rem Self-healing: crashes are logged and the bridge restarts (max 5).
rem Exit code 3 means another instance is already running -> no restart.
rem Requires: Docker stack running; Live Captions via Win+Ctrl+L.
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
if not exist bridge\.venv (
  echo Creating bridge virtual environment...
  %PY% -m venv bridge\.venv || goto :err
  bridge\.venv\Scripts\pip install -r bridge\requirements.txt || goto :err
)
set /a TRIES=0
:loop
echo [%date% %time%] starting bridge...
bridge\.venv\Scripts\python -u bridge\live_captions_bridge.py >> bridge\bridge.log 2>&1
set RC=%ERRORLEVEL%
if %RC%==3 (
  echo Another bridge instance is already running; this copy exits.
  goto :done
)
if %RC%==0 goto :done
if %RC%==130 goto :done
if %RC%==3221225786 goto :done
echo [%date% %time%] bridge crashed with code %RC%, self-checking...
bridge\.venv\Scripts\python bridge\live_captions_bridge.py --selfcheck >> bridge\bridge.log 2>&1
echo See bridge\bridge.log and bridge\selfcheck.log
set /a TRIES+=1
if %TRIES% GEQ 5 (
  echo Bridge crashed 5 times; giving up. Check bridge\bridge.log
  goto :done
)
timeout /t 5 /nobreak >nul
goto :loop
:done
endlocal