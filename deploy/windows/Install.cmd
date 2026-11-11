@echo off
rem New PC: installs GB Automations from this move folder. See docs\move-to-new-pc.md
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install.ps1" %*
if errorlevel 1 pause
