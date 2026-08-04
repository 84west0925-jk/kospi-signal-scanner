#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
swing_rsi.py — 단타(스윙) RSI 3분할 매매 엔진
────────────────────────────────────────────────────────────────────────────
전략 요약
  · 유니버스 : KOSPI 시가총액 상위 50종목 (FDR 실시간 조회, 실패 시 고정 리스트)
  · 봉       : 30분봉 / 60분봉 선택
  · 진입     : RSI(14) < 30  → 시드의 1/3 매수 (1차)
               1차 매수가 대비 -5%  → 1/3 추가 (2차)
               1차 매수가 대비 -10% → 1/3 추가 (3차, 시드 소진)
  · 청산     : RSI(14) > 70  → 보유의 1/3 매도 (1차)
               1차 매도가 대비 +5%  → 1/3 매도 (2차)
               1차 매도가 대비 +10% → 잔량 전량 (3차)
  · 리스크   : 평단 대비 -15% 이탈 시 강제 청산 경고 (물타기 무한루프 차단)

이 파일은 Streamlit 앱(app.py)과 알림 봇(alert_bot.py)이 공유한다.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd
import yfinance as yf

KST = timezone(timedelta(hours=9))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "swing_positions.json")

# ── 전략 파라미터 (기본값) ────────────────────────────────────────────────────
RSI_PERIOD      = 14
RSI_BUY         = 30.0     # 과매도 진입선
RSI_SELL        = 70.0     # 과매수 청산선
ADD_BUY_STEP_1  = -5.0     # 1차 매수가 대비 %
ADD_BUY_STEP_2  = -10.0
ADD_SELL_STEP_1 = 5.0      # 1차 매도가 대비 %
ADD_SELL_STEP_2 = 10.0
HARD_STOP_PCT   = -15.0    # 평단 대비 강제 손절선
INTERVAL_MAP    = {"30분봉": "30m", "60분봉": "60m"}

# ── KOSPI 시총 50위 폴백 (FDR 조회 실패 시 사용) ──────────────────────────────
TOP50_FALLBACK = {
    "005930": "삼성전자",        "000660": "SK하이닉스",
    "373220": "LG에너지솔루션",  "207940": "삼성바이오로직스",
    "005380": "현대차",          "005935": "삼성전자우",
    "000270": "기아",            "068270": "셀트리온",
    "105560": "KB금융",          "329180": "HD현대중공업",
    "012450": "한화에어로스페이스","005490": "POSCO홀딩스",
    "055550": "신한지주",        "035420": "NAVER",
    "028260": "삼성물산",        "012330": "현대모비스",
    "034020": "두산에너빌리티",  "042660": "한화오션",
    "009540": "HD한국조선해양",  "015760": "한국전력",
    "086790": "하나금융지주",    "035720": "카카오",
    "051910": "LG화학",          "032830": "삼성생명",
    "138040": "메리츠금융지주",  "010130": "고려아연",
    "316140": "우리금융지주",    "259960": "크래프톤",
    "011200": "HMM",             "006400": "삼성SDI",
    "033780": "KT&G",            "096770": "SK이노베이션",
    "017670": "SK텔레콤",        "018260": "삼성에스디에스",
    "030200": "KT",              "003670": "포스코퓨처엠",
    "066570": "LG전자",          "011070": "LG이노텍",
    "010140": "삼성중공업",      "267250": "HD현대",
    "090430": "아모레퍼시픽",    "047810": "한국항공우주",
    "064350": "현대로템",        "272210": "한화시스템",
    "251270": "넷마블",          "377300": "카카오페이",
    "009150": "삼성전기",        "024110": "기업은행",
    "323410": "카카오뱅크",      "402340": "SK스퀘어",
}


# ══════════════════════════════════════════════════════════════════════════════
# 유니버스
# ══════════════════════════════════════════════════════════════════════════════
def get_top50() -> dict[str, str]:
    """KOSPI 시총 상위 50 {'005930.KS': '삼성전자'} 반환. 실패 시 폴백."""
    try:
        import FinanceDataReader as fdr
        df = fdr.StockListing("KOSPI")
        cap_col = next((c for c in ("Marcap", "MarketCap", "Marketcap") if c in df.columns), None)
        code_col = next((c for c in ("Code", "Symbol") if c in df.columns), None)
        if cap_col and code_col:
            df = df.dropna(subset=[cap_col])
            # 우선주·스팩·리츠 제외
            df = df[~df["Name"].str.contains("우$|스팩|리츠|홀딩스우", regex=True, na=False)]
            top = df.sort_values(cap_col, ascending=False).head(50)
            out = {f"{str(r[code_col]).zfill(6)}.KS": r["Name"] for _, r in top.iterrows()}
            if len(out) >= 30:
                return out
    except Exception:
        pass
    return {f"{c}.KS": n for c, n in TOP50_FALLBACK.items()}


# ══════════════════════════════════════════════════════════════════════════════
# 지표
# ══════════════════════════════════════════════════════════════════════════════
def rsi_wilder(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """Wilder 방식 RSI — HTS/증권사 기본값과 동일."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50.0)


def fetch_intraday(tickers: list[str], interval: str = "30m") -> dict[str, pd.DataFrame]:
    """분봉 일괄 다운로드. yfinance 제한: 30m=최근 60일, 60m=최근 730일."""
    period = "60d" if interval == "30m" else "180d"
    raw = yf.download(
        tickers, period=period, interval=interval,
        progress=False, auto_adjust=True, group_by="ticker", threads=True,
    )
    out: dict[str, pd.DataFrame] = {}
    for t in tickers:
        try:
            df = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
            df = df.dropna(subset=["Close"])
            if len(df) >= RSI_PERIOD * 3:
                out[t] = df
        except Exception:
            continue
    return out


def build_indicators(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["RSI"] = rsi_wilder(d["Close"])
    d["MA20"] = d["Close"].rolling(20).mean()
    return d


# ══════════════════════════════════════════════════════════════════════════════
# 포지션 상태 (JSON 영속화 — Streamlit 앱과 알림 봇이 공유)
# ══════════════════════════════════════════════════════════════════════════════
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"positions": {}, "history": [], "updated": None}


def save_state(state: dict) -> None:
    state["updated"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def new_position(name: str) -> dict:
    return {
        "name": name,
        "buy_stage": 0,        # 0~3 (매수 진행 단계)
        "sell_stage": 0,       # 0~3 (매도 진행 단계)
        "first_buy": None,     # 1차 매수가 (분할매수 기준가)
        "first_sell": None,    # 1차 매도가 (분할매도 기준가)
        "entries": [],         # [{"stage":1,"price":..,"time":..}]
        "exits": [],
        "avg_price": None,
        "opened": None,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 신호 판정 — 핵심 로직
# ══════════════════════════════════════════════════════════════════════════════
def decide(pos: dict | None, price: float, rsi_now: float, rsi_prev: float,
           rsi_buy: float = RSI_BUY, rsi_sell: float = RSI_SELL,
           step1: float = ADD_BUY_STEP_1, step2: float = ADD_BUY_STEP_2,
           sstep1: float = ADD_SELL_STEP_1, sstep2: float = ADD_SELL_STEP_2) -> list[dict]:
    """
    현재 봉 기준 실행해야 할 액션 목록을 반환.
    반환: [{"action": "BUY"/"SELL"/"STOP", "stage": 1~3, "reason": str, "price": float}]
    """
    acts: list[dict] = []
    holding = pos is not None and pos["buy_stage"] > 0

    # ── 강제 손절 (평단 -15%) : 다른 모든 판정보다 우선 ─────────────────────
    if holding and pos.get("avg_price"):
        pnl = (price - pos["avg_price"]) / pos["avg_price"] * 100
        if pnl <= HARD_STOP_PCT:
            return [{"action": "STOP", "stage": 9, "price": price,
                     "reason": f"평단 대비 {pnl:+.1f}% — 손절선 {HARD_STOP_PCT}% 이탈, 전량 청산"}]

    # ── 매수 ────────────────────────────────────────────────────────────────
    if not holding:
        # 1차: RSI가 과매도선을 하향 돌파한 직후 (신규 진입)
        if rsi_now < rsi_buy and rsi_prev >= rsi_buy:
            acts.append({"action": "BUY", "stage": 1, "price": price,
                         "reason": f"RSI {rsi_prev:.1f}→{rsi_now:.1f}, 과매도({rsi_buy:.0f}) 진입 · 시드 1/3"})
    else:
        base = pos["first_buy"]
        drop = (price - base) / base * 100
        if pos["buy_stage"] == 1 and drop <= step1:
            acts.append({"action": "BUY", "stage": 2, "price": price,
                         "reason": f"1차가 대비 {drop:+.1f}% (기준 {step1}%) · 시드 1/3 추가"})
        elif pos["buy_stage"] == 2 and drop <= step2:
            acts.append({"action": "BUY", "stage": 3, "price": price,
                         "reason": f"1차가 대비 {drop:+.1f}% (기준 {step2}%) · 마지막 1/3 투입"})

    # ── 매도 ────────────────────────────────────────────────────────────────
    if holding:
        if pos["sell_stage"] == 0:
            if rsi_now > rsi_sell and rsi_prev <= rsi_sell:
                acts.append({"action": "SELL", "stage": 1, "price": price,
                             "reason": f"RSI {rsi_prev:.1f}→{rsi_now:.1f}, 과매수({rsi_sell:.0f}) 진입 · 보유 1/3 청산"})
        else:
            sbase = pos["first_sell"]
            rise = (price - sbase) / sbase * 100
            if pos["sell_stage"] == 1 and rise >= sstep1:
                acts.append({"action": "SELL", "stage": 2, "price": price,
                             "reason": f"1차 매도가 대비 {rise:+.1f}% (기준 +{sstep1}%) · 1/3 추가 청산"})
            elif pos["sell_stage"] == 2 and rise >= sstep2:
                acts.append({"action": "SELL", "stage": 3, "price": price,
                             "reason": f"1차 매도가 대비 {rise:+.1f}% (기준 +{sstep2}%) · 잔량 전량 청산"})
    return acts


def apply_action(state: dict, ticker: str, name: str, act: dict, ts: str,
                 unit_krw: float | None = None) -> dict:
    """액션을 상태에 반영하고 갱신된 포지션을 반환.
    unit_krw: 1회 투입금액. 주면 수량까지 기록해 손익 계산이 가능해진다."""
    pos = state["positions"].get(ticker) or new_position(name)
    price = act["price"]
    qty = int(unit_krw // price) if unit_krw and price > 0 else 0

    if act["action"] == "BUY":
        pos["buy_stage"] = act["stage"]
        pos["entries"].append({"stage": act["stage"], "price": price,
                               "qty": qty, "time": ts})
        pos.setdefault("source", "bot")
        if act["stage"] == 1:
            pos["first_buy"] = price
            pos["opened"] = ts
        tot_q = sum(e.get("qty", 0) for e in pos["entries"])
        if tot_q:
            pos["avg_price"] = round(
                sum(e["price"] * e.get("qty", 0) for e in pos["entries"]) / tot_q, 2)
        else:
            prices = [e["price"] for e in pos["entries"]]
            pos["avg_price"] = round(sum(prices) / len(prices), 2)
        state["positions"][ticker] = pos

    elif act["action"] == "SELL":
        held = sum(e.get("qty", 0) for e in pos["entries"]) - \
               sum(x.get("qty", 0) for x in pos.get("exits", []))
        sell_q = held if act["stage"] >= 3 else held // (4 - act["stage"])
        pos["sell_stage"] = act["stage"]
        pos["exits"].append({"stage": act["stage"], "price": price,
                             "qty": max(sell_q, 0), "time": ts})
        if act["stage"] == 1:
            pos["first_sell"] = price
        if act["stage"] >= 3:
            _close(state, ticker, pos, price, ts, "3차 청산 완료")
            return pos
        state["positions"][ticker] = pos

    elif act["action"] == "STOP":
        held = sum(e.get("qty", 0) for e in pos["entries"]) - \
               sum(x.get("qty", 0) for x in pos.get("exits", []))
        pos["exits"].append({"stage": 9, "price": price,
                             "qty": max(held, 0), "time": ts})
        _close(state, ticker, pos, price, ts, "손절 청산")
        return pos

    return pos


def _close(state: dict, ticker: str, pos: dict, price: float, ts: str, note: str) -> None:
    avg = pos.get("avg_price") or price
    state["history"].append({
        "ticker": ticker, "name": pos["name"], "opened": pos.get("opened"),
        "closed": ts, "avg_buy": avg, "last_sell": price,
        "pnl_pct": round((price - avg) / avg * 100, 2), "note": note,
    })
    state["positions"].pop(ticker, None)


# ══════════════════════════════════════════════════════════════════════════════
# 스캔 — 전 종목 1회 평가
# ══════════════════════════════════════════════════════════════════════════════
def scan(universe: dict[str, str], interval: str = "30m",
         rsi_buy: float = RSI_BUY, rsi_sell: float = RSI_SELL,
         state: dict | None = None, commit: bool = False) -> tuple[pd.DataFrame, list[dict]]:
    """
    전 종목 RSI 평가 + 액션 도출.
    commit=True 이면 state에 반영 후 저장(알림 봇용). False면 조회만(앱 화면용).
    """
    state = state if state is not None else load_state()
    data = fetch_intraday(list(universe.keys()), interval)

    rows, alerts = [], []
    for ticker, df in data.items():
        name = universe.get(ticker, ticker)
        d = build_indicators(df)
        if len(d) < 2:
            continue
        price = float(d["Close"].iloc[-1])
        rsi_now = float(d["RSI"].iloc[-1])
        rsi_prev = float(d["RSI"].iloc[-2])
        ts = d.index[-1].strftime("%Y-%m-%d %H:%M")

        pos = state["positions"].get(ticker)
        acts = decide(pos, price, rsi_now, rsi_prev, rsi_buy, rsi_sell)

        for a in acts:
            a.update({"ticker": ticker, "name": name, "time": ts, "rsi": round(rsi_now, 1)})
            alerts.append(a)
            if commit:
                pos = apply_action(state, ticker, name, a, ts)

        pos = state["positions"].get(ticker)
        rows.append({
            "종목": name,
            "코드": ticker.replace(".KS", ""),
            "현재가": round(price, 1),
            "RSI": round(rsi_now, 1),
            "직전RSI": round(rsi_prev, 1),
            "구간": ("과매도" if rsi_now < rsi_buy else "과매수" if rsi_now > rsi_sell else "중립"),
            "보유단계": f"{pos['buy_stage']}/3" if pos else "-",
            "평단": pos["avg_price"] if pos else None,
            "수익률%": (round((price - pos["avg_price"]) / pos["avg_price"] * 100, 2)
                        if pos and pos.get("avg_price") else None),
            "매도단계": f"{pos['sell_stage']}/3" if pos and pos["sell_stage"] else "-",
            "신호": " / ".join(f"{a['action']}{a['stage']}" for a in acts) or "",
            "시각": ts,
        })

    if commit:
        save_state(state)

    df_out = pd.DataFrame(rows)
    if not df_out.empty:
        df_out = df_out.sort_values("RSI").reset_index(drop=True)
    return df_out, alerts


def format_alert(a: dict) -> str:
    """카카오/텔레그램 전송용 메시지 1건."""
    icon = {"BUY": "🟢 매수", "SELL": "🔴 매도", "STOP": "⛔ 손절"}[a["action"]]
    stage = "전량" if a["stage"] == 9 else f"{a['stage']}차"
    return (f"{icon} {stage} · {a['name']}({a['ticker'].replace('.KS','')})\n"
            f"가격 {a['price']:,.0f}원 | RSI {a['rsi']}\n"
            f"{a['reason']}\n{a['time']}")


# ══════════════════════════════════════════════════════════════════════════════
# 백테스트 — 동일 로직을 과거 분봉에 그대로 적용
# ══════════════════════════════════════════════════════════════════════════════
def backtest(df: pd.DataFrame, rsi_buy: float = RSI_BUY, rsi_sell: float = RSI_SELL,
             seed: float = 3_000_000) -> tuple[pd.DataFrame, dict]:
    d = build_indicators(df).dropna(subset=["RSI"])
    pos, trades, unit = None, [], seed / 3
    for i in range(1, len(d)):
        price = float(d["Close"].iloc[i])
        r_now, r_prev = float(d["RSI"].iloc[i]), float(d["RSI"].iloc[i - 1])
        ts = d.index[i]
        for a in decide(pos, price, r_now, r_prev, rsi_buy, rsi_sell):
            if a["action"] == "BUY":
                pos = pos or new_position("BT")
                pos["buy_stage"] = a["stage"]
                pos["entries"].append({"stage": a["stage"], "price": price, "time": str(ts)})
                if a["stage"] == 1:
                    pos["first_buy"], pos["opened"] = price, ts
                pos["avg_price"] = sum(e["price"] for e in pos["entries"]) / len(pos["entries"])
            elif a["action"] == "SELL":
                pos["sell_stage"] = a["stage"]
                if a["stage"] == 1:
                    pos["first_sell"] = price
                if a["stage"] >= 3:
                    trades.append(_bt_close(pos, price, ts, "3차청산"))
                    pos = None
            elif a["action"] == "STOP":
                trades.append(_bt_close(pos, price, ts, "손절"))
                pos = None

    tdf = pd.DataFrame(trades)
    if tdf.empty:
        return tdf, {"거래수": 0, "승률%": 0, "평균손익%": 0, "누적손익%": 0, "최대손실%": 0}
    stats = {
        "거래수": len(tdf),
        "승률%": round((tdf["pnl_pct"] > 0).mean() * 100, 1),
        "평균손익%": round(tdf["pnl_pct"].mean(), 2),
        "누적손익%": round(tdf["pnl_pct"].sum(), 2),
        "최대손실%": round(tdf["pnl_pct"].min(), 2),
    }
    return tdf, stats


def _bt_close(pos: dict, price: float, ts, note: str) -> dict:
    avg = pos["avg_price"]
    return {"진입": str(pos["opened"])[:16], "청산": str(ts)[:16],
            "분할차수": pos["buy_stage"], "평단": round(avg, 1),
            "청산가": round(price, 1), "pnl_pct": round((price - avg) / avg * 100, 2),
            "사유": note}


# ══════════════════════════════════════════════════════════════════════════════
# Streamlit 화면
# ══════════════════════════════════════════════════════════════════════════════
def render(st):
    st.title("⚡ 단타 RSI 3분할 시스템")
    st.caption("KOSPI 시총 50위 · 30/60분봉 · RSI 30 과매도 매수 / 70 과매수 매도 · 시드 3분할")

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        tf_label = st.selectbox("차트 주기", list(INTERVAL_MAP.keys()), index=0)
    with c2:
        rsi_buy = st.number_input("매수 RSI(과매도)", 10.0, 45.0, RSI_BUY, 1.0)
    with c3:
        rsi_sell = st.number_input("매도 RSI(과매수)", 55.0, 90.0, RSI_SELL, 1.0)
    with c4:
        seed = st.number_input("총 시드(원)", 300_000, 1_000_000_000, 3_000_000, 100_000)
    interval = INTERVAL_MAP[tf_label]

    st.info(
        f"**분할 규칙** — 매수: RSI<{rsi_buy:.0f} 진입 시 {seed/3:,.0f}원 → 1차가 대비 -5% 추가 → -10% 추가 "
        f"／ 매도: RSI>{rsi_sell:.0f} 시 1/3 → 1차 매도가 +5% → +10% 전량 "
        f"／ 안전장치: 평단 -15% 이탈 시 강제 청산"
    )

    state = load_state()
    run = st.button("🔍 단타 신호 스캔", type="primary", use_container_width=True)

    if run:
        with st.spinner(f"KOSPI 시총 50위 {tf_label} 조회 중..."):
            universe = get_top50()
            df, alerts = scan(universe, interval, rsi_buy, rsi_sell, state, commit=False)
        st.session_state["swing_df"] = df
        st.session_state["swing_alerts"] = alerts

    df = st.session_state.get("swing_df")
    alerts = st.session_state.get("swing_alerts", [])

    if df is None:
        st.warning("‘단타 신호 스캔’을 눌러 시작하세요.")
        return

    if alerts:
        st.subheader(f"🚨 지금 실행할 액션 {len(alerts)}건")
        for a in alerts:
            (st.success if a["action"] == "BUY" else st.error)(format_alert(a).replace("\n", " ｜ "))
    else:
        st.success("현재 트리거된 매매 신호 없음 — 대기")

    m1, m2, m3 = st.columns(3)
    m1.metric("과매도 종목", int((df["구간"] == "과매도").sum()))
    m2.metric("과매수 종목", int((df["구간"] == "과매수").sum()))
    m3.metric("보유 포지션", len(state["positions"]))

    t1, t2, t3, t4 = st.tabs(["📋 전체 현황", "🟢 과매도 후보", "💼 보유 포지션", "🧪 백테스트"])
    with t1:
        st.dataframe(df, use_container_width=True, hide_index=True)
    with t2:
        st.dataframe(df[df["구간"] == "과매도"], use_container_width=True, hide_index=True)
    with t3:
        if state["positions"]:
            st.dataframe(pd.DataFrame([
                {"종목": p["name"], "매수단계": f"{p['buy_stage']}/3", "평단": p["avg_price"],
                 "1차가": p["first_buy"], "매도단계": f"{p['sell_stage']}/3", "진입": p["opened"]}
                for p in state["positions"].values()
            ]), use_container_width=True, hide_index=True)
        else:
            st.info("보유 포지션 없음 (알림 봇이 실행되면 자동 기록됩니다)")
        if state["history"]:
            st.markdown("**청산 이력**")
            st.dataframe(pd.DataFrame(state["history"]), use_container_width=True, hide_index=True)
    with t4:
        universe = get_top50()
        pick = st.selectbox("백테스트 종목", list(universe.values()))
        if st.button("백테스트 실행"):
            tk = [k for k, v in universe.items() if v == pick][0]
            with st.spinner("과거 분봉 재현 중..."):
                data = fetch_intraday([tk], interval)
                if tk in data:
                    trades, stats = backtest(data[tk], rsi_buy, rsi_sell, seed)
                    cols = st.columns(len(stats))
                    for col, (k, v) in zip(cols, stats.items()):
                        col.metric(k, v)
                    st.dataframe(trades, use_container_width=True, hide_index=True)
                    st.caption("※ 30분봉은 최근 60일, 60분봉은 최근 180일 구간만 검증 가능 (yfinance 제한). "
                               "수수료·세금·슬리피지 미반영.")
                else:
                    st.error("데이터 조회 실패")
