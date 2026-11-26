@echo off
cd /d "%~dp0"
set UPSTOX_TOKEN=YAHAN_APNA_TOKEN_PASTE_KAREIN
set NTFY_TOPIC=
python live_server.py
pause
