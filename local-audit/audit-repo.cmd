@echo off
setlocal
rem Local audit pre-pass launcher. Scans a whole repo for "slop" candidates
rem (silent failures + misleading counters) and opens the list in VS Code.
rem
rem   audit-repo.cmd <path>   -> scans that repo
rem   audit-repo.cmd          -> scans the current directory
rem
rem This only DISCOVERS candidates. Judgment happens separately: open a file,
rem paste its rows, run your audit. Read-only; nothing is written inside the target repo.

set "REPO=%~1"
if "%REPO%"=="" set "REPO=%CD%"

for %%I in ("%REPO%") do set "NAME=%%~nxI"
set "OUT=%~dp0out"
if not exist "%OUT%" mkdir "%OUT%"
set "OUTFILE=%OUT%\%NAME%-candidates.md"

echo Scanning %REPO% for slop candidates ...
python "%~dp0slop_prepass.py" "%REPO%" > "%OUTFILE%"
if errorlevel 1 (
  echo.
  echo Pre-pass FAILED. Check that Python is on PATH and that "%REPO%" is a git repo root.
  pause
  exit /b 1
)

echo Done. Candidate list: %OUTFILE%
code "%OUTFILE%"
endlocal
