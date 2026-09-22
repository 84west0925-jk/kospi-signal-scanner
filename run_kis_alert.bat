@echo off
chcp 65001 > nul
cd /d "%~dp0"
title 단타 RSI KIS 정시 알림

if not exist ".env" (
    echo ===============================================================
    echo  .env 파일이 없습니다.
    echo  .env.example 을 .env 로 복사한 뒤 KIS APP KEY / SECRET을 입력하세요.
    echo  텔레그램 토큰과 채팅ID는 기존 설정을 그대로 사용합니다.
    echo ===============================================================
    pause
    exit /b 1
)

python kis_alert_runner.py
if errorlevel 1 (
    echo.
    echo [오류] 실행이 중단되었습니다. 위 메시지를 확인하세요.
    pause
)
