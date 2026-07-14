#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🎯 Smart Money Radar
기관·외국인 자금 흐름 기반 매매 전략 플랫폼.
- 등급 분류(S~D) · Smart Money Score · 순매수비율 · 연속 순매수
- Early Entry / Exit Signal · Sector Rotation · Timeline 애니메이션
- 백테스트(최근 ~120거래일) · 전략 시뮬레이터 · 관심종목 알림

데이터: 네이버 금융(수급, 근사치) + yfinance(가격/거래량 이력)
"""
import io
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import numpy as np
import pandas as pd
import requests
import streamlit as st

try:
    import plotly.express as px
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    PLOTLY_OK = True
except ImportError:
    PLOTLY_OK = False

# 공용 데이터 레이어 재사용 (전 종목 시세, 수급 API, 시장 동향)
from smart_money import (
    EOK, NAVER_HDR, PLOTLY_TMPL, DARK_CSS, THEME_KEYWORDS,
    _n, _fetch_trend_rows, classify_sector, eokify, style_table,
    fmt_eok, load_all_stocks, load_market_trend, load_index_value,
)

try:
    import derivatives
    DERIV_OK = True
except Exception:
    DERIV_OK = False

RADAR_N = 250        # 분석 대상: 거래대금 상위 250종목 (KOSPI250 근사)
HIST_DAYS = 120      # 수급 이력 조회 일수

GRADE_STARS = {"S": "★★★★★", "A": "★★★★☆", "B": "★★★☆☆", "C": "★★☆☆☆", "D": "★☆☆☆☆"}
GRADE_LABEL = {"S": "Strong Buy", "A": "Buy", "B": "Watch", "C": "관망", "D": "Exit"}

# ═══════════════════════════════════════════════════════
# 데이터 로딩
# ═══════════════════════════════════════════════════════
@st.cache_data(ttl=900, show_spinner=False)
def load_flows_history(codes: tuple, days: int = HIST_DAYS) -> pd.DataFrame:
    """종목별 일별 투자자 순매수 이력 (long format, 근사 금액)"""
    def one(code):
        try:
            rows = _fetch_trend_rows(code, days)
            return [(code, x["bizdate"],
                     _n(x["foreignerPureBuyQuant"]) * _n(x["closePrice"]),
                     _n(x["organPureBuyQuant"]) * _n(x["closePrice"]),
                     _n(x["foreignerHoldRatio"])) for x in rows]
        except Exception:
            return []
    with ThreadPoolExecutor(max_workers=12) as ex:
        res = list(ex.map(one, codes))
    flat = [r for sub in res for r in sub]
    df = pd.DataFrame(flat, columns=["종목코드", "날짜", "외국인", "기관", "보유율"])
    if df.empty:
        return df
    df["날짜"] = pd.to_datetime(df["날짜"])
    return df.sort_values(["종목코드", "날짜"])

@st.cache_data(ttl=3600, show_spinner=False)
def load_price_history(codes: tuple) -> pd.DataFrame:
    """yfinance 배치 — 일별 종가/거래량/고가 (long format, 1년)"""
    import yfinance as yf
    frames = []
    codes = list(codes)
    for i in range(0, len(codes), 80):
        chunk = codes[i:i + 80]
        symbols = [c + ".KS" for c in chunk]
        try:
            raw = yf.download(symbols, period="1y", interval="1d", progress=False,
                              auto_adjust=True, group_by="ticker", threads=True)
        except Exception:
            continue
        for c, s in zip(chunk, symbols):
            try:
                sub = raw[s] if isinstance(raw.columns, pd.MultiIndex) else raw
                sub = sub[["Close", "Volume", "High"]].dropna()
                if sub.empty:
                    continue
                t = sub.reset_index()
                t.columns = ["날짜", "종가", "거래량", "고가"]
                t["종목코드"] = c
                frames.append(t)
            except Exception:
                continue
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df["날짜"] = pd.to_datetime(df["날짜"]).dt.tz_localize(None)
    df["거래대금"] = df["종가"] * df["거래량"]
    return df.sort_values(["종목코드", "날짜"])

def _pivot(df, col):
    return df.pivot_table(index="날짜", columns="종목코드", values=col, aggfunc="last")

def _streak_from_end(bool_df: pd.DataFrame) -> pd.Series:
    """각 컬럼(종목)별, 마지막 행부터 연속 True 일수"""
    arr = bool_df.values[::-1]           # 최신이 첫 행
    out = np.zeros(arr.shape[1], dtype=int)
    active = np.ones(arr.shape[1], dtype=bool)
    for row in arr:
        good = row & active
        out += good.astype(int)
        active = good
        if not active.any():
            break
    return pd.Series(out, index=bool_df.columns)

# ═══════════════════════════════════════════════════════
# 지표 · 등급 · 신호 계산
# ═══════════════════════════════════════════════════════
@st.cache_data(ttl=900, show_spinner=False)
def build_metrics(codes: tuple, p: int) -> pd.DataFrame:
    """종목별 레이더 지표 (p = 수급 기준기간, 거래일)"""
    fl = load_flows_history(codes)
    pr = load_price_history(codes)
    if fl.empty or pr.empty:
        return pd.DataFrame()

    frg = _pivot(fl, "외국인")
    org = _pivot(fl, "기관")
    hold = _pivot(fl, "보유율")
    close = _pivot(pr, "종가")
    tv = _pivot(pr, "거래대금")

    common = sorted(set(frg.columns) & set(close.columns))
    frg, org = frg[common], org[common]
    close, tv = close[common], tv[common]

    ma20 = close.rolling(20).mean()
    hi20 = close.rolling(20).max()

    last_close = close.ffill().iloc[-1]
    last_ma20 = ma20.ffill().iloc[-1]
    last_hi20 = hi20.ffill().iloc[-1]

    frg_p = frg.tail(p).sum()
    org_p = org.tail(p).sum()
    tv_p = tv.tail(p).sum()
    tv_cur = tv.tail(p).mean()
    tv_prev = tv.iloc[-2 * p:-p].mean() if len(tv) >= 2 * p else tv.head(p).mean()
    tv_growth = (tv_cur / tv_prev.replace(0, np.nan) - 1) * 100

    vol = tv / close.replace(0, np.nan)
    vol_cur = vol.tail(p).mean()
    vol_prev = vol.iloc[-2 * p:-p].mean() if len(vol) >= 2 * p else vol.head(p).mean()
    vol_growth = (vol_cur / vol_prev.replace(0, np.nan) - 1) * 100

    tv5_up = tv.rolling(5).mean().ffill().iloc[-1] > tv.rolling(5).mean().shift(5).ffill().iloc[-1]
    tv_down5 = _streak_from_end(tv.diff() < 0) >= 5

    above_ma20 = last_close > last_ma20
    high20 = last_close >= last_hi20 * 0.999
    chg20 = (last_close / close.ffill().iloc[-21] - 1) * 100 if len(close) >= 21 else pd.Series(0, index=common)
    near_break = (~above_ma20) & (last_close >= last_ma20 * 0.97)

    frg_streaks = {d: _streak_from_end(frg.tail(d) > 0) for d in (3, 5, 10, 20)}
    org_streaks = {d: _streak_from_end(org.tail(d) > 0) for d in (3, 5, 10, 20)}
    frg_streak = _streak_from_end(frg > 0)
    org_streak = _streak_from_end(org > 0)

    m = pd.DataFrame({
        "외국인순매수": frg_p, "기관순매수": org_p,
        "거래대금증가율": tv_growth, "거래량증가율": vol_growth,
        "기간거래대금": tv_p,
        "외국인비율": (frg_p / tv_p.replace(0, np.nan) * 100).round(2),
        "기관비율": (org_p / tv_p.replace(0, np.nan) * 100).round(2),
        "20일선위": above_ma20, "20일신고가": high20,
        "돌파직전": near_break, "5일거래대금증가": tv5_up,
        "거래대금5일연속감소": tv_down5,
        "외국인연속": frg_streak, "기관연속": org_streak,
        "상승률20일": chg20.round(2),
        "외국인보유율": hold.ffill().iloc[-1] if not hold.empty else np.nan,
    })
    for d in (3, 5, 10, 20):
        m[f"외{d}일연속"] = frg_streaks[d] >= d
        m[f"기{d}일연속"] = org_streaks[d] >= d

    # ── Smart Money Score (100점) ──
    r = lambda s: s.rank(pct=True) * 100
    m["Score"] = (0.25 * r(m["거래대금증가율"].fillna(0))
                  + 0.25 * r(m["외국인순매수"])
                  + 0.25 * r(m["기관순매수"])
                  + 0.10 * r(m["거래량증가율"].fillna(0))
                  + 10 * m["20일신고가"].astype(int)
                  + 5 * m["20일선위"].astype(int)).clip(0, 100).round(1)
    m["Score등급"] = pd.cut(m["Score"], [-1, 60, 70, 80, 90, 101],
                          labels=["Weak", "중립", "Watch", "Buy", "Strong Buy"])

    # ── 등급 분류 (S~D) ──
    tv_top20 = m["거래대금증가율"].rank(pct=True) >= 0.80
    conds = [
        (m["외국인순매수"] > 0) & (m["기관순매수"] > 0) & tv_top20
        & m["5일거래대금증가"] & m["20일선위"] & m["20일신고가"],
        (m["외국인순매수"] > 0) & (m["기관순매수"] > 0)
        & (m["거래대금증가율"] > 0) & m["20일선위"],
        ((m["외국인순매수"] > 0) | (m["기관순매수"] > 0)) & (m["거래대금증가율"] > 0),
        (m["외국인순매수"] < 0) & (m["기관순매수"] < 0) & (m["거래대금증가율"] < 0),
    ]
    m["등급"] = np.select(conds, ["S", "A", "B", "D"], default="C")
    m["별점"] = m["등급"].map(GRADE_STARS)
    m["판정"] = m["등급"].map(GRADE_LABEL)

    # ── 신호 ──
    m["SmartAccum"] = (m["외3일연속"]) & (m["기3일연속"])
    m["EarlyEntry"] = ((m["외국인순매수"] > 0) & (m["기관순매수"] > 0)
                       & (m["거래대금증가율"] > 0) & (m["상승률20일"] <= 5)
                       & (m["돌파직전"] | m["20일선위"]))
    m["ExitSignal"] = ((m["외국인순매수"] < 0) & (m["기관순매수"] < 0)
                       & (m["거래대금증가율"] < 0) & (m["거래대금5일연속감소"])
                       & (~m["20일선위"]))
    m["신호"] = np.select(
        [m["ExitSignal"], m["EarlyEntry"] & m["SmartAccum"], m["EarlyEntry"], m["SmartAccum"]],
        ["🔴 Exit", "🔥🟢 Accum+Early", "🟢 Early Entry", "🔥 Smart Accumulation"], default="")
    m.index.name = "종목코드"
    return m.reset_index()

# ═══════════════════════════════════════════════════════
# 백테스트
# ═══════════════════════════════════════════════════════
@st.cache_data(ttl=1800, show_spinner=False)
def run_backtest(codes: tuple, threshold: float,
                 fut_filter: bool = False, prog_filter: bool = False,
                 mode: str = "signal"):
    """일별 Score 재계산 → threshold 이상 신호의 사후 수익률 분석"""
    fl = load_flows_history(codes)
    pr = load_price_history(codes)
    if fl.empty or pr.empty:
        return pd.DataFrame(), pd.DataFrame()

    frg = _pivot(fl, "외국인")
    org = _pivot(fl, "기관")
    close = _pivot(pr, "종가")
    tv = _pivot(pr, "거래대금")
    common = sorted(set(frg.columns) & set(close.columns))
    frg, org = frg[common], org[common]
    close, tv = close[common].ffill(), tv[common]

    # 수급 이력이 있는 날짜로 정렬
    close_a, tv_a = close.reindex(frg.index).ffill(), tv.reindex(frg.index)

    frg5 = frg.rolling(5).sum()
    org5 = org.rolling(5).sum()
    tv5 = tv_a.rolling(5).mean()
    tvg = (tv5 / tv5.shift(5) - 1) * 100
    vol5 = (tv_a / close_a).rolling(5).mean()
    volg = (vol5 / vol5.shift(5) - 1) * 100
    ma20 = close_a.rolling(20).mean()
    hi20 = close_a.rolling(20).max()
    above = (close_a > ma20).astype(int)
    high = (close_a >= hi20 * 0.999).astype(int)

    rk = lambda d: d.rank(axis=1, pct=True) * 100
    if mode == "accum":
        # Accumulation Score: 20일 누적 매집 기준
        frg20 = frg.rolling(20).sum()
        org20 = org.rolling(20).sum()
        tv20m = tv_a.rolling(20).mean()
        tvg20 = (tv20m / tv20m.shift(20) - 1) * 100
        gain20 = close_a / close_a.shift(20) - 1
        score = (0.30 * rk(frg20) + 0.30 * rk(org20) + 0.15 * rk(tvg20)
                 + 0.15 * (100 - rk(gain20)) + 0.10 * rk(frg5 + org5)).clip(0, 100)
        score = score.iloc[45:]                 # 롤링 워밍업 제외
    else:
        score = (0.25 * rk(tvg) + 0.25 * rk(frg5) + 0.25 * rk(org5)
                 + 0.10 * rk(volg) + 10 * high + 5 * above).clip(0, 100)
        score = score.iloc[25:]                 # 롤링 워밍업 제외

    # 미래 수익률
    events = []
    horizons = [5, 10, 20, 60]
    fwd = {h: close_a.shift(-h) / close_a - 1 for h in horizons}
    # KOSPI 벤치마크
    try:
        import yfinance as yf
        k = yf.download("^KS11", period="1y", interval="1d", progress=False, auto_adjust=True)
        if isinstance(k.columns, pd.MultiIndex):
            k.columns = k.columns.get_level_values(0)
        kclose = k["Close"].squeeze()
        kclose.index = pd.to_datetime(kclose.index).tz_localize(None)
        kclose = kclose.reindex(close_a.index).ffill()
        kfwd = {h: kclose.shift(-h) / kclose - 1 for h in horizons}
    except Exception:
        kfwd = {h: pd.Series(np.nan, index=close_a.index) for h in horizons}

    sig = score >= threshold
    for dt in sig.index:
        hit_codes = sig.columns[sig.loc[dt].fillna(False)]
        for c in hit_codes:
            ev = {"날짜": dt, "종목코드": c, "Score": round(float(score.loc[dt, c]), 1)}
            for h in horizons:
                v = fwd[h].loc[dt, c]
                ev[f"{h}일후"] = round(float(v) * 100, 2) if pd.notna(v) else np.nan
                kv = kfwd[h].loc[dt]
                kv = float(kv) if pd.notna(kv) else np.nan
                ev[f"{h}일초과"] = (round((float(v) - kv) * 100, 2)
                                  if pd.notna(v) and pd.notna(kv) else np.nan)
            # 20일 내 최대 낙폭
            try:
                pos = close_a.index.get_loc(dt)
                path = close_a[c].iloc[pos:pos + 21]
                ev["MDD20"] = round(float(path.min() / path.iloc[0] - 1) * 100, 2) if len(path) > 1 else np.nan
            except Exception:
                ev["MDD20"] = np.nan
            events.append(ev)

    edf = pd.DataFrame(events)
    if edf.empty:
        return edf, pd.DataFrame()

    # 파생시장 조건 필터 (신호일 기준)
    if fut_filter or prog_filter:
        try:
            import derivatives as _d
            edf["_d"] = pd.to_datetime(edf["날짜"]).dt.normalize()
            if fut_filter:
                fut = _d.load_futures_trend(130)
                ok = set(pd.to_datetime(fut.index[fut["외국인"] > 0]).normalize())
                edf = edf[edf["_d"].isin(ok)]
            if prog_filter and not edf.empty:
                prog = _d.load_program_trend(130)
                ok2 = set(pd.to_datetime(prog.index[prog["프로그램"] > 0]).normalize())
                edf = edf[edf["_d"].isin(ok2)]
            edf = edf.drop(columns=["_d"], errors="ignore")
        except Exception:
            pass
        if edf.empty:
            return edf, pd.DataFrame()

    stats = []
    for h in horizons:
        col = edf[f"{h}일후"].dropna()
        exc = edf[f"{h}일초과"].dropna()
        if len(col) == 0:
            continue
        stats.append({
            "기간": f"{h}일 후",
            "표본수": len(col),
            "승률(%)": round((col > 0).mean() * 100, 1),
            "평균수익률(%)": round(col.mean(), 2),
            "최대수익(%)": round(col.max(), 2),
            "최대손실(%)": round(col.min(), 2),
            "Sharpe": round(col.mean() / col.std() * np.sqrt(248 / h), 2) if col.std() > 0 else np.nan,
            "Hit Ratio(%)": round((exc > 0).mean() * 100, 1) if len(exc) else np.nan,
        })
    sdf = pd.DataFrame(stats)
    sdf["평균MDD20(%)"] = round(edf["MDD20"].dropna().mean(), 2)
    return edf, sdf

# ═══════════════════════════════════════════════════════
# Signal Engine · AI Decision · Accumulation Engine
# ═══════════════════════════════════════════════════════
SIG_GRADE_DEF = [
    (95, "S+", "★★★★★", "Strong Buy", "🟢"),
    (90, "S",  "★★★★★", "Buy", "🟢"),
    (80, "A",  "★★★★☆", "Buy Candidate", "🟡"),
    (70, "B",  "★★★☆☆", "Watch", "🟠"),
    (60, "C",  "★★☆☆☆", "Neutral", "⚪"),
    (-1, "Exit", "★☆☆☆☆", "Exit", "🔴"),
]

def _sig_grade(s: float):
    for thr, g, stars, label, icon in SIG_GRADE_DEF:
        if s >= thr:
            return g, stars, label, icon
    return "Exit", "★☆☆☆☆", "Exit", "🔴"

@st.cache_data(ttl=1800, show_spinner=False)
def daily_score_frame(codes: tuple):
    """일별 Signal Score 이력 (Signal History · 발생일 · 패턴 승률의 원천)"""
    fl = load_flows_history(codes)
    pr = load_price_history(codes)
    if fl.empty or pr.empty:
        return pd.DataFrame(), pd.DataFrame()
    frg, org = _pivot(fl, "외국인"), _pivot(fl, "기관")
    close = _pivot(pr, "종가").ffill()
    tv = _pivot(pr, "거래대금")
    common = sorted(set(frg.columns) & set(close.columns))
    frg, org = frg[common], org[common]
    close, tv = close[common].reindex(frg.index).ffill(), tv[common].reindex(frg.index)
    frg5, org5 = frg.rolling(5).sum(), org.rolling(5).sum()
    tv5 = tv.rolling(5).mean()
    tvg = (tv5 / tv5.shift(5) - 1) * 100
    vol5 = (tv / close).rolling(5).mean()
    volg = (vol5 / vol5.shift(5) - 1) * 100
    ma20 = close.rolling(20).mean()
    hi20 = close.rolling(20).max()
    rk = lambda d: d.rank(axis=1, pct=True) * 100
    score = (0.20 * rk(tvg) + 0.20 * rk(frg5) + 0.20 * rk(org5) + 0.05 * rk(volg)
             + 10 * (close >= hi20 * 0.999).astype(int)
             + 8 * (close > ma20).astype(int)
             + 12 * ((frg5 > 0) & (org5 > 0)).astype(int)).clip(0, 100)
    return score.iloc[25:], close

@st.cache_data(ttl=1800, show_spinner=False)
def bucket_stats(codes: tuple) -> dict:
    """Score 구간별 과거 성과 (승률·평균수익·MDD) → Confidence·목표가 산출용"""
    score, close = daily_score_frame(codes)
    out = {}
    if score.empty:
        return out
    fwd5 = (close.shift(-5) / close - 1) * 100
    fwd20 = (close.shift(-20) / close - 1) * 100
    mdd20 = pd.concat([(close.shift(-i) / close - 1) for i in range(1, 21)]).groupby(level=0).min() * 100
    for lo, hi, key in [(90, 101, "90+"), (80, 90, "80s"), (70, 80, "70s"), (0, 70, "low")]:
        mask = (score >= lo) & (score < hi)
        r5 = fwd5.reindex(score.index)[mask].stack().dropna()
        r20 = fwd20.reindex(score.index)[mask].stack().dropna()
        m20 = mdd20.reindex(score.index)[mask].stack().dropna()
        if len(r20) >= 10:
            out[key] = dict(
                n=len(r20),
                win5=round((r5 > 0).mean() * 100, 1) if len(r5) else 50.0,
                win20=round((r20 > 0).mean() * 100, 1),
                ret20=round(r20.mean(), 2),
                mdd20=round(m20.mean(), 2) if len(m20) else -5.0)
    return out

def _score_bucket(s: float) -> str:
    if s >= 90: return "90+"
    if s >= 80: return "80s"
    if s >= 70: return "70s"
    return "low"

@st.cache_data(ttl=900, show_spinner=False)
def extra_metrics(codes: tuple) -> pd.DataFrame:
    """누적 수급(5/10/20/60일) · 60일선 · 변동성 · 20일선 신규돌파"""
    fl = load_flows_history(codes)
    pr = load_price_history(codes)
    if fl.empty or pr.empty:
        return pd.DataFrame()
    frg, org = _pivot(fl, "외국인"), _pivot(fl, "기관")
    close = _pivot(pr, "종가").ffill()
    tv = _pivot(pr, "거래대금")
    common = sorted(set(frg.columns) & set(close.columns))
    frg, org, close, tv = frg[common], org[common], close[common], tv[common]
    ma20, ma60 = close.rolling(20).mean(), close.rolling(60).mean()
    tv20 = tv.rolling(20).mean()
    tvg20 = (tv20.iloc[-1] / tv20.iloc[-21] - 1) * 100 if len(tv20) >= 21 else pd.Series(0, index=common)
    ret = close.pct_change()
    out = pd.DataFrame({
        "외5": frg.tail(5).sum(), "외10": frg.tail(10).sum(),
        "외20": frg.tail(20).sum(), "외60": frg.tail(60).sum(),
        "기5": org.tail(5).sum(), "기10": org.tail(10).sum(),
        "기20": org.tail(20).sum(), "기60": org.tail(60).sum(),
        "거래대금증가율20": tvg20,
        "60일선위": close.iloc[-1] > ma60.iloc[-1],
        "신규돌파20": (close.iloc[-1] > ma20.iloc[-1]) & (close.iloc[-2] <= ma20.iloc[-2]),
        "변동성20": ret.tail(20).std() * 100,
    })
    out.index.name = "종목코드"
    return out.reset_index()

def build_signal_layer(df: pd.DataFrame, codes: tuple, p: int, ds: dict) -> pd.DataFrame:
    """Signal Score · 등급 · Stage · Decision · Confidence · 목표가 · 매집 지표"""
    ex = extra_metrics(codes)
    df = df.merge(ex, on="종목코드", how="left")
    for c in ["외5", "외10", "외20", "외60", "기5", "기10", "기20", "기60",
              "거래대금증가율20", "변동성20"]:
        df[c] = df[c].fillna(0)
    df["60일선위"] = df["60일선위"].fillna(False).astype(bool)
    df["신규돌파20"] = df["신규돌파20"].fillna(False).astype(bool)

    r = lambda s: s.rank(pct=True)
    stock_bull = (df["외국인순매수"] + df["기관순매수"]) > 0
    fut_ok = float(ds.get("fut_pos", False))
    prog_ok = float(ds.get("prog_pos", False))

    # 섹터 모멘텀 (섹터별 외+기 순매수 percentile)
    sec_flow = df.groupby("섹터")["외국인순매수"].transform("sum") \
             + df.groupby("섹터")["기관순매수"].transform("sum")
    sector_mom = r(sec_flow)

    # AI 패턴 유사도 (과거 동일 Score 구간 승률)
    bstats = bucket_stats(codes)
    def pattern_win(s):
        b = bstats.get(_score_bucket(s))
        return (b["win20"] / 100) if b else 0.5
    base_score = df["Score"]  # 기존 Radar Score를 1차 근사로 사용
    pat = base_score.map(pattern_win)

    # ── Signal Score (100점, 옵션 제외 재배분) ──
    # 거래대금증가 20 · 외국인 20 · 기관 20 · 외국인선물 12 · 프로그램 8
    # 거래량증가 5 · 20일선 돌파 5 · 60일선 위 3 · 신고가 3 · 섹터모멘텀 2 · AI패턴 2
    df["Signal Score"] = (
        20 * r(df["거래대금증가율"].fillna(0))
        + 20 * r(df["외국인순매수"]) + 20 * r(df["기관순매수"])
        + 12 * (stock_bull & bool(fut_ok)).astype(float)
        + 8 * (stock_bull & bool(prog_ok)).astype(float)
        + 5 * r(df["거래량증가율"].fillna(0))
        + 5 * (df["20일선위"].astype(float) * 0.6 + df["신규돌파20"].astype(float) * 0.4)
        + 3 * df["60일선위"].astype(float)
        + 3 * df["20일신고가"].astype(float)
        + 2 * sector_mom + 2 * pat
    ).clip(0, 100).round(1)

    g = df["Signal Score"].map(_sig_grade)
    df["Sig등급"] = g.map(lambda x: x[0])
    df["Sig별점"] = g.map(lambda x: x[1])
    df["Sig라벨"] = g.map(lambda x: f"{x[3]} {x[2]}")

    # ── Stage 분류 (자금 유입 6단계) ──
    dist = (df["외20"] < 0) & (df["기20"] < 0) & (df["거래대금증가율20"] < 0)
    stage4 = df["20일선위"] & df["60일선위"] & (df["20일신고가"] | df["신규돌파20"]) \
             & (df["거래량증가율"].fillna(0) > 0) & stock_bull
    stage5 = df["20일선위"] & df["60일선위"] & (df["외국인연속"] >= 3) & (df["상승률20일"] > 5)
    stage3 = (df["외국인순매수"] > 0) & (df["기관순매수"] > 0) & (df["거래대금증가율"].fillna(0) > 0)
    stage2 = (df["외국인순매수"] > 0) | (df["기관순매수"] > 0)
    df["Stage"] = np.select(
        [dist, stage4, stage5, stage3, stage2, df["거래대금증가율"].fillna(0) > 0],
        ["6️⃣🔴 분배", "4️⃣🟢 돌파", "5️⃣🔵 추세유지", "3️⃣🟠 매집", "2️⃣🟡 초기유입", "1️⃣⚪ 관심"],
        default="1️⃣⚪ 관심")

    # ── 매집 지표 ──
    df["Accum Score"] = (
        30 * r(df["외20"]) + 30 * r(df["기20"]) + 15 * r(df["거래대금증가율20"])
        + 10 * (1 - r(df["상승률20일"])) + 10 * sector_mom + 5 * pat
    ).clip(0, 100).round(1)
    df["HiddenGem"] = ((df["외20"] > 0) & (df["기20"] > 0) & (df["거래대금증가율20"] > 0)
                       & (~df["20일신고가"]) & (df["상승률20일"] <= 10))
    low_vol = df["변동성20"] <= df["변동성20"].median()
    df["Silent"] = ((df["상승률20일"].abs() <= 5) & low_vol & (df["거래대금증가율20"] > 0)
                    & (df["외국인연속"] >= 5) & (df["기관연속"] >= 3))
    df["Distribution"] = dist
    df["매집표시"] = np.select(
        [df["Distribution"], df["Silent"], df["HiddenGem"]],
        ["⚠ Distribution", "🔷 Silent Accum", "💎 Hidden Gem"], default="")

    # ── AI Confidence ──
    deriv_confirm = (stock_bull & bool(fut_ok)).astype(float)
    win_arr = base_score.map(lambda s: bstats.get(_score_bucket(s), {}).get("win20", 50.0))
    df["Confidence"] = (0.55 * df["Signal Score"] + 15 * deriv_confirm
                        + 0.30 * win_arr).clip(1, 99).round(0).astype(int)

    # ── AI Target Price / Risk-Reward (과거 유사 신호 성과 기반 참고치) ──
    exp20 = base_score.map(lambda s: bstats.get(_score_bucket(s), {}).get("ret20", 5.0))
    mdd20 = base_score.map(lambda s: bstats.get(_score_bucket(s), {}).get("mdd20", -5.0))
    exp20 = exp20.clip(lower=1.0)
    mdd20 = mdd20.clip(upper=-1.0)
    df["AI목표가"] = (df["현재가"] * (1 + exp20 / 100)).round(0)
    df["손절라인"] = (df["현재가"] * (1 + mdd20 / 100)).round(0)
    df["예상수익률"] = exp20.round(1)
    df["RiskReward"] = (exp20 / mdd20.abs()).round(2)

    # ── AI Decision (행동 제안) ──
    take_profit = (df["상승률20일"] > 15) & (df["외5"] < 0)
    df["AI Decision"] = np.select(
        [df["Distribution"] | (df["Sig등급"] == "Exit"),
         take_profit,
         (df["Sig등급"].isin(["S+", "S"])) & (df["Stage"].str.startswith(("4", "5"))),
         df["Sig등급"].isin(["S+", "S", "A"]) & stock_bull,
         df["Sig등급"] == "B"],
        ["🔴 전량 매도 검토", "🟠 일부 차익실현", "🟢 적극 매수", "🟢 분할 매수", "🟡 관망"],
        default="🟡 관망")

    # ── 신호 발생일 · 유지일 ──
    try:
        sc_hist, _ = daily_score_frame(codes)
        cur_bucket = df.set_index("종목코드")["Signal Score"].map(_score_bucket)
        starts, holds = {}, {}
        for c in sc_hist.columns:
            if c not in cur_bucket.index:
                continue
            series = sc_hist[c].dropna()
            if series.empty:
                continue
            b = series.map(_score_bucket)
            tgt = cur_bucket[c]
            cnt = 0
            for v in b.values[::-1]:
                if v == tgt:
                    cnt += 1
                else:
                    break
            holds[c] = cnt
            starts[c] = series.index[-cnt].strftime("%m/%d") if cnt > 0 else "-"
        df["발생일"] = df["종목코드"].map(starts).fillna("-")
        df["유지일"] = df["종목코드"].map(holds).fillna(0).astype(int)
    except Exception:
        df["발생일"], df["유지일"] = "-", 0
    return df

def consensus_score(bull_val: float, df: pd.DataFrame):
    """Smart Money Consensus (0~100) — Bull Score + 시장 폭(breadth) + 매집 폭"""
    stock_bull = ((df["외국인순매수"] + df["기관순매수"]) > 0).mean() * 100
    accum_b = (df["Accum Score"] >= 70).mean() * 100 if "Accum Score" in df.columns else 50
    score = float(np.clip(0.70 * bull_val + 0.15 * stock_bull + 0.15 * accum_b, 0, 100))
    if score >= 90:   lab = "🟢 Strong Bull"
    elif score >= 70: lab = "🟢 Bull"
    elif score >= 50: lab = "🟡 Neutral"
    elif score >= 30: lab = "🟠 Bear"
    else:             lab = "🔴 Strong Bear"
    return round(score, 1), lab

@st.cache_data(ttl=600, show_spinner=False)
def signal_summary(p: int = 5) -> dict:
    """Dashboard 최상단 Ultimate 카드용 요약 (캐시 공유)"""
    base = load_all_stocks()
    codes = tuple(base.nlargest(RADAR_N, "거래대금")["종목코드"].tolist())
    m = build_metrics(codes, p)
    if m.empty:
        return {}
    ds = derivatives.get_deriv_state(p=5) if DERIV_OK else {"ok": False}
    base_cols = ["종목코드", "종목명", "현재가", "등락률", "거래대금", "시가총액", "섹터"]
    if "테마" in base.columns:
        base_cols.append("테마")
    df = m.merge(base[base_cols], on="종목코드", how="left")
    df = build_signal_layer(df, codes, p, ds)
    try:
        mt60 = load_market_trend(60)
        iv60 = load_index_value(60)
        bull_val = derivatives.bull_score(mt60, iv60, 5)[0] if DERIV_OK else 50.0
    except Exception:
        bull_val = 50.0
    cons, cons_lab = consensus_score(bull_val, df)
    best_sec = (df.groupby("섹터")
                .apply(lambda g: g["외국인순매수"].sum() + g["기관순매수"].sum())
                .idxmax())
    return dict(
        consensus=cons, consensus_label=cons_lab, bull=bull_val,
        strong_buy=int(df["Sig등급"].isin(["S+", "S"]).sum()),
        buy=int((df["Sig등급"] == "A").sum()),
        early=int(df["EarlyEntry"].sum()),
        exit=int((df["Sig등급"] == "Exit").sum()),
        hidden=int(df["HiddenGem"].sum()),
        silent=int(df["Silent"].sum()),
        dist=int(df["Distribution"].sum()),
        best_sector=best_sec,
        market_trend="상승 우위" if bull_val >= 60 else ("하락 우위" if bull_val < 40 else "중립"),
    )

# ═══════════════════════════════════════════════════════
# 메인 렌더링
# ═══════════════════════════════════════════════════════
def render():
    st.markdown(DARK_CSS, unsafe_allow_html=True)
    st.markdown('<h2 class="sm-title">🎯 SMART MONEY RADAR</h2>', unsafe_allow_html=True)
    st.caption("기관·외국인 자금 흐름 매매 전략 플랫폼 | 거래대금 상위 250종목 · 수급 근사치(수량×종가)")

    if not PLOTLY_OK:
        st.error("plotly 패키지가 없습니다.")
        return

    c1, c2, c3 = st.columns([3, 2, 2])
    with c1:
        p = st.radio("수급 기준기간(거래일)", [1, 5, 20], index=1, horizontal=True, key="rd_p")
    with c2:
        refresh_r = st.selectbox("자동 새로고침", ["끄기", "5분", "10분", "30분"], key="rd_auto")
    with c3:
        if st.button("🔄 데이터 갱신", key="rd_refresh", use_container_width=True):
            st.cache_data.clear()
            st.rerun()
    if refresh_r != "끄기":
        _sec = {"5분": 300, "10분": 600, "30분": 1800}[refresh_r]
        st.markdown(f'<meta http-equiv="refresh" content="{_sec}">', unsafe_allow_html=True)
    st.caption("첫 로딩 1~2분(이후 캐시) · 시세는 갱신 주기별, 종목 수급은 일별 확정치 기준")

    try:
        with st.spinner("전 종목 시세 로딩..."):
            base = load_all_stocks()
        if base.empty:
            st.error("시세 로딩 실패 — 잠시 후 재시도하세요.")
            return
        codes = tuple(base.nlargest(RADAR_N, "거래대금")["종목코드"].tolist())
        with st.spinner(f"수급·가격 이력 분석 중... (상위 {RADAR_N}종목 × {HIST_DAYS}일)"):
            m = build_metrics(codes, p)
    except Exception as e:
        st.error(f"데이터 로딩 실패: {e}")
        return
    if m.empty:
        st.error("지표 계산에 필요한 데이터가 부족합니다.")
        return

    base_cols = ["종목코드", "종목명", "현재가", "등락률", "거래대금", "시가총액", "섹터"]
    if "테마" in base.columns:
        base_cols.append("테마")
    df = m.merge(base[base_cols], on="종목코드", how="left")
    if "테마" not in df.columns:
        df["테마"] = ""

    # ── 파생시장 통합: Confirmation · Score 보정 · 등급 게이트 ──
    ds = derivatives.get_deriv_state(p=min(p, 20) if p > 1 else 5) if DERIV_OK else {"ok": False}
    stock_bull = (df["외국인순매수"] + df["기관순매수"]) > 0
    if ds.get("ok"):
        df["파생확인"] = [derivatives.confirmation_label(b, ds["fut_pos"]) for b in stock_bull]
        # Score: 현물 85% + 파생 확인 보너스 15% (외국인 선물 일치 10 + 프로그램 일치 5)
        bonus = (stock_bull & ds["fut_pos"]).astype(int) * 10 \
              + (stock_bull & ds["prog_pos"]).astype(int) * 5
        df["Score"] = (0.85 * df["Score"] + bonus).clip(0, 100).round(1)
        df["Score등급"] = pd.cut(df["Score"], [-1, 60, 70, 80, 90, 101],
                               labels=["Weak", "중립", "Watch", "Buy", "Strong Buy"])
        # S등급 게이트: 파생시장(외국인 선물 + 프로그램) 우호가 아니면 S → A
        if not ds["deriv_pos"]:
            df.loc[df["등급"] == "S", "등급"] = "A"
            df["별점"] = df["등급"].map(GRADE_STARS)
            df["판정"] = df["등급"].map(GRADE_LABEL)
        # Exit 신호에 파생 확인 표시
        if ds["deriv_neg"]:
            df.loc[df["ExitSignal"], "신호"] = "🔴✔ Exit(파생확인)"
    else:
        df["파생확인"] = "—"

    # ── Signal Engine · AI Decision · Accumulation 레이어 ──
    with st.spinner("AI Signal Engine 계산 중..."):
        df = build_signal_layer(df, codes, p, ds)

    # 🔔 신규 Strong Buy 알림 (5분 갱신 시 이전 상태와 비교)
    cur_strong = set(df.loc[df["Sig등급"].isin(["S+", "S"]), "종목명"])
    prev_strong = st.session_state.get("prev_strong_set")
    new_signals = sorted(cur_strong - prev_strong) if prev_strong is not None else []
    st.session_state["prev_strong_set"] = cur_strong
    if new_signals:
        rows_ns = df[df["종목명"].isin(new_signals)].nlargest(3, "Signal Score")
        msg = " | ".join(f"**{r_['종목명']}** {r_['Sig별점']} Signal {r_['Signal Score']:.0f}점"
                         for _, r_ in rows_ns.iterrows())
        st.warning(f"🔔 NEW SIGNAL ({datetime.now():%H:%M})  {msg}")

    # ── Bull Score 게이지 (현물+선물+프로그램+거래대금) + KPI ──
    gcol, kcol = st.columns([2, 6])
    with gcol:
        try:
            mt60 = load_market_trend(60)
            iv60 = load_index_value(60)
            if DERIV_OK:
                bs_val, bs_grade, bs_stars, _ = derivatives.bull_score(mt60, iv60, p=5)
            else:
                cur = (mt60["외국인"] + mt60["기관"]).rolling(5).sum()
                bs_val = float((cur.dropna() < cur.iloc[-1]).mean() * 100)
                bs_grade, bs_stars = "—", ""
        except Exception:
            bs_val, bs_grade, bs_stars = 50.0, "—", ""
        fig_g = go.Figure(go.Indicator(
            mode="gauge+number", value=round(bs_val),
            title={"text": f"Bull Score — {bs_grade} {bs_stars}<br>"
                           "<sub>현물+선물+프로그램+거래대금 종합</sub>",
                   "font": {"size": 13}},
            gauge={"axis": {"range": [0, 100]},
                   "bar": {"color": "#f0a500"},
                   "steps": [{"range": [0, 20], "color": "#4a1520"},
                             {"range": [20, 40], "color": "#3a1f24"},
                             {"range": [40, 60], "color": "#1a1f2e"},
                             {"range": [60, 80], "color": "#12351f"},
                             {"range": [80, 100], "color": "#0e4429"}]}))
        fig_g.update_layout(template=PLOTLY_TMPL, height=230, margin=dict(t=60, b=10))
        st.plotly_chart(fig_g, use_container_width=True)
        if ds.get("ok"):
            st.caption(f"선물(외인) {ds['fut_frg']:+,.0f}억 · 프로그램 {ds['prog']:+,.0f}억 "
                       f"({min(p, 20) if p > 1 else 5}일 합)")
    with kcol:
        gc = df["등급"].value_counts()
        k = st.columns(4)
        k[0].metric("🌟 S등급 (Strong Buy)", f"{gc.get('S', 0)}개")
        k[1].metric("⭐ A등급 (Buy)", f"{gc.get('A', 0)}개")
        k[2].metric("👀 B등급 (Watch)", f"{gc.get('B', 0)}개")
        k[3].metric("🚪 D등급 (Exit)", f"{gc.get('D', 0)}개")
        k2 = st.columns(4)
        k2[0].metric("🟢 Early Entry", f"{int(df['EarlyEntry'].sum())}개")
        k2[1].metric("🔥 Smart Accumulation", f"{int(df['SmartAccum'].sum())}개")
        k2[2].metric("🔴 Exit Signal", f"{int(df['ExitSignal'].sum())}개")
        k2[3].metric("Score 90+", f"{int((df['Score'] >= 90).sum())}개")

    # ── 관심종목 알림 (14) ──
    favs = st.session_state.get("sm_favs", [])
    if favs:
        fav_df = df[df["종목명"].isin(favs)]
        alerts = []
        for _, r_ in fav_df.iterrows():
            tags = []
            if r_["등급"] == "S": tags.append("Strong Buy")
            if r_["EarlyEntry"]: tags.append("🟢 Early Entry")
            if r_["ExitSignal"]: tags.append("🔴 Exit Signal")
            if tags:
                alerts.append(f"**{r_['종목명']}** → {' · '.join(tags)}")
        if alerts:
            st.warning("🔔 관심종목 알림: " + "  |  ".join(alerts))

    # ── AI 시장 분석 (11) ──
    top90 = df[df["Score"] >= 90].nlargest(3, "Score")["종목명"].tolist()
    sec_mom = df.groupby("섹터").agg(순매수=("외국인순매수", "sum"), 기관=("기관순매수", "sum"))
    sec_mom["합"] = sec_mom["순매수"] + sec_mom["기관"]
    best_secs = sec_mom.nlargest(2, "합").index.tolist()
    worst_sec = sec_mom.nsmallest(1, "합").index[0]
    ai = (f"이번 기간({p}거래일) **{' · '.join(best_secs)}** 섹터에 외국인·기관 동반 순매수가 집중되었습니다. ")
    if top90:
        ai += f"Smart Money Score 90점 이상 종목은 **{', '.join(top90)}** 입니다. "
    ai += f"반면 **{worst_sec}** 섹터는 자금 이탈이 나타나 주의가 필요합니다."
    if ds.get("ok"):
        if ds["deriv_pos"]:
            ai += (" 파생시장에서도 **외국인 선물 순매수 + 프로그램 매수**가 동반되어 "
                   "매수 신호의 신뢰도가 높습니다.")
        elif ds["deriv_neg"]:
            ai += (" 파생시장은 **외국인 선물 순매도 + 프로그램 매도**로 약세 확인 중 — "
                   "신규 진입보다 리스크 관리가 우선입니다.")
        else:
            ai += " 파생시장은 혼조 — 개별 종목의 파생확인(✔) 신호를 함께 확인하세요."
    st.markdown(f'<div class="sm-ai">🤖 <b>AI 전략 브리핑</b><br>{ai}</div>', unsafe_allow_html=True)
    st.markdown("")

    # ── 내부 탭 ──
    t_ai, t_accum, t_radar, t_sig, t_rot, t_tl, t_bt, t_sim = st.tabs(
        ["🤖 AI Decision", "💎 매집 분석", "🎯 Radar 등급", "📡 매매 신호",
         "🔄 Sector Rotation", "🎬 Timeline", "🧪 백테스트", "🛠️ 전략 시뮬레이터"])

    disp_cols = ["종목명", "별점", "판정", "Score", "Score등급", "신호", "파생확인",
                 "현재가", "등락률", "거래대금", "거래대금증가율", "외국인순매수", "기관순매수",
                 "외국인비율", "기관비율", "외국인연속", "기관연속",
                 "20일선위", "20일신고가", "섹터", "테마"]

    # ═══ 🤖 AI Decision ═══
    with t_ai:
        # Today's Signal 카운트
        ts = st.columns(4)
        ts[0].metric("🟢 Strong Buy / Buy", f"{int(df['Sig등급'].isin(['S+','S']).sum())} / "
                                            f"{int((df['Sig등급']=='A').sum())}")
        ts[1].metric("🔵 Early Entry", f"{int(df['EarlyEntry'].sum())}개")
        ts[2].metric("🟠 Watch", f"{int((df['Sig등급']=='B').sum())}개")
        ts[3].metric("🔴 Exit", f"{int((df['Sig등급']=='Exit').sum())}개")

        st.subheader("🏆 Smart Ranking TOP30 — AI 추천 순위")
        st.caption("정렬: Signal Score 40% + Confidence 30% + Accumulation 30% · "
                   "목표가/손절은 과거 유사 신호의 20일 성과 기반 참고치 (확정 아님)")
        rank_score = (0.4 * df["Signal Score"] + 0.3 * df["Confidence"]
                      + 0.3 * df["Accum Score"])
        top30 = df.assign(종합=rank_score).sort_values("종합", ascending=False).head(30)
        cols_ai = ["종목명", "Sig별점", "Sig라벨", "Signal Score", "Confidence",
                   "AI Decision", "Stage", "파생확인", "발생일", "유지일",
                   "현재가", "AI목표가", "예상수익률", "손절라인", "RiskReward",
                   "외국인순매수", "기관순매수", "섹터", "테마"]
        d_ai = eokify(top30[cols_ai]).reset_index(drop=True)
        d_ai.index += 1
        st.dataframe(d_ai, use_container_width=True, height=560, column_config={
            **style_table(top30[cols_ai]),
            "Signal Score": st.column_config.ProgressColumn("Signal Score", min_value=0, max_value=100, format="%.0f"),
            "Confidence": st.column_config.ProgressColumn("Confidence(%)", min_value=0, max_value=100, format="%d"),
            "현재가": st.column_config.NumberColumn("현재가", format="%d원"),
            "AI목표가": st.column_config.NumberColumn("AI목표가", format="%d원"),
            "손절라인": st.column_config.NumberColumn("손절라인", format="%d원"),
            "예상수익률": st.column_config.NumberColumn("예상수익률(%)", format="+%.1f%%"),
        })

        # Stage 분포 + Sector Signal
        cst1, cst2 = st.columns(2)
        with cst1:
            st.subheader("📶 자금 유입 Stage 분포")
            stage_cnt = df["Stage"].value_counts().sort_index()
            fig_st = px.bar(x=stage_cnt.index, y=stage_cnt.values, template=PLOTLY_TMPL,
                            height=340, labels={"x": "Stage", "y": "종목 수"},
                            color=stage_cnt.values, color_continuous_scale="Viridis")
            st.plotly_chart(fig_st, use_container_width=True)
        with cst2:
            st.subheader("🏭 Sector Signal TOP10")
            ss = df.groupby("섹터").agg(
                StrongBuy=("Sig등급", lambda s: s.isin(["S+", "S"]).sum()),
                Buy=("Sig등급", lambda s: (s == "A").sum()),
                Watch=("Sig등급", lambda s: (s == "B").sum()),
                Exit=("Sig등급", lambda s: (s == "Exit").sum()),
                평균SignalScore=("Signal Score", "mean"),
            ).sort_values("평균SignalScore", ascending=False).head(10).round(1)
            st.dataframe(ss, use_container_width=True, height=340)

        # Signal History (재계산 기반)
        st.subheader("🗓️ Signal History")
        hist_win = st.radio("조회 기간(거래일)", [30, 60, 90], horizontal=True, key="hist_win")
        try:
            sc_hist, _ = daily_score_frame(codes)
            hh = sc_hist.tail(hist_win)
            hist_df = pd.DataFrame({
                "Strong Buy(90+)": (hh >= 90).sum(axis=1),
                "Buy(80~89)": ((hh >= 80) & (hh < 90)).sum(axis=1),
                "Watch(70~79)": ((hh >= 70) & (hh < 80)).sum(axis=1),
            })
            fig_hi = px.bar(hist_df, template=PLOTLY_TMPL, height=320,
                            color_discrete_sequence=["#00c853", "#f0a500", "#ff9800"],
                            labels={"value": "종목 수", "index": "날짜"})
            st.plotly_chart(fig_hi, use_container_width=True)
            pick_h = st.selectbox("종목별 이력 조회", sorted(df["종목명"]), key="hist_pick",
                                  index=None, placeholder="종목명 입력")
            if pick_h:
                code_h = df.loc[df["종목명"] == pick_h, "종목코드"].iloc[0]
                if code_h in sc_hist.columns:
                    fig_hs = px.line(sc_hist[code_h].tail(hist_win), template=PLOTLY_TMPL,
                                     height=280, labels={"value": "Signal Score"})
                    fig_hs.add_hline(y=90, line_color="#00c853", line_dash="dot")
                    fig_hs.add_hline(y=60, line_color="#ff5252", line_dash="dot")
                    st.plotly_chart(fig_hs, use_container_width=True)
        except Exception as e:
            st.caption(f"이력 계산 실패: {e}")
        st.caption("※ 종목별 수급은 일별 확정치 기준 — 신호는 확률 기반 참고용이며 투자 판단·책임은 투자자 본인에게 있습니다.")

    # ═══ 💎 매집 분석 (Smart Accumulation Engine) ═══
    with t_accum:
        ac = st.columns(5)
        ac[0].metric("💎 Hidden Gem", f"{int(df['HiddenGem'].sum())}개")
        ac[1].metric("🔷 Silent Accum", f"{int(df['Silent'].sum())}개")
        ac[2].metric("🟢 Strong Buy", f"{int(df['Sig등급'].isin(['S+','S']).sum())}개")
        ac[3].metric("⚠ Distribution", f"{int(df['Distribution'].sum())}개")
        best_acc_sec = df.groupby("섹터")["외20"].sum().add(
            df.groupby("섹터")["기20"].sum()).idxmax()
        ac[4].metric("최다 매집 섹터(20일)", best_acc_sec)

        # AI 매집 코멘트
        top_acc = df.nlargest(1, "Accum Score").iloc[0]
        acc_ai = (f"**{top_acc['종목명']}**은(는) 최근 20거래일 외국인 {fmt_eok(top_acc['외20'])}, "
                  f"기관 {fmt_eok(top_acc['기20'])} 누적 순매수 중입니다 "
                  f"(Accumulation Score {top_acc['Accum Score']:.0f}점). ")
        if not top_acc["20일신고가"]:
            acc_ai += "거래대금이 늘고 있으나 주가는 아직 신고가가 아니어서 장기 매집 가능성이 있습니다."
        else:
            acc_ai += "이미 신고가 영역이므로 추격보다 눌림 구간 분할 접근이 유효합니다."
        st.markdown(f'<div class="sm-ai">🤖 <b>AI Accumulation Comment</b><br>{acc_ai}</div>',
                    unsafe_allow_html=True)
        st.markdown("")

        st.subheader("🐘 Big Player Tracking — 20일 누적 매집 TOP50")
        big = df.assign(합산20=df["외20"] + df["기20"]).sort_values("합산20", ascending=False).head(50)
        cols_bp = ["종목명", "매집표시", "외20", "기20", "합산20", "거래대금",
                   "Accum Score", "Signal Score", "Confidence", "상승률20일", "섹터", "테마"]
        d_bp = eokify(big[cols_bp].rename(columns={"외20": "외국인누적", "기20": "기관누적",
                                                   "합산20": "합산누적"})).reset_index(drop=True)
        d_bp.index += 1
        st.dataframe(d_bp, use_container_width=True, height=480,
                     column_config=style_table(d_bp))

        cq1, cq2 = st.columns(2)
        with cq1:
            st.subheader("💎 Hidden Gem — 숨은 강세 후보")
            hg = df[df["HiddenGem"]].sort_values("Accum Score", ascending=False)
            cols_hg = ["종목명", "Accum Score", "외20", "기20", "상승률20일",
                       "거래대금증가율20", "Confidence", "섹터"]
            st.dataframe(eokify(hg[cols_hg]).reset_index(drop=True),
                         use_container_width=True, height=300)
            st.subheader("🔷 Silent Accumulation — 조용한 매집")
            sil = df[df["Silent"]].sort_values("Accum Score", ascending=False)
            st.dataframe(eokify(sil[cols_hg]).reset_index(drop=True),
                         use_container_width=True, height=250)
        with cq2:
            st.subheader("⚠ Distribution — 세력 이탈 경고")
            dd = df[df["Distribution"]].sort_values("외20")
            cols_dd = ["종목명", "외20", "기20", "거래대금증가율20", "상승률20일",
                       "Sig등급", "AI Decision", "섹터"]
            st.dataframe(eokify(dd[cols_dd]).reset_index(drop=True),
                         use_container_width=True, height=300)
            st.subheader("🏭 Sector Accumulation TOP10 (20일)")
            sa = df.groupby("섹터").agg(외국인누적=("외20", "sum"), 기관누적=("기20", "sum"),
                                       평균AccumScore=("Accum Score", "mean")).round(1)
            sa["합산"] = sa["외국인누적"] + sa["기관누적"]
            sa = sa.sort_values("합산", ascending=False).head(10)
            st.dataframe(eokify(sa.reset_index()), use_container_width=True, height=300,
                         column_config=style_table(sa.reset_index()))

        # Money Trail
        st.subheader("💸 Money Trail — 자금 이동 추적")
        try:
            s20 = df.groupby("섹터").apply(lambda g: (g["외20"] + g["기20"]).sum()).rank(pct=True)
            s5 = df.groupby("섹터").apply(lambda g: (g["외5"] + g["기5"]).sum()).rank(pct=True)
            accel = (s5 - s20).sort_values(ascending=False)
            past_top = s20.sort_values(ascending=False).head(2).index.tolist()
            now_top = s5.sort_values(ascending=False).head(2).index.tolist()
            next_top = [s for s in accel.index if s not in now_top][:3]
            st.markdown(
                '<style>@keyframes flowp {0%{opacity:.3}50%{opacity:1}100%{opacity:.3}} '
                '.flow-a{animation:flowp 1.2s infinite;color:#f0a500;font-size:1.2em}</style>'
                f'<div class="sm-ai">과거 20일: <b>{" · ".join(past_top)}</b> '
                f'<span class="flow-a"> ⟶ </span> 최근 5일: '
                f'<b style="color:#00e676">{" · ".join(now_top)}</b> '
                f'<span class="flow-a"> ⟶ </span> 다음 예상(자금 가속): '
                f'<b style="color:#00b0ff">{" · ".join(next_top)}</b>'
                f'<br><sub>※ AI 참고치 — 5일 대비 20일 순매수 순위 가속도 기준</sub></div>',
                unsafe_allow_html=True)
        except Exception:
            pass

    # ═══ 🎯 Radar 등급 ═══
    with t_radar:
        g1, g2, g3 = st.columns([2, 2, 2])
        pick_g = g1.multiselect("등급 필터", ["S", "A", "B", "C", "D"],
                                default=["S", "A", "B"], key="rd_grade")
        pick_s = g2.multiselect("대표 섹터", sorted(df["섹터"].dropna().unique()), key="rd_sec")
        q = g3.text_input("종목·섹터·테마 검색", key="rd_q", placeholder="예: 삼성, HBM, 방산")
        view = df[df["등급"].isin(pick_g)] if pick_g else df
        if pick_s:
            view = view[view["섹터"].isin(pick_s)]
        if q:
            q_ = q.strip()
            view = view[view["종목명"].str.contains(q_, na=False)
                        | view["섹터"].str.contains(q_, na=False)
                        | view["테마"].str.contains(q_, na=False)]
        view = view.sort_values(["등급", "Score"], ascending=[True, False])
        d = eokify(view[disp_cols]).reset_index(drop=True)
        d.index += 1
        st.dataframe(d, use_container_width=True, height=560,
                     column_config=style_table(view[disp_cols]))
        st.caption("등급 기준 — S: 외인+기관 순매수·거래대금 증가 상위20%·5일 거래대금 증가·20일선 위·20일 신고가 / "
                   "A: 외인+기관 순매수·거래대금 증가·20일선 위 / B: 외인 또는 기관 순매수·거래대금 증가 / "
                   "D: 외인·기관 순매도·거래대금 감소 / C: 그 외")

    # ═══ 📡 매매 신호 ═══
    with t_sig:
        s1, s2 = st.columns(2)
        with s1:
            st.subheader("🟢 Early Entry — 선취매 후보")
            st.caption("외인·기관 순매수 + 거래대금 증가 + 20일 상승률 5% 이하 + 20일선 돌파 직전/직후")
            ee = df[df["EarlyEntry"]].sort_values("Score", ascending=False)
            cols_e = ["종목명", "Score", "별점", "현재가", "상승률20일", "외국인순매수",
                      "기관순매수", "거래대금증가율", "섹터"]
            st.dataframe(eokify(ee[cols_e]).reset_index(drop=True), use_container_width=True,
                         column_config=style_table(ee[cols_e]))

            st.subheader("🔥 Smart Accumulation — 동반 연속 매집")
            st.caption("외국인·기관 모두 3일 이상 연속 순매수")
            sa = df[df["SmartAccum"]].sort_values(["외국인연속", "기관연속"], ascending=False)
            cols_a = ["종목명", "외국인연속", "기관연속", "Score", "외국인순매수", "기관순매수",
                      "외국인비율", "기관비율", "섹터"]
            st.dataframe(eokify(sa[cols_a]).reset_index(drop=True), use_container_width=True,
                         column_config=style_table(sa[cols_a]))
        with s2:
            st.subheader("🔴 Exit Signal — 매도 경고")
            st.caption("외인·기관 순매도 + 거래대금 감소(5일 연속) + 20일선 이탈")
            ex_ = df[df["ExitSignal"]].sort_values("Score")
            cols_x = ["종목명", "Score", "현재가", "등락률", "외국인순매수", "기관순매수",
                      "거래대금증가율", "섹터"]
            st.dataframe(eokify(ex_[cols_x]).reset_index(drop=True), use_container_width=True,
                         column_config=style_table(ex_[cols_x]))

            st.subheader("📆 연속 순매수 현황")
            streak_cols = ["종목명", "외국인연속", "기관연속",
                           "외3일연속", "외5일연속", "외10일연속", "외20일연속",
                           "기3일연속", "기5일연속", "기10일연속", "기20일연속"]
            sk = df.sort_values(["외국인연속", "기관연속"], ascending=False).head(50)
            st.dataframe(sk[streak_cols].reset_index(drop=True), use_container_width=True)

    # ═══ 🔄 Sector Rotation ═══
    with t_rot:
        st.subheader("🔄 Sector Rotation — 자금 이동")
        win = st.radio("비교 기준(거래일)", [5, 20, 60], horizontal=True, key="rot_win")
        try:
            fl = load_flows_history(codes)
            pr = load_price_history(codes)
            frg_pv, org_pv = _pivot(fl, "외국인"), _pivot(fl, "기관")
            tv_pv, close_pv = _pivot(pr, "거래대금"), _pivot(pr, "종가")
            code2sec = dict(zip(base["종목코드"], base["섹터"]))

            rows = []
            for sec_name in sorted(base["섹터"].dropna().unique()):
                cs = [c for c in frg_pv.columns if code2sec.get(c) == sec_name]
                if not cs:
                    continue
                chg = (close_pv[cs].ffill().iloc[-1] / close_pv[cs].ffill().iloc[-min(win + 1, len(close_pv))] - 1) * 100
                tv_c = tv_pv[cs].tail(win).sum().sum()
                tv_p_ = tv_pv[cs].iloc[-2 * win:-win].sum().sum() if len(tv_pv) >= 2 * win else np.nan
                rows.append({
                    "섹터": sec_name,
                    "거래대금": tv_c,
                    "거래대금증감률": (tv_c / tv_p_ - 1) * 100 if tv_p_ and tv_p_ > 0 else 0,
                    "외국인순매수": frg_pv[cs].tail(win).sum().sum(),
                    "기관순매수": org_pv[cs].tail(win).sum().sum(),
                    "평균등락률": chg.mean(),
                    "상승종목비율": (chg > 0).mean() * 100,
                })
            rot = pd.DataFrame(rows).set_index("섹터")
            rr = lambda s: s.rank(pct=True) * 100
            rot["Momentum Score"] = (0.25 * rr(rot["거래대금증감률"]) + 0.25 * rr(rot["외국인순매수"])
                                     + 0.25 * rr(rot["기관순매수"]) + 0.15 * rr(rot["상승종목비율"])
                                     + 0.10 * rr(rot["평균등락률"])).round(1)
            rot = rot.sort_values("Momentum Score", ascending=False)

            # 자금 이동 화살표 (유출 → 유입)
            top5 = rot.head(5).index.tolist()
            bot3 = rot.tail(3).index.tolist()
            chain = " ".join(
                [f'<span style="color:#ff5252">{s}</span>' for s in reversed(bot3)]
                + ['<span class="rot-arrow" style="color:#f0a500;font-size:1.3em"> ⟶ </span>']
                + [f'<span style="color:#00e676;font-weight:bold">{s}</span>'
                   + (" → " if i < len(top5) - 1 else "") for i, s in enumerate(top5)])
            st.markdown(
                '<style>@keyframes pulse {0%{opacity:.3}50%{opacity:1}100%{opacity:.3}} '
                '.rot-arrow{animation:pulse 1.2s infinite}</style>'
                f'<div class="sm-ai">💸 자금 이동: {chain}</div>', unsafe_allow_html=True)
            st.markdown("")

            # ⑨ Sector Momentum Ranking TOP10
            st.subheader("🏅 Sector Momentum Ranking TOP10")
            fig_r = px.bar(rot.head(10).reset_index(), x="Momentum Score", y="섹터",
                           orientation="h", color="Momentum Score",
                           color_continuous_scale=["#d32f2f", "#f0a500", "#00c853"],
                           template=PLOTLY_TMPL, height=420)
            fig_r.update_layout(yaxis=dict(autorange="reversed"))
            st.plotly_chart(fig_r, use_container_width=True)
            st.dataframe(eokify(rot.reset_index()), use_container_width=True,
                         column_config=style_table(rot.reset_index()))
        except Exception as e:
            st.warning(f"섹터 분석 실패: {e}")

    # ═══ 🎬 Timeline ═══
    with t_tl:
        st.subheader("🎬 Smart Money Timeline — 자금 흐름 애니메이션")
        win_t = st.radio("기간(거래일)", [20, 60, 120], horizontal=True, key="tl_win")
        try:
            fl = load_flows_history(codes)
            top50 = df.nlargest(50, "거래대금")["종목코드"].tolist()
            sub = fl[fl["종목코드"].isin(top50)].copy()
            sub = sub[sub["날짜"] >= sub["날짜"].max() - pd.Timedelta(days=int(win_t * 1.6))]
            sub = sub.sort_values("날짜")
            sub["외국인누적"] = sub.groupby("종목코드")["외국인"].cumsum() / EOK
            sub["기관누적"] = sub.groupby("종목코드")["기관"].cumsum() / EOK
            name_map = dict(zip(base["종목코드"], base["종목명"]))
            sec_map2 = dict(zip(base["종목코드"], base["섹터"]))
            tvmap = dict(zip(base["종목코드"], base["거래대금"]))
            sub["종목명"] = sub["종목코드"].map(name_map)
            sub["섹터"] = sub["종목코드"].map(sec_map2)
            sub["크기"] = sub["종목코드"].map(tvmap).fillna(1) / EOK
            sub["일자"] = sub["날짜"].dt.strftime("%m-%d")
            fig_a = px.scatter(
                sub, x="외국인누적", y="기관누적", size="크기", color="섹터",
                hover_name="종목명", animation_frame="일자", animation_group="종목명",
                template=PLOTLY_TMPL, height=620,
                labels={"외국인누적": "외국인 누적 순매수(억)", "기관누적": "기관 누적 순매수(억)"})
            fig_a.add_hline(y=0, line_dash="dot", line_color="gray")
            fig_a.add_vline(x=0, line_dash="dot", line_color="gray")
            st.plotly_chart(fig_a, use_container_width=True)
            st.caption("▶ 재생 버튼으로 시간 흐름에 따른 자금 이동을 확인 (상위 50종목)")

            # ⑤ 종목별 4개 추이 한 화면
            st.markdown("---")
            st.subheader("📊 종목별 수급 4분할 추이")
            pick_t = st.selectbox("종목 선택", sorted(df["종목명"].tolist()), key="tl_pick",
                                  index=None, placeholder="종목명 입력")
            if pick_t:
                code_t = df.loc[df["종목명"] == pick_t, "종목코드"].iloc[0]
                pr_all = load_price_history(codes)
                p_one = pr_all[pr_all["종목코드"] == code_t].set_index("날짜").tail(win_t)
                f_one = fl[fl["종목코드"] == code_t].set_index("날짜").tail(win_t)
                fig4 = make_subplots(rows=2, cols=2, subplot_titles=(
                    "주가", "거래대금(억)", "외국인 누적(억)", "기관 누적(억)"))
                fig4.add_trace(go.Scatter(x=p_one.index, y=p_one["종가"],
                                          line=dict(color="#e8e8e8"), name="주가"), 1, 1)
                fig4.add_trace(go.Bar(x=p_one.index, y=p_one["거래대금"] / EOK,
                                      marker_color="#4caf50", name="거래대금"), 1, 2)
                fig4.add_trace(go.Scatter(x=f_one.index, y=f_one["외국인"].cumsum() / EOK,
                                          line=dict(color="#00b0ff"), name="외국인"), 2, 1)
                fig4.add_trace(go.Scatter(x=f_one.index, y=f_one["기관"].cumsum() / EOK,
                                          line=dict(color="#f0a500"), name="기관"), 2, 2)
                fig4.update_layout(template=PLOTLY_TMPL, height=560, showlegend=False,
                                   title=f"{pick_t} — 최근 {win_t}거래일")
                st.plotly_chart(fig4, use_container_width=True)
        except Exception as e:
            st.warning(f"타임라인 생성 실패: {e}")

    # ═══ 🧪 백테스트 ═══
    with t_bt:
        st.subheader("🧪 Score 백테스트")
        st.caption(f"최근 {HIST_DAYS}거래일(약 6개월) 데이터 기준 · 60일 후 수익률은 오래된 신호만 계산 가능")
        bt_target = st.radio("검증 대상", ["Signal Score", "Accumulation Score"],
                             horizontal=True, key="bt_target")
        thr = st.select_slider("신호 기준 Score", [70, 75, 80, 85, 90, 95], value=90, key="bt_thr")
        bc1, bc2 = st.columns(2)
        bt_fut = bc1.checkbox("+ 신호일 외국인 선물 순매수 조건", key="bt_fut")
        bt_prog = bc2.checkbox("+ 신호일 프로그램 순매수 조건", key="bt_prog")
        if st.button("▶ 백테스트 실행", key="bt_run", type="primary"):
            with st.spinner("일별 Score 재계산 및 수익률 분석 중..."):
                try:
                    edf, sdf = run_backtest(
                        codes, float(thr), bt_fut, bt_prog,
                        "accum" if bt_target == "Accumulation Score" else "signal")
                except Exception as e:
                    st.error(f"백테스트 실패: {e}")
                    edf, sdf = pd.DataFrame(), pd.DataFrame()
            if sdf.empty:
                st.info("조건을 충족한 신호가 없습니다. 기준 Score를 낮춰보세요.")
            else:
                st.success(f"Score ≥ {thr} 신호 {len(edf):,}건 분석 완료")
                st.dataframe(sdf, use_container_width=True, hide_index=True)
                name_map = dict(zip(base["종목코드"], base["종목명"]))
                edf["종목명"] = edf["종목코드"].map(name_map)
                show = edf.sort_values("날짜", ascending=False).head(200)
                cols_b = ["날짜", "종목명", "Score", "5일후", "10일후", "20일후", "60일후", "MDD20"]
                st.dataframe(show[cols_b].reset_index(drop=True), use_container_width=True, height=380)
                # 수익률 분포
                fig_h = px.histogram(edf.dropna(subset=["20일후"]), x="20일후", nbins=40,
                                     template=PLOTLY_TMPL, height=320,
                                     labels={"20일후": "20일 후 수익률(%)"})
                fig_h.add_vline(x=0, line_color="#f0a500")
                st.plotly_chart(fig_h, use_container_width=True)

    # ═══ 🛠️ 전략 시뮬레이터 ═══
    with t_sim:
        st.subheader("🛠️ 전략 시뮬레이터 — 나만의 조건 검색")
        sc1, sc2, sc3 = st.columns(3)
        with sc1:
            c_frg = st.number_input("외국인 순매수 ≥ (억)", value=0, step=10, key="sim_frg")
            c_ins = st.number_input("기관 순매수 ≥ (억)", value=0, step=10, key="sim_ins")
        with sc2:
            c_tvg = st.number_input("거래대금 증가율 ≥ (%)", value=0, step=10, key="sim_tvg")
            c_score = st.number_input("Smart Score ≥", value=0, step=5, key="sim_score")
        with sc3:
            c_ma = st.checkbox("20일선 위", key="sim_ma")
            c_hi = st.checkbox("20일 신고가", key="sim_hi")
            c_accum = st.checkbox("🔥 동반 연속 매집만", key="sim_ac")

        res = df.copy()
        res = res[(res["외국인순매수"] >= c_frg * EOK) & (res["기관순매수"] >= c_ins * EOK)
                  & (res["거래대금증가율"].fillna(-999) >= c_tvg) & (res["Score"] >= c_score)]
        if c_ma: res = res[res["20일선위"]]
        if c_hi: res = res[res["20일신고가"]]
        if c_accum: res = res[res["SmartAccum"]]
        res = res.sort_values("Score", ascending=False)

        st.markdown(f"**검색 결과: {len(res)}종목**")
        d = eokify(res[disp_cols]).reset_index(drop=True)
        d.index += 1
        st.dataframe(d, use_container_width=True, height=420,
                     column_config=style_table(res[disp_cols]))

        # 조건 저장 (세션 + JSON 파일)
        st.markdown("---")
        cond = {"외국인_억": c_frg, "기관_억": c_ins, "거래대금증가율": c_tvg,
                "Score": c_score, "20일선위": c_ma, "신고가": c_hi, "동반매집": c_accum}
        sv1, sv2, sv3 = st.columns([2, 2, 3])
        cname = sv1.text_input("전략 이름", key="sim_name", placeholder="예: 수급집중전략")
        if sv2.button("💾 세션에 저장", key="sim_save") and cname:
            st.session_state.setdefault("saved_strategies", {})[cname] = cond
            st.success(f"'{cname}' 저장됨 (브라우저 세션 유지 중 사용 가능)")
        sv3.download_button("📥 조건 JSON 다운로드",
                            json.dumps({cname or "strategy": cond}, ensure_ascii=False, indent=2),
                            file_name="smart_strategy.json", mime="application/json")
        saved = st.session_state.get("saved_strategies", {})
        if saved:
            st.caption("저장된 전략: " + " · ".join(
                f"**{k}** ({', '.join(f'{a}={b}' for a, b in v.items() if b)})"
                for k, v in saved.items()))

    st.caption(
        f"데이터: 네이버 금융(수급 근사치) + Yahoo Finance(가격) · 분석 대상 거래대금 상위 {RADAR_N}종목 · "
        f"수급 이력 {HIST_DAYS}거래일 · 15분 캐시 · 본 화면은 투자 참고용이며 투자 판단의 책임은 투자자 본인에게 있습니다")
