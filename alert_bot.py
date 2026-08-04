#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
alert_bot.py — 단타 RSI 신호 감시 + 카카오톡 알림 (GitHub Actions 크론용)
────────────────────────────────────────────────────────────────────────────
실행: python alert_bot.py            (기본 30분봉)
      INTERVAL=60m python alert_bot.py
동작:
  1) KOSPI 시총 50위 분봉 조회 → RSI 3분할 로직 평가
  2) 신규 액션 발생 시 swing_positions.json 갱신 + 카카오톡 전송
  3) 최신 봉이 '오늘'이 아니면 휴장으로 간주하고 종료 (공휴일 자동 스킵)
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

import swing_rsi as sw
from kakao_notify import send_text, get_access_token

INTERVAL = os.getenv("INTERVAL", "30m")
RSI_BUY = float(os.getenv("RSI_BUY", sw.RSI_BUY))
RSI_SELL = float(os.getenv("RSI_SELL", sw.RSI_SELL))
SEED = float(os.getenv("SEED", 3_000_000))
FORCE = os.getenv("FORCE", "0") == "1"   # 휴장 체크 무시 (테스트용)
TEST = os.getenv("TEST", "0") == "1"     # 카카오 연결만 테스트하고 종료


def main() -> int:
    now = datetime.now(sw.KST)
    print(f"=== 단타 RSI 스캔 {now:%Y-%m-%d %H:%M} KST / {INTERVAL} ===")

    if TEST:
        print("[TEST 모드] 카카오 연결 테스트 메시지 발송")
        ok = send_text(f"✅ 알림 봇 연결 테스트 성공 ({now:%m/%d %H:%M})\n"
                       f"GitHub Actions에서 정상 발송되었습니다.")
        print("카카오 전송:", "성공" if ok else "실패")
        return 0 if ok else 1

    if now.weekday() >= 5 and not FORCE:
        print("주말 — 종료")
        return 0

    state = sw.load_state()
    universe = sw.get_top50()
    print(f"유니버스 {len(universe)}종목")

    df, alerts = sw.scan(universe, INTERVAL, RSI_BUY, RSI_SELL, state, commit=False)
    if df.empty:
        print("데이터 없음 — 종료")
        return 0

    # 휴장 판정: 최신 봉 날짜가 오늘이 아니면 장이 열리지 않은 것
    last_day = str(df["시각"].max())[:10]
    if last_day != now.strftime("%Y-%m-%d") and not FORCE:
        print(f"최신 봉 {last_day} ≠ 오늘 — 휴장으로 간주, 종료")
        return 0

    print(f"과매도 {int((df['구간']=='과매도').sum())} · 과매수 {int((df['구간']=='과매수').sum())} · "
          f"보유 {len(state['positions'])} · 액션 {len(alerts)}")

    if not alerts:
        print("트리거 없음 — 알림 미발송")
        return 0

    # 상태 반영 (commit)
    for a in alerts:
        sw.apply_action(state, a["ticker"], a["name"], a, a["time"])
    sw.save_state(state)

    unit = SEED / 3
    header = f"📣 KOSPI 단타 신호 {now:%m/%d %H:%M} ({INTERVAL})\n1회 투입금 {unit:,.0f}원\n"
    body = "\n\n".join(sw.format_alert(a) for a in alerts)
    msg = header + "\n" + body

    token = get_access_token()
    ok = send_text(msg, token=token)
    print("카카오 전송:", "성공" if ok else "실패")
    for a in alerts:
        print(" -", sw.format_alert(a).replace("\n", " | "))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
