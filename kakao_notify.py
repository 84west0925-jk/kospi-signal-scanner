#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kakao_notify.py — 카카오톡 '나에게 보내기' 알림 전송기
────────────────────────────────────────────────────────────────────────────
주 채널 = 텔레그램 (봇 토큰은 만료가 없다)
  TELEGRAM_BOT_TOKEN : @BotFather 로 봇 생성 시 받는 토큰
  TELEGRAM_CHAT_ID   : 본인 채팅 ID (@userinfobot 에게 말 걸면 알려줌)

보조 채널 = 카카오톡 (선택, 설정된 경우에만 함께 발송)
  KAKAO_REST_API_KEY / KAKAO_CLIENT_SECRET / KAKAO_REFRESH_TOKEN
  ※ 카카오 refresh_token은 약 2개월마다 만료되고, 만료 1개월 전부터는
    갱신할 때마다 새 토큰으로 회전하며 기존 토큰이 무효화된다.
    자동 저장 수단이 없으면 조용히 끊기므로 단독 채널로 쓰지 않는다.

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
    payload = {
        "grant_type": "refresh_token",
        "client_id": rest_key,
        "refresh_token": refresh,
    }
    secret = _env("KAKAO_CLIENT_SECRET")
    if secret:
        payload["client_secret"] = secret
    try:
        r = requests.post(TOKEN_URL, data=payload, timeout=10)
        r.raise_for_status()
        js = r.json()
        if "refresh_token" in js:
            # 카카오는 만료 1개월 전부터 갱신 시 새 refresh_token을 발급하고
            # 기존 토큰을 무효화한다. 저장하지 않으면 다음 실행부터 알림이 끊긴다.
            print("[kakao] 새 refresh_token 발급됨 — Secret 자동 갱신 시도")
            _persist_refresh_token(js["refresh_token"])
        return js.get("access_token")
    except Exception as e:
        print(f"[kakao] 토큰 갱신 실패: {e}")
        return None


def _persist_refresh_token(new_token: str) -> bool:
    """회전된 refresh_token을 GitHub Secret(KAKAO_REFRESH_TOKEN)에 덮어쓴다.

    필요 조건 (GitHub Actions 환경)
      · Secret `GH_PAT` : 이 저장소에 'Secrets: Read and write' 권한이 있는 PAT
      · 워크플로에서 GH_PAT, GITHUB_REPOSITORY 를 env로 전달
    실패해도 이번 실행의 발송에는 영향이 없다. 다만 다음 실행부터 끊기므로
    실패 시 로그에 크게 남겨 워크플로가 실패로 끝나도록 한다."""
    pat = _env("GH_PAT")
    repo = _env("GITHUB_REPOSITORY")
    if not pat or not repo:
        print("[kakao] ❌ 새 refresh_token을 저장할 수 없습니다 (GH_PAT 미설정).")
        print("[kakao] ❌ 아래 값을 GitHub Secret KAKAO_REFRESH_TOKEN 에 직접 넣으세요:")
        print("[kakao] KAKAO_REFRESH_TOKEN =", new_token)
        return False

    try:
        from base64 import b64encode
        from nacl import encoding, public

        h = {"Authorization": f"Bearer {pat}",
             "Accept": "application/vnd.github+json"}
        key = requests.get(
            f"https://api.github.com/repos/{repo}/actions/secrets/public-key",
            headers=h, timeout=10).json()

        sealed = public.SealedBox(
            public.PublicKey(key["key"].encode(), encoding.Base64Encoder())
        ).encrypt(new_token.encode())

        r = requests.put(
            f"https://api.github.com/repos/{repo}/actions/secrets/KAKAO_REFRESH_TOKEN",
            headers=h, timeout=10,
            json={"encrypted_value": b64encode(sealed).decode(),
                  "key_id": key["key_id"]})
        if r.status_code in (201, 204):
            print("[kakao] ✅ KAKAO_REFRESH_TOKEN Secret 자동 갱신 완료")
            return True
        print(f"[kakao] ❌ Secret 갱신 실패 {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[kakao] ❌ Secret 갱신 예외: {e}")

    print("[kakao] ❌ 아래 값을 GitHub Secret KAKAO_REFRESH_TOKEN 에 직접 넣으세요:")
    print("[kakao] KAKAO_REFRESH_TOKEN =", new_token)
    return False


def send_text(text: str, link_url: str = "https://finance.naver.com/sise/",
              token: str | None = None) -> bool:
    """알림 발송. 텔레그램이 주 채널, 카카오는 설정된 경우에만 보조로 함께 보낸다.
    한 채널이라도 성공하면 True."""
    tg_ok = _telegram(text)
    kk_ok = _kakao(text, link_url, token)

    if tg_ok is None and kk_ok is None:
        print("[notify] 설정된 알림 채널이 없습니다 "
              "(TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID 또는 KAKAO_* 를 등록하세요)")
        return False
    return bool(tg_ok) or bool(kk_ok)


def _telegram(text: str) -> bool | None:
    """텔레그램 전송. 미설정이면 None, 성공 True, 실패 False.
    봇 토큰은 만료가 없어 주 채널로 쓴다. 메시지 길이 4096자까지 한 번에 전송."""
    tok, chat = _env("TELEGRAM_BOT_TOKEN"), _env("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return None
    ok = True
    for chunk in _split(text, 3800):
        try:
            r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                              data={"chat_id": chat, "text": chunk,
                                    "disable_web_page_preview": True}, timeout=15)
            if r.status_code != 200:
                print(f"[telegram] 전송 실패 {r.status_code}: {r.text[:200]}")
                ok = False
        except Exception as e:
            print(f"[telegram] 전송 예외: {e}")
            ok = False
    if ok:
        print("[telegram] 전송 성공")
    return ok


def _kakao(text: str, link_url: str, token: str | None) -> bool | None:
    """카카오톡 나에게 보내기(보조). 미설정이면 None.
    refresh_token은 약 2개월마다 만료되므로 단독 채널로 쓰지 않는다."""
    if not (_env("KAKAO_REST_API_KEY") and _env("KAKAO_REFRESH_TOKEN")):
        return None
    token = token or get_access_token()
    if not token:
        return False
    ok = True
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
    if ok:
        print("[kakao] 전송 성공")
    return ok


def _split(text: str, size: int) -> list[str]:
    """길이 제한에 맞춰 분할. 줄 단위로 나누되, 한 줄이 제한보다 길면
    그 줄도 잘라서 이어붙인다(잘려나가 유실되지 않도록)."""
    out: list[str] = []
    buf = ""
    for ln in text.split("\n"):
        while len(ln) > size:            # 제한보다 긴 줄은 강제로 쪼갠다
            if buf:
                out.append(buf)
                buf = ""
            out.append(ln[:size])
            ln = ln[size:]
        if not buf:
            buf = ln
        elif len(buf) + 1 + len(ln) <= size:
            buf = f"{buf}\n{ln}"
        else:
            out.append(buf)
            buf = ln
    if buf:
        out.append(buf)
    return out or [""]


def _json_str(s: str) -> str:
    import json
    return json.dumps(s, ensure_ascii=False)


if __name__ == "__main__":
    print("전송 결과:", send_text("✅ KOSPI 단타 알림 봇 연결 테스트 성공"))
