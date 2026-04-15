#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KOSPI 매매 신호 스캐너 v3 - Streamlit 웹 서비스
전략 선택 / 시장 필터 / 점수 가중치 개선판
"""
import time, warnings
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
warnings.filterwarnings("ignore")

import streamlit as st
import pandas as pd
import yfinance as yf

try:
    import FinanceDataReader as fdr
    FDR_OK = True
except Exception:
    FDR_OK = False

STOP_LOSS_PCT  = -5.0
TARGET_PCT     = +10.0
RSI_BUY_LOW    = 40
RSI_BUY_HIGH   = 60
RSI_SELL       = 70
MAX_WORKERS    = 8
VOL_SURGE_MULT = 1.5
BB_LOW_THRESH  = 0.25
BB_HIGH_THRESH = 0.75

STRATEGY_THRESHOLDS = {
    "SAFE":       {"buy": 7, "watch": 5},
    "NORMAL":     {"buy": 6, "watch": 4},
    "AGGRESSIVE": {"buy": 5, "watch": 3},
}

EXTRA = {
    "0080G0.KS": "KODEX 방산TOP10",
    "0091P0.KS": "TIGER 코리아원전",
    "395160.KS": "KODEX AI반도체",
    "487240.KS": "KODEX AI전력핵심설비",
    "0176E0.KS": "RISE 미국AI전력인프라",
    "138520.KS": "TIGER 삼성그룹",
    "005930.KS": "삼성전자",
    "000660.KS": "SK하이닉스",
    "006800.KS": "미래에셋증권",
    "010120.KS": "LS ELECTRIC",
    "034020.KS": "두산에너빌리티",
    "005380.KS": "현대차",
    "329180.KS": "HD현대중공업",
    "042660.KS": "한화오션",
    "000880.KS": "한화",
}

@st.cache_data(ttl=1800, show_spinner=False)
def market_filter():
    try:
        idx = yf.download("^KS11", period="3mo", progress=False)
        if isinstance(idx.columns, pd.MultiIndex):
            idx.columns = idx.columns.get_level_values(0)
        close = idx["Close"].squeeze()
        ma60  = close.rolling(60).mean()
        return bool(close.iloc[-1] > ma60.iloc[-1]), float(close.iloc[-1]), float(ma60.iloc[-1])
    except Exception:
        return True, 0.0, 0.0

@st.cache_data(ttl=3600, show_spinner=False)
def get_kospi200():
    if not FDR_OK:
        return {}
    try:
        df = fdr.StockListing('KOSPI')
        if df is None or len(df) == 0:
            return {}
        df.columns = [c.strip() for c in df.columns]
        code_col = next((c for c in df.columns if c in ('Code','Symbol','종목코드')), None)
        name_col = next((c for c in df.columns if c in ('Name','종목명','ShortName')), None)
        cap_col  = next((c for c in df.columns if c in ('Marcap','MarCap','시가총액','market_cap')), None)
        if code_col is None or name_col is None:
            return {}
        if cap_col:
            df = df.dropna(subset=[cap_col])
            df = df.sort_values(cap_col, ascending=False).head(200)
        else:
            df = df.head(200)
        result = {}
        for _, row in df.iterrows():
            code = str(row[code_col]).zfill(6)
            result[code + ".KS"] = str(row[name_col])
        return result
    except Exception:
        return {}

def get_signal(ticker, name, strategy="NORMAL", market_ok=True,
               rsi_low=40, rsi_high=60, vol_mult=1.5):
    try:
        df = yf.download(ticker, period="1y", interval="1d",
                         progress=False, auto_adjust=True)
        if df is None or len(df) < 70:
            return None
    except Exception:
        return None
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    close = df["Close"].squeeze()
    if not isinstance(close, pd.Series):
        close = pd.Series(close)
    df["MA20"] = close.rolling(20).mean()
    df["MA60"] = close.rolling(60).mean()
    delta = close.diff()
    gain  = delta.where(delta > 0, 0.0).rolling(14).mean()
    loss  = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
    rs    = gain / loss.replace(0, 1e-9)
    df["RSI"] = 100 - (100 / (1 + rs))
    ema12      = close.ewm(span=12, adjust=False).mean()
    ema26      = close.ewm(span=26, adjust=False).mean()
    df["MACD"] = ema12 - ema26
    df["Sig"]  = df["MACD"].ewm(span=9, adjust=False).mean()
    df["Hist"] = df["MACD"] - df["Sig"]
    bb_std       = close.rolling(20).std()
    df["BB_UP"]  = df["MA20"] + 2 * bb_std
    df["BB_LOW"] = df["MA20"] - 2 * bb_std
    bb_range     = (df["BB_UP"] - df["BB_LOW"]).replace(0, 1e-9)
    df["BB_PCT"] = (close - df["BB_LOW"]) / bb_range
    if "Volume" in df.columns:
        vol_s          = df["Volume"].squeeze()
        df["VOL_MA20"] = vol_s.rolling(20).mean()
        df["VOL_RATIO"]= vol_s / df["VOL_MA20"].replace(0, 1e-9)
    else:
        df["VOL_RATIO"] = 1.0
    try:
        lat   = df.iloc[-1]
        prev  = df.iloc[-2]
        price     = float(lat["Close"])
        ma20      = float(lat["MA20"])
        ma60      = float(lat["MA60"])
        rsi       = float(lat["RSI"])
        macd      = float(lat["MACD"])
        sig       = float(lat["Sig"])
        hist      = float(lat["Hist"])
        p_his     = float(prev["Hist"])
        bb_pct    = float(lat["BB_PCT"])
        bb_up     = float(lat["BB_UP"])
        bb_low    = float(lat["BB_LOW"])
        vol_ratio = float(lat["VOL_RATIO"])
    except Exception:
        return None
    change_1d = float(close.pct_change().iloc[-1]) * 100 if len(close) > 1 else 0.0
    c1 = ma20 > ma60
    c2 = rsi_low <= rsi <= rsi_high
    c3 = macd > sig
    c4 = vol_ratio >= vol_mult
    c5 = bb_pct <= BB_LOW_THRESH
    c6 = price > float(close.rolling(20).max().iloc[-2])
    c7 = change_1d > 0
    d1 = rsi >= RSI_SELL and hist < p_his
    d2 = price < ma20
    d3 = bb_pct >= BB_HIGH_THRESH
    score = 0
    if c1: score += 3
    if c3: score += 3
    if c2: score += 2
    if c4: score += 1
    if c5: score += 1
    thr = STRATEGY_THRESHOLDS[strategy]
    if not market_ok:
        signal = "NO TRADE"
        reason = f"하락장 진입 차단 | score={score} RSI={rsi:.1f}"
    elif d1 or (d2 and d3):
        signal = "SELL"
        parts  = []
        if d1: parts.append(f"RSI={rsi:.1f} 과매수+MACD꺾임")
        if d2: parts.append("MA20이탈")
        if d3: parts.append(f"BB상단({bb_pct:.2f})")
        reason = "|".join(parts)
    else:
        if strategy == "SAFE":
            if score >= thr["buy"] and c6 and c7:
                signal = "BUY"
            elif score >= thr["watch"]:
                signal = "WATCH"
            else:
                signal = "NEUTRAL"
        elif strategy == "NORMAL":
            if score >= thr["buy"] and c6:
                signal = "BUY"
            elif score >= thr["watch"]:
                signal = "WATCH"
            else:
                signal = "NEUTRAL"
        else:
            if score >= thr["buy"]:
                signal = "BUY"
            elif score >= thr["watch"]:
                signal = "WATCH"
            else:
                signal = "NEUTRAL"
        if signal == "BUY":
            extras = []
            if c6: extras.append("20일 고가 돌파")
            if c7: extras.append("당일 상승")
            if c4: extras.append(f"거래량급증x{vol_ratio:.1f}")
            if c5: extras.append("BB하단반등")
            base   = f"score={score}|MA정렬|RSI={rsi:.1f}|MACD골든"
            reason = base + ("|" + "|".join(extras) if extras else "")
        elif signal == "WATCH":
            conds  = [x for x, y in [("MA정렬",c1),(f"RSI={rsi:.1f}",c2),("MACD",c3)] if y]
            reason = "|".join(conds) + f" (score={score})"
            if c4: reason += "|거래량급증"
            if c5: reason += "|BB하단"
        else:
            reason = f"score={score}|MA:{'V' if c1 else 'X'} RSI:{'V' if c2 else 'X'} MACD:{'V' if c3 else 'X'}"
    target = stop = None
    if signal in ("BUY", "WATCH"):
        target = int(round(price * (1 + TARGET_PCT / 100)))
        stop   = int(round(price * (1 + STOP_LOSS_PCT / 100)))
    return dict(
        ticker=ticker, name=name, signal=signal, reason=reason,
        price=int(price), change_1d=round(change_1d, 2),
        ma20=ma20, ma60=ma60, rsi=rsi,
        macd=macd, macd_sig=sig,
        bb_pct=round(bb_pct, 3), bb_up=bb_up, bb_low=bb_low,
        vol_ratio=round(vol_ratio, 2),
        score=score, target=target, stop=stop,
        date=df.index[-1].strftime("%Y-%m-%d"),
    )

def scan_all(watchlist, strategy, market_ok, rsi_low, rsi_high, vol_mult,
             progress_bar, status_text):
    total   = len(watchlist)
    counter = [0]
    results = []
    def worker(item):
        ticker, name = item
        r = get_signal(ticker, name, strategy, market_ok, rsi_low, rsi_high, vol_mult)
        counter[0] += 1
        return r, counter[0], name
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(worker, it): it for it in list(watchlist.items())}
        for f in as_completed(futs):
            r, cnt, name = f.result()
            if r:
                results.append(r)
            pct = cnt / total
            progress_bar.progress(pct)
            status_text.text(f"스캔 중... {cnt}/{total}  {name[:12]}")
    return results

SIG_ICON = {"BUY":"🟢","WATCH":"🟡","SELL":"🔴","NEUTRAL":"⚪","NO TRADE":"🚫","STOPLOSS":"🚨"}
SIG_KR   = {"BUY":"매수","WATCH":"관심","SELL":"매도","NEUTRAL":"중립","NO TRADE":"진입차단","STOPLOSS":"손절"}

def to_df(lst):
    if not lst:
        return pd.DataFrame()
    rows = []
    for r in lst:
        rows.append({
            "종목명":    r["name"],
            "코드":      r["ticker"],
            "신호":      SIG_ICON.get(r["signal"],"") + " " + SIG_KR.get(r["signal"], r["signal"]),
            "점수":      r["score"],
            "현재가":    r["price"],
            "등락(%)":   r["change_1d"],
            "RSI":       round(r["rsi"], 1),
            "BB%B":      r["bb_pct"],
            "거래량배율": r["vol_ratio"],
            "목표가":    r["target"] or "-",
            "손절가":    r["stop"]   or "-",
            "날짜":      r["date"],
            "판단근거":  r["reason"],
        })
    return pd.DataFrame(rows)

st.set_page_config(page_title="KOSPI 매매 신호 스캐너 v3", page_icon="📊", layout="wide")
st.title("📊 KOSPI 매매 신호 스캐너 v3")
st.caption("기술지표 기반 자동 신호 분석 — MA / RSI / MACD / 볼린저밴드 / 거래량 / 전략 필터")

with st.sidebar:
    st.header("⚙️ 스캔 설정")
    strategy_map = {
        "보수형 — 돌파+당일상승 필수": "SAFE",
        "중립형 — 균형 (기본)":       "NORMAL",
        "공격형 — 저점 반등 포함":    "AGGRESSIVE",
    }
    strategy_label = st.radio("투자 전략", list(strategy_map.keys()), index=1)
    strategy = strategy_map[strategy_label]
    st.markdown("---")
    include_kospi200 = st.toggle("KOSPI 200 전체 스캔", value=True, help="OFF 시 EXTRA 관심종목 15개만 스캔")
    st.markdown("---")
    st.markdown("**파라미터 조정**")
    rsi_low  = st.slider("RSI 매수 하한", 20, 50, RSI_BUY_LOW)
    rsi_high = st.slider("RSI 매수 상한", 40, 75, RSI_BUY_HIGH)
    vol_mult = st.slider("거래량 급증 배율", 1.0, 3.0, VOL_SURGE_MULT, 0.1)
    st.markdown("---")
    st.markdown("**신호 필터**")
    show_buy     = st.checkbox("매수",   value=True)
    show_watch   = st.checkbox("관심",   value=True)
    show_sell    = st.checkbox("매도",   value=True)
    show_notrade = st.checkbox("진입차단", value=False)
    show_neutral = st.checkbox("중립",   value=False)

with st.spinner("KOSPI 시장 상태 확인 중..."):
    mkt_ok, kospi_now, kospi_ma60 = market_filter()

if mkt_ok:
    st.success(f"📈 상승장 — KOSPI {kospi_now:,.0f} > MA60 {kospi_ma60:,.0f}  |  신규 진입 가능")
else:
    st.error(f"📉 하락장 — KOSPI {kospi_now:,.0f} < MA60 {kospi_ma60:,.0f}  |  신규 진입 차단")

strat_desc = {
    "SAFE":       "보수형 — score >= 7 + 20일 고가 돌파 + 당일 상승 동시 충족 시 매수",
    "NORMAL":     "중립형 — score >= 6 + 20일 고가 돌파 시 매수",
    "AGGRESSIVE": "공격형 — score >= 5 이면 매수 (저점 반등 포함)",
}
st.info(strat_desc[strategy])

col_btn, col_last = st.columns([2, 8])
with col_btn:
    run_btn = st.button("스캔 시작", type="primary", use_container_width=True)
with col_last:
    if "last_scan" in st.session_state:
        st.caption(f"마지막 스캔: {st.session_state['last_scan']}")

if run_btn:
    with st.spinner("종목 리스트 로딩 중..."):
        watchlist = get_kospi200() if include_kospi200 else {}
        for k, v in EXTRA.items():
            watchlist.setdefault(k, v)
    total = len(watchlist)
    st.info(f"총 {total}개 종목 스캔 시작 · 전략: {strategy} · 시장: {'상승장' if mkt_ok else '하락장'}")
    pb   = st.progress(0)
    stat = st.empty()
    t0   = time.time()
    results = scan_all(watchlist, strategy, mkt_ok, rsi_low, rsi_high, vol_mult, pb, stat)
    elapsed = time.time() - t0
    pb.empty()
    stat.empty()
    st.session_state["results"]   = results
    st.session_state["last_scan"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st.success(f"스캔 완료 — {len(results)}개 분석 ({elapsed:.0f}초)")

if "results" in st.session_state:
    results = st.session_state["results"]
    buy_list     = sorted([r for r in results if r["signal"] == "BUY"],      key=lambda r: -r["score"])
    watch_list   = sorted([r for r in results if r["signal"] == "WATCH"],    key=lambda r: -r["score"])
    sell_list    = sorted([r for r in results if r["signal"] == "SELL"],     key=lambda r:  r["rsi"])
    notrade_list = [r for r in results if r["signal"] == "NO TRADE"]
    neutral_list = [r for r in results if r["signal"] == "NEUTRAL"]
    st.markdown("---")
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("매수",    len(buy_list))
    c2.metric("관심",    len(watch_list))
    c3.metric("매도",    len(sell_list))
    c4.metric("진입차단", len(notrade_list))
    c5.metric("중립",    len(neutral_list))
    c6.metric("전체",    len(results))
    st.markdown("---")
    tabs = st.tabs(["매수", "관심", "매도", "진입차단", "중립"])
    tab_data = [
        (tabs[0], buy_list,     show_buy,     "매수 신호 없음"),
        (tabs[1], watch_list,   show_watch,   "관심 종목 없음"),
        (tabs[2], sell_list,    show_sell,    "매도 신호 없음"),
        (tabs[3], notrade_list, show_notrade, "진입 차단 종목 없음"),
        (tabs[4], neutral_list, show_neutral, "중립 종목 없음"),
    ]
    for tab, lst, show, empty_msg in tab_data:
        with tab:
            if not show:
                st.info("사이드바에서 해당 신호를 활성화하세요.")
                continue
            df = to_df(lst)
            if df.empty:
                st.info(empty_msg)
            else:
                st.dataframe(
                    df, use_container_width=True, hide_index=True,
                    column_config={
                        "점수":      st.column_config.ProgressColumn("점수",  min_value=0, max_value=10, format="%d"),
                        "현재가":    st.column_config.NumberColumn("현재가",   format="%d원"),
                        "등락(%)":   st.column_config.NumberColumn("등락(%)", format="%.2f%%"),
                        "목표가":    st.column_config.NumberColumn("목표가",   format="%d원"),
                        "손절가":    st.column_config.NumberColumn("손절가",   format="%d원"),
                        "거래량배율": st.column_config.NumberColumn("거래량",  format="x%.1f"),
                    }
                )
    st.markdown("---")
    all_signal = buy_list + watch_list + sell_list
    if all_signal:
        csv_df = to_df(all_signal)
        st.download_button(
            label="결과 CSV 다운로드",
            data=csv_df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig"),
            file_name=f"kospi_signal_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
            mime="text/csv",
)
