@echo off
rem Starts GB Automations if needed, then opens the status page.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-GbAutomations.ps1" %*
