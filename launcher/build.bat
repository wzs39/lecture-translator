@echo off
rem Builds LectureTranslatorLauncher.exe using the .NET Framework csc that
rem ships with Windows (no installs, no NuGet).
setlocal
set CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe
if not exist "%CSC%" set CSC=%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe
if not exist "%CSC%" (
  echo csc.exe not found - .NET Framework 4.x is required.
  exit /b 1
)
"%CSC%" /nologo /target:winexe /codepage:65001 /win32res:app.res ^
  /r:System.Windows.Forms.dll /r:System.Drawing.dll ^
  /out:..\LectureTranslatorLauncher.exe Launcher.cs
if errorlevel 1 (
  echo Build failed.
  exit /b 1
)
echo Built ..\LectureTranslatorLauncher.exe