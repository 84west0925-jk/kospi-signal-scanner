@echo off
chcp 65001 >nul
echo.
echo  카카오 refresh_token 발급 도구
echo  ----------------------------------------
echo.
uv run --python 3.11 --with requests python "%~dp0get_kakao_token.py"
echo.
pause
