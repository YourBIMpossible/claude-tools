@echo off
rem Double-click this on the new PC. It asks for admin (UAC), then runs the setup.
rem Dry run (checks only):  Start-Setup.cmd -DryRun
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Setup-FleetPC.ps1" %*
