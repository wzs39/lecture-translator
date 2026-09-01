@echo off
rem Lecture Translator one-click install.
rem Copies this folder to %%LOCALAPPDATA%%\LectureTranslator and creates a
rem desktop shortcut "Lecture Translator" that opens the launcher app.
rem No administrator rights needed.
setlocal
set DEST=%LOCALAPPDATA%\LectureTranslator
echo Installing to %DEST% ...
robocopy "%~dp0" "%DEST%" /E /XD .git /XF *.err selftest.log /NFL /NDL /NJH /NJS /NC /NS >nul
if errorlevel 8 (
  echo Install failed (robocopy error).
  exit /b 1
)
powershell -NoProfile -Command "$ws=New-Object -ComObject WScript.Shell; $lnk=$ws.CreateShortcut([Environment]::GetFolderPath('Desktop')+'\Lecture Translator.lnk'); $lnk.TargetPath='%DEST%\LectureTranslatorLauncher.exe'; $lnk.WorkingDirectory='%DEST%'; $lnk.Save()"
if errorlevel 1 (
  echo Failed to create desktop shortcut.
  exit /b 1
)
echo.
echo Done. Desktop shortcut "Lecture Translator" created.
echo Double-click it, then press 启动 to bring everything up.
exit /b 0