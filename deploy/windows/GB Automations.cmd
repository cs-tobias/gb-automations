@echo off
rem Opens the GB Automations program. The first time, it also puts the
rem GB Automations icon on the desktop and the start at Windows sign-in.
start "" powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0GbAutomations.ps1"
