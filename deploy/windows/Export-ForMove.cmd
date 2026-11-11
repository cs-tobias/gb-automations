@echo off
rem Old PC: packs GB Automations into a move folder on the Desktop. See docs\move-to-new-pc.md
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Export-ForMove.ps1" %*
pause
