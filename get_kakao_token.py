#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
get_kakao_token.py — 카카오 refresh_token 최초 1회 발급 도우미 (로컬 실행)
────────────────────────────────────────────────────────────────────────────
사전 준비 (카카오 디벨로퍼스 https://developers.kakao.com)
  1. 애플리케이션 추가하기 → 앱 생성
  2. [앱 키] REST API 키 복사
  3. [카카오 로그인] 활성화 ON
  4. [카카오 로그인 > Redirect URI] 에  https://example.com/oauth  등록
  5. [카카오 로그인 > 동의항목] '카카오톡 메시지 전송(talk_message)' 선택 동의로 설정

실행:  python get_kakao_token.py
"""
import requests

REDIRECT_URI = "https://example.com/oauth"

rest_key = input("REST API 키: ").strip()

auth_url = (
    "https://kauth.kakao.com/oauth/authorize"
    f"?client_id={rest_key}&redirect_uri={REDIRECT_URI}"
    "&response_type=code&scope=talk_message"
)
print("\n① 아래 주소를 브라우저에 붙여넣고 '동의하고 계속하기'를 누르세요.\n")
print(auth_url)
print("\n② 이동된 주소창의  ...?code=XXXXX  에서 XXXXX 부분만 복사하세요.\n")

code = input("code: ").strip()

r = requests.post("https://kauth.kakao.com/oauth/token", data={
    "grant_type": "authorization_code",
    "client_id": rest_key,
    "redirect_uri": REDIRECT_URI,
    "code": code,
}, timeout=10)

js = r.json()
if "refresh_token" not in js:
    print("발급 실패:", js)
    raise SystemExit(1)

print("\n✅ 발급 완료 — 아래 두 값을 GitHub Secrets에 등록하세요.\n")
print("KAKAO_REST_API_KEY  =", rest_key)
print("KAKAO_REFRESH_TOKEN =", js["refresh_token"])
print(f"\n(refresh_token 유효기간 약 {js.get('refresh_token_expires_in', 5184000)//86400}일)")
