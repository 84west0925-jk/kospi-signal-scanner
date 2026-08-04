#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
get_kakao_token.py — 카카오 refresh_token 발급 도우미
────────────────────────────────────────────────────────────────────────────
용도
  · 최초 발급
  · refresh_token 만료(약 2개월) 시 재발급  ← 주기적으로 다시 실행할 것

실행: python get_kakao_token.py   (또는 카카오토큰발급.bat 더블클릭)

앱 정보 (KOSPI 단타알림 / ID 1532737)
  REST API 키      : 카카오 디벨로퍼스 > 앱 > 플랫폼 키
  클라이언트 시크릿 : 같은 화면의 '클라이언트 시크릿' 코드
  Redirect URI     : https://example.com/oauth  (등록 완료됨)
"""
import requests

REDIRECT_URI = "https://example.com/oauth"

rest_key = input("REST API 키: ").strip()
client_secret = input("클라이언트 시크릿 (없으면 엔터): ").strip()

auth_url = (
    "https://kauth.kakao.com/oauth/authorize"
    f"?client_id={rest_key}&redirect_uri={REDIRECT_URI}"
    "&response_type=code&scope=talk_message"
)
print("\n① 아래 주소를 브라우저에 붙여넣고 '동의하고 계속하기'를 누르세요.\n")
print(auth_url)
print("\n② 이동된 주소창의  ...?code=XXXXX  에서 XXXXX 부분만 복사하세요.")
print("   (example.com 페이지가 안 열려도 정상입니다. 주소창만 보면 됩니다.)\n")

code = input("code: ").strip()

payload = {
    "grant_type": "authorization_code",
    "client_id": rest_key,
    "redirect_uri": REDIRECT_URI,
    "code": code,
}
if client_secret:
    payload["client_secret"] = client_secret

r = requests.post("https://kauth.kakao.com/oauth/token", data=payload, timeout=10)
js = r.json()

if "refresh_token" not in js:
    print("\n발급 실패:", js)
    print("code는 1회용이며 10분 내 만료됩니다. ①부터 다시 시도하세요.")
    raise SystemExit(1)

print("\n✅ 발급 완료 — GitHub Secrets에 아래 값을 등록/갱신하세요.\n")
print("KAKAO_REST_API_KEY  =", rest_key)
if client_secret:
    print("KAKAO_CLIENT_SECRET =", client_secret)
print("KAKAO_REFRESH_TOKEN =", js["refresh_token"])
print(f"\n(refresh_token 유효기간 약 {js.get('refresh_token_expires_in', 5184000)//86400}일)")
input("\n엔터를 누르면 종료합니다.")
