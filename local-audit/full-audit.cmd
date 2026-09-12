@echo off
setlocal
rem Full local second-opinion audit in one shot: pre-pass discovers, the local
rem Qwen model classifies, one report is written to out\. Read-only on the repo.
rem Needs Ollama running (localhost:11434). ~1 min per flagged file.
rem
rem   Double-click            -> audits F:\BIMpossible (all flagged files)
rem   full-audit.cmd <path>   -> audits that repo instead
rem
rem On-demand only. Do NOT schedule this (owner decision 2026-08-08): a weaker
rem model's misses go unnoticed unattended. Findings are HYPOTHESES to verify.

set "REPO=%~1"
if "%REPO%"=="" set "REPO=F:\BIMpossible"

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
