@echo off
chcp 65001 > nul
cd /d "%~dp0"
title 단타 RSI KIS 최초 데이터 준비
python kis_alert_runner.py --bootstrap-only
pause
