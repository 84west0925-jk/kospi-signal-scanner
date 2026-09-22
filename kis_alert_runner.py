#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kis_alert_runner.py — KIS 기반 정시 텔레그램 알림 상시 실행기
────────────────────────────────────────────────────────────────────────────
기존 Streamlit/전략/포지션 구조는 그대로 두고, 장중 알림만 정확한 봉 마감시각에 실행한다.

실행:
  python kis_alert_runner.py
  python kis_alert_runner.py --once
  python kis_alert_runner.py --bootstrap-only

30분봉 기준 실행시각(KST):
  09:30, 10:00, 10:30, ... , 15:30

GitHub Actions의 schedule 지연을 사용하지 않으므로 정시성이 높다.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import swing_rsi as sw
from alert_bot import run_once
from kis_market import KISClient, KISError, KISRSIDataSource, latest_scheduled_close, scheduled_closes

INTERVAL = os.getenv("INTERVAL", "30m")
DELAY_SEC = max(1, int(os.getenv("KIS_CLOSE_DELAY_SEC", "4")))
GRACE_SEC = max(30, int(os.getenv("KIS_ALERT_GRACE_SEC", "55")))


def _bootstrap() -> int:
    """RSI 워밍업 캐시는 기존 yfinance 분봉으로 준비한다.

    KIS 과거분봉(FHKST03010230)은 호출하지 않는다.
    실제 신호를 만드는 최신 확정봉만 KIS 당일분봉으로 교체·누적한다.
    """
    now = datetime.now(sw.KST)
    uni = sw.get_universe(int(os.getenv("KOSPI_N", "100")), int(os.getenv("KOSDAQ_N", "10")))
    ds = KISRSIDataSource(KISClient())
    close = latest_scheduled_close(now, INTERVAL)
    if close is None:
        closes = scheduled_closes(now.date(), INTERVAL)
        before = closes[0] - timedelta(minutes=int(INTERVAL.rstrip("m")))
    else:
        before = close

    missing = ds.missing_history(uni.keys(), INTERVAL, before)
    if not missing:
        print(f"[준비] RSI 과거 캐시 이미 준비됨: {len(uni)}종목")
        return 0

    print(f"[준비] 기존 분봉으로 RSI 워밍업 캐시 생성: {len(missing)}종목")
    frames = sw.fetch_intraday(missing, INTERVAL)
    failed = ds.seed_history_from_frames(frames, INTERVAL, before)
    not_returned = [t for t in missing if t not in frames]
    for t in not_returned:
        failed[t] = "기존 분봉 다운로드 실패"
    success = len(missing) - len(failed)
    print(f"[준비] RSI 캐시 완료: 성공 {success} / 실패 {len(failed)}")
    if failed:
        for ticker, reason in list(failed.items())[:20]:
            print(f" - {ticker}: {reason}")
    return 0 if success > 0 else 1


def _kis_test(code: str) -> int:
    """과거분봉 없이 KIS 당일분봉 1종목만 통신 테스트한다."""
    now = datetime.now(sw.KST)
    close = latest_scheduled_close(now, INTERVAL)
    if close is None:
        print("아직 첫 확정봉 전입니다. 장중 09:30 이후 다시 테스트하세요.")
        return 0
    step = int(INTERVAL.rstrip("m"))
    start = close - timedelta(minutes=step)
    client = KISClient()
    try:
        bar = client.fetch_interval_bar(code, start, close, INTERVAL)
    except Exception as e:
        print(f"[KIS 테스트 실패] {e}")
        return 1
    if bar.empty:
        print("[KIS 테스트 실패] 대상 확정봉 데이터가 없습니다.")
        return 1
    r = bar.iloc[-1]
    print(f"[KIS 테스트 성공] {code} / {start:%H:%M}~{(close-timedelta(minutes=1)):%H:%M} / "
          f"종가 {float(r['Close']):,.0f}원")
    return 0


def _run_single() -> int:
    now = datetime.now(sw.KST)
    close = latest_scheduled_close(now, INTERVAL)
    if close is None:
        print("아직 첫 30/60분봉 마감 전입니다.")
        return 0
    age = (now - close).total_seconds()
    if age > GRACE_SEC:
        print(f"최신 봉 확정 후 {age:.0f}초 경과 — 늦은 신호 방지를 위해 실행하지 않습니다.")
        return 0
    return run_once(expected_close=close)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true", help="현재 시각의 최신 마감봉을 1회 검사")
    p.add_argument("--bootstrap-only", action="store_true", help="기존 분봉으로 RSI 워밍업 캐시만 준비")
    p.add_argument("--kis-test", metavar="CODE", help="KIS 당일분봉 1종목 통신 테스트 (예: 005930)")
    args = p.parse_args()

    if args.bootstrap_only:
        return _bootstrap()
    if args.kis_test:
        return _kis_test(args.kis_test.strip())
    if args.once:
        return _run_single()

    try:
        # 시작 시 인증 오류를 즉시 확인한다.
        client = KISClient()
        client.access_token()
    except KISError as e:
        print(f"[실패] KIS 인증: {e}")
        return 1

    # 정시가 되기 전에 RSI 과거분봉을 미리 준비한다.
    # 캐시가 이미 있으면 빠르게 끝나며, 최초 실행만 시간이 걸린다.
    if os.getenv("KIS_AUTO_BOOTSTRAP", "1").strip().lower() not in ("0", "false", "off"):
        print("[준비] RSI 워밍업 캐시 확인/보충 (기존 분봉 사용)")
        _bootstrap()

    print("=" * 92)
    print(f" 단타 RSI KIS 정시 알림 실행기 / {INTERVAL}")
    print(" 30분봉 기준 09:30부터 15:30까지 봉 마감 직후 실행")
    print(f" 봉 마감 +{DELAY_SEC}초 실행 / 늦은 실행 차단 {GRACE_SEC}초")
    print(" 종료: Ctrl+C")
    print("=" * 92)

    last_run: str | None = None
    while True:
        now = datetime.now(sw.KST)
        if now.weekday() < 5:
            for close in scheduled_closes(now.date(), INTERVAL):
                due = close + timedelta(seconds=DELAY_SEC)
                deadline = close + timedelta(seconds=GRACE_SEC)
                key = close.isoformat()
                if due <= now <= deadline and key != last_run:
                    print(f"\n[정시 실행] {close:%Y-%m-%d %H:%M} 확정봉 / 실제 시작 {now:%H:%M:%S}")
                    try:
                        rc = run_once(expected_close=close)
                        print(f"[완료] return={rc} / {datetime.now(sw.KST):%H:%M:%S}")
                    except Exception as e:
                        print(f"[오류] 정시 스캔 실패: {e}")
                    last_run = key
                    break
        time.sleep(1.0 if 9 <= now.hour <= 16 else 10.0)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n사용자 종료")
        sys.exit(0)
