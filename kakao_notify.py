#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kakao_notify.py — 카카오톡 '나에게 보내기' 알림 전송기
────────────────────────────────────────────────────────────────────────────
필요 환경변수 (GitHub Secrets 또는 .env)
  KAKAO_REST_API_KEY  : 카카오 디벨로퍼스 > 내 애플리케이션 > 앱 키 > REST API 키
  KAKAO_REFRESH_TOKEN : 최초 1회 발급 (get_token.py 참고). 유효기간 약 2개월
선택
  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID : 설정 시 텔레그램에도 동시 발송(백업)

동작: 매 실행마다 refresh_token으로 access_token을 새로 받아 사용한다.
"""
from __future__ import annotations

import os
import requests

TOKEN_URL = "https://kauth.kakao.com/oauth/token"
MEMO_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
TEXT_LIMIT = 190  # 카카오 text 템플릿 200자 제한 — 여유 확보


def _env(key: str) -> str | None:
    val = os.getenv(key)
    if val:
        return val.strip()
    try:  # Streamlit 환경 지원
        import streamlit as st
        return str(st.secrets[key]).strip()
    except Exception:
        return None


def get_access_token() -> str | None:
    rest_key, refresh = _env("KAKAO_REST_API_KEY"), _env("KAKAO_REFRESH_TOKEN")
    if not rest_key or not refresh:
        print("[kakao] 키 미설정 — 전송 생략")
        return None
    try:
        r = requests.post(TOKEN_URL, data={
            "grant_type": "refresh_token",
            "client_id": rest_key,
            "refresh_token": refresh,
        }, timeout=10)
        r.raise_for_status()
        js = r.json()
        if "refresh_token" in js:
            print("[kakao] ⚠️ 새 refresh_token 발급됨 — GitHub Secret을 아래 값으로 갱신하세요:")
            print("[kakao] KAKAO_REFRESH_TOKEN =", js["refresh_token"])
        return js.get("access_token")
    except Exception as e:
        print(f"[kakao] 토큰 갱신 실패: {e}")
        return None


def send_text(text: str, link_url: str = "https://finance.naver.com/sise/",
              token: str | None = None) -> bool:
    """카카오톡 나에게 보내기. 200자 초과 시 자동 분할 전송."""
    token = token or get_access_token()
    ok = True
    if token:
        for chunk in _split(text, TEXT_LIMIT):
            payload = {
                "template_object": (
                    '{"object_type":"text",'
                    f'"text":{_json_str(chunk)},'
                    f'"link":{{"web_url":"{link_url}","mobile_web_url":"{link_url}"}},'
                    '"button_title":"차트 보기"}'
                )
            }
            try:
                r = requests.post(MEMO_URL, headers={"Authorization": f"Bearer {token}"},
                                  data=payload, timeout=10)
                if r.status_code != 200:
                    print(f"[kakao] 전송 실패 {r.status_code}: {r.text[:200]}")
                    ok = False
            except Exception as e:
                print(f"[kakao] 전송 예외: {e}")
                ok = False
    else:
        ok = False

    _telegram(text)  # 설정된 경우에만 동작
    return ok


def _telegram(text: str) -> None:
    tok, chat = _env("TELEGRAM_BOT_TOKEN"), _env("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      data={"chat_id": chat, "text": text}, timeout=10)
    except Exception as e:
        print(f"[telegram] 전송 실패: {e}")


def _split(text: str, size: int) -> list[str]:
    lines, buf, out = text.split("\n"), "", []
    for ln in lines:
        if len(buf) + len(ln) + 1 > size:
            if buf:
                out.append(buf)
            buf = ln[:size]
        else:
            buf = f"{buf}\n{ln}" if buf else ln
    if buf:
        out.append(buf)
    return out or [text[:size]]


def _json_str(s: str) -> str:
    import json
    return json.dumps(s, ensure_ascii=False)


if __name__ == "__main__":
    print("전송 결과:", send_text("✅ KOSPI 단타 알림 봇 연결 테스트 성공"))
