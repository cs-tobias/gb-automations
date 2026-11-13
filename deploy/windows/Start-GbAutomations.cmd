@echo off
rem Starts GB Automations if needed, then shows its live log.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-GbAutomations.ps1" %*
