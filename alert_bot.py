#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
alert_bot.py — 단타 RSI 신호 감시 + 텔레그램/카카오 알림
────────────────────────────────────────────────────────────────────────────
두 가지 경로를 모두 지원한다(병행 운영).

  [KIS 경로]  KIS_APP_KEY / KIS_APP_SECRET 가 있으면 한국투자증권 공식 분봉으로
              '이번에 막 확정된 봉'만 정확히 판정한다. PC 상시 실행용.
              → python kis_alert_runner.py

  [백업 경로] KIS 키가 없으면 기존 yfinance 분봉으로 마감봉을 판정한다.
              PC가 꺼져 있을 때를 대비한 GitHub Actions 백업용.
              → python alert_bot.py

전략·RSI·포지션 로직은 두 경로 모두 swing_rsi.py 를 그대로 쓴다.
중복 발송은 swing_positions.json 의 alerted 기록을 두 경로가 공유해 차단한다.
기록은 **알림 전송에 성공한 뒤에만** 남긴다.

이 봇은 포지션을 만들지 않는다. 실제 매수분은 웹 화면에서 직접 등록한다.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import portfolio
import swing_rsi as sw
from kakao_notify import send_text

INTERVAL = os.getenv("INTERVAL", "30m")
RSI_BUY = float(os.getenv("RSI_BUY", sw.RSI_BUY))
RSI_SELL = float(os.getenv("RSI_SELL", sw.RSI_SELL))
SEED = float(os.getenv("SEED", 3_000_000))
KOSPI_N = int(os.getenv("KOSPI_N", 100))
KOSDAQ_N = int(os.getenv("KOSDAQ_N", 10))
KOSDAQ_LIMIT = int(os.getenv("KOSDAQ_LIMIT", 5))
MAX_SIGNAL_AGE_SEC = int(os.getenv("KIS_ALERT_GRACE_SEC", "55"))
IN_ACTIONS = os.getenv("GITHUB_ACTIONS", "").lower() == "true"

_FORCE_RAW = os.getenv("FORCE", "0").strip().lower()
FORCE = _FORCE_RAW in ("1", "true", "test")
TEST = _FORCE_RAW == "test" or os.getenv("TEST", "0") == "1"


# ══════════════════════════════════════════════════════════════════════════════
# 상태 공유 — PC와 GitHub Actions가 같은 alerted 기록을 본다
# ══════════════════════════════════════════════════════════════════════════════
def _load_shared_state() -> dict:
    """PC에서는 GitHub 원본을, Actions 안에서는 체크아웃된 로컬 파일을 쓴다."""
    try:
        return portfolio.load()
    except Exception as e:
        print(f"[state] 원격 로드 실패({e}) — 로컬 파일 사용")
        return sw.load_state()


def _save_shared_state(state: dict, message: str) -> bool:
    """PC에서는 GitHub Contents API로, Actions 안에서는 로컬 저장 후 워크플로가 커밋."""
    if not IN_ACTIONS:
        try:
            # 원격 갱신에는 원본 sha가 반드시 필요하다.
            # 원격 읽기에 실패해 로컬 fallback 상태가 온 경우 잘못된 덮어쓰기를 하지 않는다.
            if portfolio.writable() and "_sha" in state:
                ok, msg = portfolio.save(state, message)
                print("[state]", msg)
                if ok:
                    local = dict(state)
                    local.pop("_sha", None)
                    sw.save_state(local)
                    return True
        except Exception as e:
            print(f"[state] GitHub 저장 경고: {e}")

    local = dict(state)
    local.pop("_sha", None)
    sw.save_state(local)
    return True


def _mark_sent(state: dict, alerts: list[dict]) -> None:
    sent = state.setdefault("alerted", {})
    now = datetime.now(sw.KST).strftime("%Y-%m-%d %H:%M")
    for a in alerts:
        sent[a["_key"]] = now
    if len(sent) > 800:
        state["alerted"] = dict(sorted(sent.items(), key=lambda kv: kv[1])[-400:])


# ══════════════════════════════════════════════════════════════════════════════
# 시세 공급기 선택
# ══════════════════════════════════════════════════════════════════════════════
def _kis_source():
    """KIS 키가 있으면 공급기를, 없거나 실패하면 None을 돌려준다(→ 백업 경로)."""
    # GitHub Actions 는 '백업' 역할이다. 정시성이 보장되지 않아 KIS의 확정봉 시각
    # 판정과 맞지 않으므로, 기본적으로 yfinance 경로를 쓴다.
    # (굳이 Actions에서도 KIS를 쓰려면 KIS_IN_ACTIONS=1 로 명시한다.)
    if IN_ACTIONS and os.getenv("KIS_IN_ACTIONS", "0") != "1":
        return None
    if not (os.getenv("KIS_APP_KEY") and os.getenv("KIS_APP_SECRET")):
        return None
    try:
        from kis_market import KISClient, KISRSIDataSource
        return KISRSIDataSource(KISClient())
    except Exception as e:
        print(f"[KIS 사용 불가] {e} — yfinance 백업 경로로 전환합니다")
        return None


def _send(alerts: list[dict], stamp: datetime, tag: str) -> bool:
    unit = SEED / 3
    header = (f"📣 KOSPI 단타 신호 {stamp:%m/%d %H:%M} ({INTERVAL}{tag})\n"
              f"1회 투입금 {unit:,.0f}원 · 매수하셨다면 앱에 등록하세요\n")
    msg = header + "\n" + "\n\n".join(sw.format_alert(a) for a in alerts)
    ok = send_text(msg)
    print("알림 전송:", "성공" if ok else "실패")
    for a in alerts:
        print(" -", sw.format_alert(a).replace("\n", " | "))
    return ok


# ══════════════════════════════════════════════════════════════════════════════
# KIS 경로 — 이번에 막 확정된 봉만 판정
# ══════════════════════════════════════════════════════════════════════════════
def _run_kis(source, state: dict, universe: dict, now: datetime,
             expected_close: datetime | None) -> int:
    from kis_market import KISError, latest_scheduled_close

    expected_close = expected_close or latest_scheduled_close(now, INTERVAL)
    if expected_close is None:
        print("아직 첫 마감봉 전 — 종료")
        return 0
    if expected_close.tzinfo is None:
        expected_close = expected_close.replace(tzinfo=sw.KST)

    age = (now - expected_close).total_seconds()
    if age > MAX_SIGNAL_AGE_SEC and not FORCE:
        print(f"확정시각 {expected_close:%H:%M} 이후 {age:.0f}초 경과 — 늦은 신호 방지를 위해 종료")
        return 0

    print(f"대상 확정봉: {expected_close:%Y-%m-%d %H:%M} / 데이터: 한국투자증권 KIS")
    expected_start = expected_close - timedelta(minutes=sw.INTERVAL_MIN.get(INTERVAL, 30))

    # 과거 캐시가 없으면 RSI 워밍업분만 기존 분봉으로 1회 보충한다.
    missing = source.missing_history(universe.keys(), INTERVAL, expected_start)
    if missing:
        print(f"[준비] RSI 워밍업 캐시 보충 {len(missing)}종목")
        source.seed_history_from_frames(sw.fetch_intraday(missing, INTERVAL),
                                        INTERVAL, expected_start)

    try:
        result = source.get_signal_frames(universe.keys(), INTERVAL, expected_close)
    except KISError as e:
        print(f"[KIS 오류] {e} — yfinance 백업 경로로 재시도합니다")
        return _run_yfinance(state, universe, now)

    print(f"KIS 조회 {len(result.frames)}/{len(universe)}종목 · 실패 {len(result.failed)}종목")
    for ticker, reason in list(result.failed.items())[:10]:
        print(f" - 조회 실패 {ticker}: {reason}")
    if not result.frames:
        print("KIS 분봉 데이터 없음 — 종료")
        return 0

    df, alerts = sw.scan(universe, INTERVAL, RSI_BUY, RSI_SELL, state,
                         commit=False, kosdaq_limit=KOSDAQ_LIMIT,
                         closed_only=False, data_override=result.frames,
                         expected_close=expected_close, filter_alerted=True)
    return _finish(df, alerts, state, expected_close, " · KIS")


# ══════════════════════════════════════════════════════════════════════════════
# 백업 경로 — yfinance 마감봉 판정 (GitHub Actions)
# ══════════════════════════════════════════════════════════════════════════════
def _run_yfinance(state: dict, universe: dict, now: datetime) -> int:
    print("데이터: yfinance 마감봉 (백업 경로)")
    df, alerts = sw.scan(universe, INTERVAL, RSI_BUY, RSI_SELL, state,
                         commit=False, kosdaq_limit=KOSDAQ_LIMIT,
                         closed_only=True, filter_alerted=True)
    if df.empty:
        print("데이터 없음 — 종료")
        return 0

    # 휴장 판정: 최신 마감봉 날짜가 오늘이 아니면 장이 열리지 않은 것.
    # 개장 직후(09:35 이전)에는 오늘 마감된 봉이 아직 없으므로 검사하지 않는다.
    last_day = str(df["시각"].max())[:10]
    after_first_close = (now.hour * 60 + now.minute) >= (9 * 60 + 35)
    if last_day != now.strftime("%Y-%m-%d") and after_first_close and not FORCE:
        print(f"최신 마감봉 {last_day} ≠ 오늘 — 휴장으로 간주, 종료")
        return 0

    return _finish(df, alerts, state, now, "")


def _finish(df, alerts: list[dict], state: dict, stamp: datetime, tag: str) -> int:
    if df.empty:
        print("판정 데이터 없음 — 종료")
        return 0

    print(f"과매도 {int((df['구간']=='과매도').sum())} · 과매수 {int((df['구간']=='과매수').sum())} · "
          f"보유 {len(state.get('positions', {}))} · 신규 액션 {len(alerts)}")
    if not alerts:
        print("트리거 없음 — 알림 미발송")
        return 0

    if not _send(alerts, stamp, tag):
        return 1

    # 중요: 실제 전송에 성공한 뒤에만 중복방지 기록을 남긴다.
    _mark_sent(state, alerts)
    if not _save_shared_state(state, f"alert: {stamp:%Y-%m-%d %H:%M} 신호 기록"):
        print("[경고] 알림은 발송됐으나 중복방지 상태 저장에 실패했습니다.")
        return 1
    return 0


# ══════════════════════════════════════════════════════════════════════════════
def run_once(expected_close: datetime | None = None) -> int:
    now = datetime.now(sw.KST)
    print(f"=== 단타 RSI 스캔 {now:%Y-%m-%d %H:%M:%S} KST / {INTERVAL} ===")

    if TEST:
        print("[TEST 모드] 알림 채널 연결 테스트 메시지 발송")
        ok = send_text(f"✅ 알림 봇 연결 테스트 성공 ({now:%m/%d %H:%M})")
        print("알림 전송:", "성공" if ok else "실패")
        return 0 if ok else 1

    if now.weekday() >= 5 and not FORCE:
        print("주말 — 종료")
        return 0

    state = _load_shared_state()
    universe = sw.get_universe(KOSPI_N, KOSDAQ_N)
    n_kq = sum(1 for t in universe if sw.market_of(t) == "KOSDAQ")
    print(f"유니버스 {len(universe)}종목 (코스피 {len(universe)-n_kq} / 코스닥 {n_kq})")

    source = _kis_source()
    if source is not None:
        return _run_kis(source, state, universe, now, expected_close)
    return _run_yfinance(state, universe, now)


def main() -> int:
    return run_once()


if __name__ == "__main__":
    sys.exit(main())
