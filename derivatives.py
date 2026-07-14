#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
파생시장 데이터 모듈 (네이버 금융)
- KOSPI200 선물 투자자별 순매수 (외국인/기관/개인, 억원)
- 프로그램 매매 (차익/비차익/전체 순매수, 억원)
- Market Risk Score(Bull Score) 계산
※ 옵션 투자자별 데이터는 공개 소스가 없어 제외 (KRX 차단, 네이버 서비스 중단)
"""
import io
import re
from datetime import datetime

import numpy as np
import pandas as pd
import requests
import streamlit as st

NAVER_HDR = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
}
EOK = 1e8

def _parse_date(k: str) -> pd.Timestamp:
    return pd.Timestamp(2000 + int(k[:2]), int(k[3:5]), int(k[6:8]))

# ═══════════════════════════════════════════════════════
@st.cache_data(ttl=600, show_spinner=False)
def load_futures_trend(days: int = 60) -> pd.DataFrame:
    """KOSPI200 선물 투자자별 일별 순매수 (억원) — sosok=03"""
    collected = {}
    bizdate = datetime.now().strftime("%Y%m%d")
    for _ in range(days // 10 + 3):
        try:
            r = requests.get(
                f"https://finance.naver.com/sise/investorDealTrendDay.naver"
                f"?bizdate={bizdate}&sosok=03", headers=NAVER_HDR, timeout=15)
            r.encoding = "euc-kr"
            t = pd.read_html(io.StringIO(r.text))[0]
        except Exception:
            break
        t.columns = ["날짜", "개인", "외국인", "기관계"] + [f"c{i}" for i in range(t.shape[1] - 4)]
        t = t.dropna(subset=["날짜"])
        new = 0
        for _, row in t.iterrows():
            d = str(row["날짜"]).strip()
            if not re.match(r"\d{2}\.\d{2}\.\d{2}", d):
                continue
            if d not in collected:
                collected[d] = (float(row["개인"]), float(row["외국인"]), float(row["기관계"]))
                new += 1
        if len(collected) >= days or new == 0:
            break
        oldest = min(collected)
        bizdate = (_parse_date(oldest) - pd.Timedelta(days=1)).strftime("%Y%m%d")

    if not collected:
        return pd.DataFrame()
    df = pd.DataFrame(
        [(_parse_date(k), *v) for k, v in collected.items()],
        columns=["날짜", "개인", "외국인", "기관"]).sort_values("날짜")
    return df.tail(days).set_index("날짜")

@st.cache_data(ttl=600, show_spinner=False)
def load_program_trend(days: int = 60) -> pd.DataFrame:
    """프로그램 매매 일별 (차익/비차익/전체 순매수, 억원)"""
    collected = {}
    bizdate = datetime.now().strftime("%Y%m%d")
    for _ in range(days // 10 + 3):
        try:
            r = requests.get(
                f"https://finance.naver.com/sise/programDealTrendDay.naver"
                f"?bizdate={bizdate}", headers=NAVER_HDR, timeout=15)
            r.encoding = "euc-kr"
            t = pd.read_html(io.StringIO(r.text))[0]
        except Exception:
            break
        # 컬럼: 날짜 | 차익 매수/매도/순매수 | 비차익 매수/매도/순매수 | 전체 매수/매도/순매수
        t.columns = ["날짜", "차익매수", "차익매도", "차익순매수",
                     "비차익매수", "비차익매도", "비차익순매수",
                     "전체매수", "전체매도", "전체순매수"][:t.shape[1]]
        t = t.dropna(subset=["날짜"])
        new = 0
        for _, row in t.iterrows():
            d = str(row["날짜"]).strip()
            if not re.match(r"\d{2}\.\d{2}\.\d{2}", d):
                continue
            if d not in collected:
                collected[d] = (float(row["차익순매수"]), float(row["비차익순매수"]),
                                float(row["전체순매수"]))
                new += 1
        if len(collected) >= days or new == 0:
            break
        oldest = min(collected)
        bizdate = (_parse_date(oldest) - pd.Timedelta(days=1)).strftime("%Y%m%d")

    if not collected:
        return pd.DataFrame()
    df = pd.DataFrame(
        [(_parse_date(k), *v) for k, v in collected.items()],
        columns=["날짜", "차익", "비차익", "프로그램"]).sort_values("날짜")
    return df.tail(days).set_index("날짜")

# ═══════════════════════════════════════════════════════
def get_deriv_state(p: int = 5):
    """파생시장 상태 요약 → dict
    fut_frg / fut_ins / prog / arb / nonarb : 최근 p일 합(억원)
    fut_pos / prog_pos / deriv_pos / deriv_neg : 방향 플래그
    """
    out = dict(ok=False, fut_frg=0.0, fut_ins=0.0, fut_ind=0.0,
               prog=0.0, arb=0.0, nonarb=0.0,
               fut_pos=False, prog_pos=False, deriv_pos=False, deriv_neg=False)
    try:
        fut = load_futures_trend(60)
        prog = load_program_trend(60)
        if fut.empty or prog.empty:
            return out
        out["fut_frg"] = float(fut["외국인"].tail(p).sum())
        out["fut_ins"] = float(fut["기관"].tail(p).sum())
        out["fut_ind"] = float(fut["개인"].tail(p).sum())
        out["prog"] = float(prog["프로그램"].tail(p).sum())
        out["arb"] = float(prog["차익"].tail(p).sum())
        out["nonarb"] = float(prog["비차익"].tail(p).sum())
        out["fut_pos"] = out["fut_frg"] > 0
        out["prog_pos"] = out["prog"] > 0
        out["deriv_pos"] = out["fut_pos"] and out["prog_pos"]
        out["deriv_neg"] = (out["fut_frg"] < 0) and (out["prog"] < 0)
        out["ok"] = True
    except Exception:
        pass
    return out

def bull_score(spot_trend: pd.DataFrame, idx_value: pd.DataFrame, p: int = 5):
    """Market Risk Score (Bull Score 0~100) + 등급
    구성(각 백분위, 60일 기준):
      외국인 선물 25 · 프로그램 20 · 외국인 현물 20 · 기관 현물 20 · 거래대금 증가 15
    """
    comps = {}
    def pctile(series: pd.Series, cur: float) -> float:
        s = series.dropna()
        if len(s) < 10:
            return 50.0
        return float((s < cur).mean() * 100)

    try:
        fut = load_futures_trend(60)
        prog = load_program_trend(60)
        f_roll = fut["외국인"].rolling(p).sum()
        p_roll = prog["프로그램"].rolling(p).sum()
        comps["외국인 선물"] = pctile(f_roll, f_roll.iloc[-1])
        comps["프로그램"] = pctile(p_roll, p_roll.iloc[-1])
    except Exception:
        comps["외국인 선물"] = comps["프로그램"] = 50.0

    try:
        sf = spot_trend["외국인"].rolling(p).sum()
        si = spot_trend["기관"].rolling(p).sum()
        comps["외국인 현물"] = pctile(sf, sf.iloc[-1])
        comps["기관 현물"] = pctile(si, si.iloc[-1])
    except Exception:
        comps["외국인 현물"] = comps["기관 현물"] = 50.0

    try:
        tv = idx_value["거래대금"].rolling(p).mean()
        comps["거래대금"] = pctile(tv, tv.iloc[-1])
    except Exception:
        comps["거래대금"] = 50.0

    score = (0.25 * comps["외국인 선물"] + 0.20 * comps["프로그램"]
             + 0.20 * comps["외국인 현물"] + 0.20 * comps["기관 현물"]
             + 0.15 * comps["거래대금"])
    score = float(np.clip(score, 0, 100))

    if score >= 80:   grade, stars = "Strong Bull", "★★★★★"
    elif score >= 60: grade, stars = "Bull", "★★★★"
    elif score >= 40: grade, stars = "Neutral", "★★★"
    elif score >= 20: grade, stars = "Bear", "★★"
    else:             grade, stars = "Strong Bear", "★"
    return round(score, 1), grade, stars, comps

def confirmation_label(stock_bull: bool, fut_pos: bool) -> str:
    """종목 방향 × 파생 방향 → Confirmation Signal"""
    if stock_bull and fut_pos:
        return "🟢✔ Strong Confirm"
    if stock_bull and not fut_pos:
        return "🟡△ Weak Confirm"
    if (not stock_bull) and fut_pos:
        return "🔵 Early Signal"
    return "🔴✔ Bear Confirm"
