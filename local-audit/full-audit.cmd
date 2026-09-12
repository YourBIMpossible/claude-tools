@echo off
setlocal
rem Full local second-opinion audit in one shot: pre-pass discovers, the local
rem Qwen model classifies, one report is written to out\. Read-only on the repo.
rem Needs Ollama running (localhost:11434). ~1 min per flagged file.
rem
rem   full-audit.cmd <path>   -> audits that repo (all flagged files)
rem   full-audit.cmd          -> audits the current directory
rem
rem On-demand only. Do NOT schedule this: a weaker model's misses go unnoticed
rem unattended. Findings are HYPOTHESES to verify.

set "REPO=%~1"
if "%REPO%"=="" set "REPO=%CD%"

echo Running local second-opinion audit on %REPO% ...
echo (This is slow: ~1 minute per flagged file. Leave it running.)
echo.
python "%~dp0local_audit.py" "%REPO%" --full
if errorlevel 1 (
  echo.
  echo Audit FAILED. Check that Ollama is running and that "%REPO%" is a git repo root.
  pause
  exit /b 1
)
pause
endlocal
