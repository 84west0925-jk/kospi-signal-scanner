#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🔄 테마 로테이션 지도 (Theme Rotation Map)
==================================================
"현재 한국 주식시장의 자금이 어느 테마에서 빠져나가고, 어느 테마로 이동하고 있는지"를
거래대금(자금 흐름) 변화로 시각화하는 독립 분석 모듈.

설계 원칙
- X축(자기 거래대금 범위 내 상대 위치) · Y축(거래대금 모멘텀) 모두 "거래대금" 기준이며,
  주가 등락률은 절대 rotation 위치 계산에 쓰지 않는다 (Tooltip/보조지표 전용).
- 종목 → 테마 매핑은 기존 sectors.py의 대표 섹터(단일, 종목당 1개 — 중복 집계 없음)를
  그대로 재사용한다. 필요 시 프로젝트 루트의 theme_mapping.csv(ticker,name,primary_theme)로
  개별 종목의 테마를 덮어쓸 수 있다 (파일이 없으면 조용히 무시됨 — 선택 사항).
- 당일 전종목 시세는 smart_money.load_all_stocks()(KOSPI)를 재사용하고, KOSDAQ은 동일한
  네이버 API 패턴으로 이 모듈에서만 새로 수집한다. 종목별 일별 거래대금 이력은
  smart_radar.load_price_history()를 그대로 재사용해 캐시를 공유한다(중복 수집 없음).

데이터 소스: 네이버 금융(당일 시세) + yfinance(일별 가격·거래량 이력, smart_radar 재사용)
"""
import os
import inspect
from typing import Optional

import numpy as np
import pandas as pd
import requests
import streamlit as st

try:
    import plotly.graph_objects as go
    PLOTLY_OK = True
except ImportError:
    PLOTLY_OK = False

from smart_money import (
    EOK, NAVER_HDR, PLOTLY_TMPL, DARK_CSS, _n, fmt_eok, load_all_stocks,
)
import smart_radar

try:
    from sectors import attach_sectors
    SECTORS_OK = True
except Exception:
    SECTORS_OK = False

# ══════════════════════════════════════════════════════
# 설정값 (Settings)
# ══════════════════════════════════════════════════════
ROTATION_X_METHOD_DEFAULT = "percentile"   # "percentile" 또는 "minmax"
IGNORE_ONE_DAY_REVERSAL = True             # 최근 3일 중 정반대 방향 1일 노이즈 제거

UNIVERSE_TOP_N   = 350   # 로테이션 분석 대상: 당일 거래대금 상위 N종목 (이력 조회 대상)
MIN_HISTORY_DAYS = 30    # 이 미만 이력을 가진 종목은 테마 집계에서 제외
MIN_THEME_STOCKS = 2     # 소속 종목이 이 미만인 테마는 분석에서 제외 (노이즈 방지)

MARKET_OPTIONS         = ["전체", "KOSPI", "KOSDAQ"]
MOMENTUM_DAY_OPTIONS    = [1, 3, 5]
RELATIVE_RANGE_OPTIONS  = [20, 60, 120]
TAIL_OPTIONS            = [3, 5]
THEME_COUNT_OPTIONS     = {"TOP 10": 10, "TOP 15": 15, "TOP 20": 20, "전체": None}
STOCK_COUNT_OPTIONS     = {"TOP 5": 5, "TOP 10": 10, "TOP 15": 15, "전체": None}
MIN_AMOUNT_OPTIONS      = {
    "제한 없음": 0, "100억 이상": 100, "300억 이상": 300,
    "500억 이상": 500, "1000억 이상": 1000,
}

QUADRANT_COLOR = {
    "EMERGING":     "#00c896",
    "LEADING":      "#f0a500",
    "WEAKENING":    "#ff5252",
    "OUT_OF_FAVOR": "#8a93a6",
}
QUADRANT_LABEL = {
    "EMERGING":     "부상 중",
    "LEADING":      "주도",
    "WEAKENING":    "주도에서 이탈 중",
    "OUT_OF_FAVOR": "관심 밖",
}

THEME_MAPPING_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "theme_mapping.csv")


# ══════════════════════════════════════════════════════
# 1) 데이터 수집 — 당일 전종목 시세 (market 필터: KOSPI / KOSDAQ / 전체)
# ══════════════════════════════════════════════════════
@st.cache_data(ttl=600, show_spinner=False)
def _load_kosdaq_stocks() -> pd.DataFrame:
    """KOSDAQ 전종목 시세 — smart_money.load_all_stocks()의 KOSPI 수집 로직과 동일한 패턴."""
    rows, page = [], 1
    while page <= 40:
        try:
            r = requests.get(
                f"https://m.stock.naver.com/api/stocks/marketValue/KOSDAQ"
                f"?page={page}&pageSize=100", headers=NAVER_HDR, timeout=15)
            data = r.json()
        except Exception:
            break
        stocks = data.get("stocks", [])
        if not stocks:
            break
        rows.extend(stocks)
        if page * 100 >= int(data.get("totalCount", 0)):
            break
        page += 1

    recs = []
    for s in rows:
        if s.get("stockEndType") != "stock":
            continue
        try:
            chg = _n(s.get("fluctuationsRatio"))
            code = s.get("compareToPreviousPrice", {}).get("code", "")
            if code in ("4", "5") and chg > 0:
                chg = -chg
            recs.append({
                "종목코드": s["itemCode"], "종목명": s["stockName"],
                "현재가": _n(s["closePrice"]), "등락률": chg,
                "거래량": _n(s["accumulatedTradingVolume"]),
                "거래대금": _n(s["accumulatedTradingValue"]) * 1e6,   # 백만원 → 원
                "시가총액": _n(s["marketValue"]) * 1e8,               # 억원 → 원
            })
        except Exception:
            continue
    df = pd.DataFrame(recs)
    if not df.empty:
        if SECTORS_OK:
            try:
                df = attach_sectors(df)
            except Exception:
                df["섹터"], df["테마"] = "기타", ""
        else:
            df["섹터"], df["테마"] = "기타", ""
    return df


@st.cache_data(ttl=3600, show_spinner=False)
def _load_theme_overrides() -> dict:
    """선택 사항: 프로젝트 루트 theme_mapping.csv(ticker,name,primary_theme)로 개별 종목 테마 덮어쓰기."""
    if not os.path.exists(THEME_MAPPING_CSV):
        return {}
    try:
        ov = pd.read_csv(THEME_MAPPING_CSV, dtype=str)
        ov = ov.dropna(subset=["ticker", "primary_theme"])
        ov["ticker"] = ov["ticker"].astype(str).str.zfill(6)
        return dict(zip(ov["ticker"], ov["primary_theme"]))
    except Exception:
        return {}


def apply_theme_overrides(df: pd.DataFrame) -> pd.DataFrame:
    overrides = _load_theme_overrides()
    if not overrides or df.empty:
        return df
    df = df.copy()
    mask = df["종목코드"].isin(overrides)
    df.loc[mask, "섹터"] = df.loc[mask, "종목코드"].map(overrides)
    return df


@st.cache_data(ttl=600, show_spinner=False)
def load_market_data(market: str = "전체") -> pd.DataFrame:
    """당일 전종목 시세 + 대표 섹터(테마) — market: '전체' / 'KOSPI' / 'KOSDAQ'."""
    frames = []
    if market in ("KOSPI", "전체"):
        try:
            kospi = load_all_stocks()
            if not kospi.empty:
                kospi = kospi.copy()
                kospi["시장"] = "KOSPI"
                frames.append(kospi)
        except Exception:
            pass
    if market in ("KOSDAQ", "전체"):
        try:
            kosdaq = _load_kosdaq_stocks()
            if not kosdaq.empty:
                kosdaq = kosdaq.copy()
                kosdaq["시장"] = "KOSDAQ"
                frames.append(kosdaq)
        except Exception:
            pass
    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset="종목코드", keep="first")
    df = df[df["거래대금"] > 0]                     # 거래정지·거래대금 0 종목 제외
    if "섹터" not in df.columns:
        df["섹터"] = "기타"
    df["섹터"] = df["섹터"].fillna("기타").replace("", "기타")
    return df.reset_index(drop=True)


def get_universe(df: pd.DataFrame, top_n: int = UNIVERSE_TOP_N) -> tuple:
    """이력 조회 대상 종목코드(당일 거래대금 상위 N)."""
    if df.empty:
        return tuple()
    d = df[df["거래대금"] > 0].nlargest(top_n, "거래대금")
    return tuple(d["종목코드"].tolist())


def load_universe_history(codes: tuple) -> pd.DataFrame:
    """종목별 일별 가격·거래대금 이력 — smart_radar 캐시를 그대로 재사용(중복 수집 없음)."""
    return smart_radar.load_price_history(codes)


# ══════════════════════════════════════════════════════
# 2) 테마 집계 (Aggregation)
# ══════════════════════════════════════════════════════
def aggregate_theme_data(price_df: pd.DataFrame, code2theme: dict) -> dict:
    """
    종목별 일별 이력을 테마별로 합산.
    반환: {'tv': DataFrame(날짜×테마, 거래대금 합계),
           'ret': DataFrame(날짜×테마, 거래대금가중 평균 등락률 %),
           'n_stocks': {테마: 종목수}}
    """
    empty = {"tv": pd.DataFrame(), "ret": pd.DataFrame(), "n_stocks": {}}
    if price_df.empty:
        return empty

    p = price_df.copy()
    p["테마"] = p["종목코드"].map(code2theme)
    p = p.dropna(subset=["테마"])
    if p.empty:
        return empty

    tv_pivot    = p.pivot_table(index="날짜", columns="종목코드", values="거래대금", aggfunc="last")
    close_pivot = p.pivot_table(index="날짜", columns="종목코드", values="종가", aggfunc="last")
    code_theme  = p.drop_duplicates("종목코드").set_index("종목코드")["테마"]
    n_stocks    = code_theme.value_counts().to_dict()

    ret_pivot = close_pivot.pct_change(fill_method=None) * 100
    weight    = tv_pivot.shift(1)          # 전일 거래대금 가중(당일 신호에 대한 look-ahead 없음)

    tv_theme, ret_theme = pd.DataFrame(index=tv_pivot.index), pd.DataFrame(index=tv_pivot.index)
    for th in sorted(code_theme.unique()):
        cs = [c for c in tv_pivot.columns if code_theme.get(c) == th]
        if not cs:
            continue
        tv_theme[th] = tv_pivot[cs].sum(axis=1, min_count=1)
        wsum = weight[cs].sum(axis=1)
        rw   = (ret_pivot[cs] * weight[cs]).sum(axis=1)
        ret_theme[th] = rw / wsum.replace(0, np.nan)

    return {"tv": tv_theme, "ret": ret_theme, "n_stocks": n_stocks}


def calculate_theme_share(tv_theme: pd.DataFrame) -> pd.DataFrame:
    """테마 거래대금 점유율(%) = 테마 거래대금 / (분석대상 유니버스) 전체 거래대금 × 100."""
    if tv_theme.empty:
        return tv_theme
    total = tv_theme.sum(axis=1)
    return tv_theme.div(total.replace(0, np.nan), axis=0) * 100


# ══════════════════════════════════════════════════════
# 3) X축 — 거래대금 상대 위치 (percentile / minmax, 함수화)
# ══════════════════════════════════════════════════════
def calculate_rotation_position(share: pd.Series, window: int,
                                method: str = ROTATION_X_METHOD_DEFAULT) -> pd.Series:
    """
    테마 자신의 과거 window일 거래대금 점유율 범위 대비 현재 위치 (0~100).
    rolling(window)는 각 시점까지의 과거 데이터만 사용 — look-ahead bias 없음.
    rolling_max == rolling_min(변동 없음) 구간은 중립값 50으로 대체.
    """
    minp = max(5, window // 3)
    if method == "minmax":
        roll_min = share.rolling(window, min_periods=minp).min()
        roll_max = share.rolling(window, min_periods=minp).max()
        rng = roll_max - roll_min
        pos = (share - roll_min) / rng.replace(0, np.nan) * 100
    else:  # percentile (기본값, 극단값에 덜 민감)
        pos = share.rolling(window, min_periods=minp).apply(
            lambda w: w.rank(pct=True).iloc[-1] * 100, raw=False)
    return pos.clip(0, 100).fillna(50.0)


def _relative_position_frame(share_df: pd.DataFrame, window: int, method: str) -> pd.DataFrame:
    return pd.DataFrame({c: calculate_rotation_position(share_df[c], window, method)
                         for c in share_df.columns})


# ══════════════════════════════════════════════════════
# 4) Y축 — 거래대금 모멘텀
# ══════════════════════════════════════════════════════
def calculate_rotation_momentum(tv: pd.Series, days: int) -> pd.Series:
    """최근 N일 평균 거래대금 vs 직전 N일 평균 거래대금 변화율(%)."""
    short_avg = tv.rolling(days).mean()
    prev_avg  = tv.shift(days).rolling(days).mean()
    return (short_avg / prev_avg.replace(0, np.nan) - 1) * 100


def _momentum_frame(tv_df: pd.DataFrame, days: int) -> pd.DataFrame:
    return pd.DataFrame({c: calculate_rotation_momentum(tv_df[c], days) for c in tv_df.columns})


# ══════════════════════════════════════════════════════
# 5) 버블 크기 · 상태 판정 · 이동 방향 · Smart Money 후보
# ══════════════════════════════════════════════════════
def bubble_size(share_value: float, scale: float = 9.0,
                min_size: float = 16.0, max_size: float = 64.0) -> float:
    if share_value is None or pd.isna(share_value) or share_value <= 0:
        return min_size
    return float(np.clip(np.sqrt(share_value) * scale, min_size, max_size))


def detect_rotation_state(x: float, y: float) -> str:
    """EMERGING(부상 중) / LEADING(주도) / WEAKENING(이탈 중) / OUT_OF_FAVOR(관심 밖)."""
    if pd.isna(x) or pd.isna(y):
        return "OUT_OF_FAVOR"
    if x >= 50:
        return "LEADING" if y >= 0 else "WEAKENING"
    return "EMERGING" if y > 0 else "OUT_OF_FAVOR"


def _direction_label(dx: float, dy: float) -> str:
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return "→ 유지"
    if dx >= 0 and dy >= 0:
        return "↗ 부상"
    if dx >= 0 and dy < 0:
        return "↘ 약화"
    if dx < 0 and dy < 0:
        return "← 이탈"
    return "↖ 회복"


def compute_direction(tail_x: list, tail_y: list, ignore_reversal: bool = IGNORE_ONE_DAY_REVERSAL) -> str:
    """꼬리(D-2→...→Today) 경로로부터 최근 이동 방향 산출. 정반대 1일 노이즈는 옵션으로 무시."""
    xs = [v for v in tail_x if pd.notna(v)]
    ys = [v for v in tail_y if pd.notna(v)]
    if len(xs) < 2 or len(ys) < 2:
        return "→ 유지"
    end_i = -1
    if ignore_reversal and len(xs) >= 3:
        dx_last, dy_last = xs[-1] - xs[-2], ys[-1] - ys[-2]
        dx_prev, dy_prev = xs[-2] - xs[-3], ys[-2] - ys[-3]
        opp_x = dx_last != 0 and dx_prev != 0 and np.sign(dx_last) == -np.sign(dx_prev)
        opp_y = dy_last != 0 and dy_prev != 0 and np.sign(dy_last) == -np.sign(dy_prev)
        if opp_x and opp_y:
            end_i = -2   # 마지막 1일짜리 반전 무시하고 그 이전 지점 기준으로 방향 판단
    return _direction_label(xs[end_i] - xs[0], ys[end_i] - ys[0])


def detect_early_rotation(pos_series: pd.Series, tv_series: pd.Series,
                          momentum_now: float, price_return_pct: float,
                          lookback: int = 5) -> bool:
    """
    'Smart Money 후보' — 🔥 Early Rotation:
      1) lookback일 전 percentile/relative position < 50 (관심 밖·부상 초입에서 출발)
      2) 최근 거래대금 모멘텀 > +20%
      3) 3거래일 연속 거래대금 증가
      4) 아직 가격 수익률 +10% 미만 (많이 오르지 않은 상태)
    """
    if pos_series is None or tv_series is None:
        return False
    valid_pos = pos_series.dropna()
    if len(valid_pos) <= lookback:
        return False
    started_low = valid_pos.iloc[-(lookback + 1)] < 50
    momentum_ok = pd.notna(momentum_now) and momentum_now > 20
    recent_tv = tv_series.dropna().tail(3)
    three_up = len(recent_tv) == 3 and recent_tv.is_monotonic_increasing
    price_ok = pd.isna(price_return_pct) or price_return_pct < 10
    return bool(started_low and momentum_ok and three_up and price_ok)


# ══════════════════════════════════════════════════════
# 5-1) 종목 Drill-down — 테마 내부 종목 로테이션
# ══════════════════════════════════════════════════════
def _safe_compound_return(close: pd.Series, days: int) -> float:
    """최근 N거래일 종가 기준 누적수익률(%). 데이터 부족 시 NaN."""
    c = close.dropna()
    if len(c) < days + 1:
        return np.nan
    base = float(c.iloc[-(days + 1)])
    last = float(c.iloc[-1])
    if base <= 0:
        return np.nan
    return (last / base - 1) * 100


def _series_last(series: pd.Series, default=np.nan) -> float:
    """Series 마지막 유효값을 안전하게 float로 반환."""
    if series is None:
        return default
    s = series.dropna()
    if s.empty:
        return default
    try:
        return float(s.iloc[-1])
    except Exception:
        return default


def build_stock_rotation_table(price_hist: pd.DataFrame, snapshot: pd.DataFrame,
                               code2theme: dict, valid_themes: list,
                               tv_theme: pd.DataFrame, momentum_days: int,
                               rel_window: int, tail_days: int,
                               x_method: str) -> tuple:
    """
    테마 내부 종목별 거래대금 로테이션 계산.

    X축: 종목 자신의 과거 거래대금 범위 내 상대 위치(0~100)
    Y축: 최근 N일 평균 거래대금 vs 직전 N일 평균 거래대금 변화율(%)

    반환: (stock_df, stock_tails)
    """
    if price_hist.empty or snapshot.empty:
        return pd.DataFrame(), {}

    p = price_hist.copy()
    required = {"종목코드", "날짜", "거래대금", "종가"}
    if not required.issubset(set(p.columns)):
        return pd.DataFrame(), {}

    p["종목코드"] = p["종목코드"].astype(str).str.zfill(6)
    p["테마"] = p["종목코드"].map(code2theme)
    p = p[p["테마"].isin(valid_themes)].copy()
    if p.empty:
        return pd.DataFrame(), {}

    snap = snapshot.copy()
    snap["종목코드"] = snap["종목코드"].astype(str).str.zfill(6)
    snap_lookup = snap.drop_duplicates("종목코드").set_index("종목코드")

    # 테마 거래대금은 기존 테마 집계값을 그대로 사용해 종목합계와 논리적으로 맞춘다.
    theme_current_tv = {}
    for th in valid_themes:
        if th in tv_theme.columns and not tv_theme[th].dropna().empty:
            theme_current_tv[th] = _series_last(tv_theme[th], 0.0)
    valid_market_total = sum(v for v in theme_current_tv.values() if pd.notna(v) and v > 0)

    global_as_of = pd.to_datetime(p["날짜"], errors="coerce").max()

    rows, tails = [], {}
    for code, g in p.groupby("종목코드", sort=False):
        g = g.sort_values("날짜").drop_duplicates("날짜", keep="last")
        theme = code2theme.get(code)
        if theme not in valid_themes:
            continue

        tv = pd.to_numeric(g["거래대금"], errors="coerce")
        tv.index = pd.to_datetime(g["날짜"], errors="coerce")
        tv = tv[~tv.index.isna()].sort_index()
        close = pd.to_numeric(g["종가"], errors="coerce")
        close.index = pd.to_datetime(g["날짜"], errors="coerce")
        close = close[~close.index.isna()].sort_index()

        if tv.dropna().empty:
            continue
        # 테마 집계의 마지막 기준일과 동일한 날짜의 거래대금만 현재값으로 사용한다.
        # 종목별 마지막 유효일을 따로 쓰면 휴장/결측 종목이 이전 거래대금으로 섞여
        # 테마 합계와 종목 합계가 어긋날 수 있다.
        if pd.isna(global_as_of) or global_as_of not in tv.index or pd.isna(tv.loc[global_as_of]):
            continue
        cur_tv = float(tv.loc[global_as_of])
        if cur_tv <= 0:
            continue

        # 종목 X축은 거래대금 자체의 과거 위치를 사용한다.
        pos = calculate_rotation_position(tv, rel_window, x_method)
        pos_pct = (pos if x_method == "percentile"
                   else calculate_rotation_position(tv, rel_window, "percentile"))
        mom = calculate_rotation_momentum(tv, momentum_days)

        cur_x = _series_last(pos, 50.0)
        cur_y_raw = _series_last(mom, np.nan)
        cur_y = cur_y_raw if pd.notna(cur_y_raw) else 0.0
        pct_ref = _series_last(pos_pct, np.nan)

        n_tail = min(tail_days + 1, len(pos))
        tail_x = pos.tail(n_tail).tolist()
        tail_y = mom.tail(n_tail).tolist()
        stock_tails_key = str(code).zfill(6)
        tails[stock_tails_key] = {"x": tail_x, "y": tail_y}

        state = detect_rotation_state(cur_x, cur_y)
        direction = compute_direction(tail_x, tail_y)

        r1 = _safe_compound_return(close, 1)
        r5 = _safe_compound_return(close, 5)
        r20 = _safe_compound_return(close, 20)

        tv20 = tv.dropna().tail(20).mean() if tv.dropna().shape[0] >= 5 else np.nan
        vs20 = ((cur_tv / tv20 - 1) * 100) if pd.notna(tv20) and tv20 > 0 else np.nan

        mom1 = _series_last(calculate_rotation_momentum(tv, 1), np.nan)
        mom3 = _series_last(calculate_rotation_momentum(tv, 3), np.nan)
        mom5 = _series_last(calculate_rotation_momentum(tv, 5), np.nan)

        price_ref = r20 if pd.notna(r20) else r5
        early = detect_early_rotation(pos, tv, cur_y, price_ref, lookback=tail_days)

        theme_tv = theme_current_tv.get(theme, 0.0)
        theme_share = (cur_tv / theme_tv * 100) if theme_tv and theme_tv > 0 else np.nan
        market_share = (cur_tv / valid_market_total * 100) if valid_market_total > 0 else np.nan

        if code in snap_lookup.index:
            sr = snap_lookup.loc[code]
            name = str(sr.get("종목명", code))
            market = str(sr.get("시장", ""))
            current_price = _n(sr.get("현재가"))
            intraday_tv = _n(sr.get("거래대금"))
        else:
            name = str(code)
            market = ""
            current_price = _series_last(close, 0.0)
            intraday_tv = np.nan

        rows.append({
            "종목코드": str(code).zfill(6),
            "종목명": name,
            "시장": market,
            "테마": theme,
            "현재가": current_price,
            "거래대금": cur_tv,
            "장중거래대금": intraday_tv,
            "테마내비중": round(theme_share, 2) if pd.notna(theme_share) else np.nan,
            "시장비중": round(market_share, 3) if pd.notna(market_share) else np.nan,
            "X": round(cur_x, 1),
            "Y": round(cur_y, 1),
            "거래대금변화_1일": round(mom1, 1) if pd.notna(mom1) else np.nan,
            "거래대금변화_3일": round(mom3, 1) if pd.notna(mom3) else np.nan,
            "거래대금변화_5일": round(mom5, 1) if pd.notna(mom5) else np.nan,
            "20일평균대비": round(vs20, 1) if pd.notna(vs20) else np.nan,
            "60일Percentile": round(pct_ref, 1) if pd.notna(pct_ref) else np.nan,
            "가격등락_1일": round(r1, 2) if pd.notna(r1) else np.nan,
            "가격등락_5일": round(r5, 2) if pd.notna(r5) else np.nan,
            "가격등락_20일": round(r20, 2) if pd.notna(r20) else np.nan,
            "상태": state,
            "상태라벨": QUADRANT_LABEL[state],
            "방향": direction,
            "EarlyRotation": early,
        })

    if not rows:
        return pd.DataFrame(), tails

    stock_df = pd.DataFrame(rows).replace([np.inf, -np.inf], np.nan)
    stock_df = stock_df.sort_values(["테마", "거래대금"], ascending=[True, False]).reset_index(drop=True)
    return stock_df, tails


def calculate_stock_theme_share(stock_df: pd.DataFrame, theme: str) -> pd.Series:
    """선택 테마 내 종목 거래대금 비중(%)을 재계산할 때 쓰는 보조 함수."""
    d = stock_df[stock_df["테마"] == theme].copy()
    if d.empty:
        return pd.Series(dtype=float)
    total = d["거래대금"].sum()
    if total <= 0:
        return pd.Series(index=d.index, dtype=float)
    return d["거래대금"] / total * 100


def calculate_stock_rotation_position(tv: pd.Series, window: int,
                                      method: str = ROTATION_X_METHOD_DEFAULT) -> pd.Series:
    """종목 X축 계산용 alias — 기존 계산 로직을 그대로 재사용."""
    return calculate_rotation_position(tv, window, method)


def calculate_stock_rotation_momentum(tv: pd.Series, days: int) -> pd.Series:
    """종목 Y축 계산용 alias — 기존 계산 로직을 그대로 재사용."""
    return calculate_rotation_momentum(tv, days)


def detect_stock_rotation_state(x: float, y: float) -> str:
    """종목 상태 판정용 alias."""
    return detect_rotation_state(x, y)


def create_theme_stock_summary(theme_df: pd.DataFrame, theme: str) -> dict:
    """선택 테마의 집중도·Breadth·자금유입 1위·품질 코멘트 생성."""
    d = theme_df.copy().sort_values("거래대금", ascending=False)
    if d.empty:
        return {
            "theme": theme, "theme_tv": 0.0, "top1_name": "—", "top1_share": 0.0,
            "top3_share": 0.0, "top5_share": 0.0, "breadth": 0.0,
            "breadth_label": "데이터 부족", "inflow_name": "—", "comment": "종목 데이터가 없습니다."
        }

    theme_tv = float(d["거래대금"].sum())
    top1 = d.iloc[0]
    top1_share = float(d["테마내비중"].fillna(0).iloc[0])
    top3_share = float(d["테마내비중"].fillna(0).head(3).sum())
    top5_share = float(d["테마내비중"].fillna(0).head(5).sum())

    valid_mom = d["Y"].dropna()
    breadth = float((valid_mom > 0).mean() * 100) if not valid_mom.empty else 0.0
    if breadth >= 80:
        breadth_label = "테마 전체 확산"
    elif breadth >= 50:
        breadth_label = "양호한 확산"
    elif breadth >= 30:
        breadth_label = "일부 종목 중심"
    else:
        breadth_label = "특정 종목 쏠림"

    inflow_d = d.sort_values("Y", ascending=False)
    inflow_name = str(inflow_d.iloc[0]["종목명"]) if not inflow_d.empty else "—"

    positive_names = d[d["Y"] > 0].sort_values("Y", ascending=False)["종목명"].tolist()
    spread_names = [n for n in positive_names if n != str(top1["종목명"])][:2]
    spread_txt = "·".join(spread_names)

    if breadth >= 70 and top1_share < 50:
        comment = (f"{theme}는 {top1['종목명']}뿐 아니라 "
                   f"{spread_txt if spread_txt else '다수 구성 종목'}으로 거래대금이 확산되고 있습니다. "
                   f"Breadth {breadth:.0f}%로 테마 전반의 건강한 자금 유입으로 판단됩니다.")
    elif breadth >= 50 and top1_share >= 50:
        comment = (f"{theme} 거래대금은 증가하고 있지만 {top1['종목명']} 비중이 {top1_share:.1f}%로 높습니다. "
                   f"현재는 테마 전체 확산보다 대장주 주도형 흐름에 가깝습니다.")
    elif breadth < 30:
        comment = (f"{theme}의 자금 유입 종목 비율이 {breadth:.0f}%에 그칩니다. "
                   f"일부 종목 쏠림 가능성이 높아 테마 강도의 신뢰도는 낮게 보는 편이 안전합니다.")
    else:
        comment = (f"{theme}는 일부 종목을 중심으로 선택적인 자금 유입이 나타나고 있습니다. "
                   f"Breadth {breadth:.0f}%로 추가 확산 여부를 확인할 필요가 있습니다.")

    return {
        "theme": theme,
        "theme_tv": theme_tv,
        "top1_name": str(top1["종목명"]),
        "top1_share": top1_share,
        "top3_share": top3_share,
        "top5_share": top5_share,
        "breadth": breadth,
        "breadth_label": breadth_label,
        "inflow_name": inflow_name,
        "comment": comment,
    }


def make_stock_ranking_table(theme_df: pd.DataFrame) -> pd.DataFrame:
    """선택 테마 구성 종목의 자금흐름 상세 랭킹."""
    if theme_df.empty:
        return pd.DataFrame()
    d = theme_df.copy().sort_values("Y", ascending=False).reset_index(drop=True)
    out = pd.DataFrame({
        "종목명": d["종목명"],
        "종목코드": d["종목코드"],
        "현재가": d["현재가"],
        "상태": d["상태라벨"] + np.where(d["EarlyRotation"], " 🔥", ""),
        "거래대금(억)": (d["거래대금"] / EOK).round(0),
        "테마내비중(%)": d["테마내비중"],
        "거래대금변화율(%)": d["Y"],
        "20일평균대비(%)": d["20일평균대비"],
        "60일Percentile": d["60일Percentile"],
        "1일등락(%)": d["가격등락_1일"],
        "5일등락(%)": d["가격등락_5일"],
        "20일등락(%)": d["가격등락_20일"],
        "최근방향": d["방향"],
        "Early Rotation": np.where(d["EarlyRotation"], "🔥", "—"),
    })
    out.index = out.index + 1
    out.index.name = "순위"
    return out.reset_index()


def _stock_bubble_size(theme_share: float) -> float:
    """종목 차트 버블 크기 — 테마 내 비중 기준 sqrt scaling."""
    if pd.isna(theme_share) or theme_share <= 0:
        return 15.0
    return float(np.clip(np.sqrt(theme_share) * 8.5, 15.0, 58.0))


def create_stock_rotation_chart(theme_df: pd.DataFrame, stock_tails: dict,
                                theme: str, momentum_days: int,
                                max_stocks: Optional[int] = 10):
    """선택 테마 내부 종목 로테이션 미니맵."""
    d = theme_df.copy().sort_values("거래대금", ascending=False)
    if max_stocks:
        d = d.head(max_stocks)

    y_vals = [float(v) for v in d["Y"].tolist() if pd.notna(v)]
    y_pad = max(8.0, (max(y_vals) - min(y_vals)) * 0.20) if y_vals else 8.0
    y_min = min(y_vals) - y_pad if y_vals else -20.0
    y_max = max(y_vals) + y_pad if y_vals else 20.0
    if y_min >= 0:
        y_min = -5.0
    if y_max <= 0:
        y_max = 5.0

    fig = go.Figure()
    fig.add_shape(type="rect", x0=0, x1=50, y0=0, y1=y_max, fillcolor="rgba(0,200,150,0.07)", line_width=0, layer="below")
    fig.add_shape(type="rect", x0=50, x1=100, y0=0, y1=y_max, fillcolor="rgba(240,165,0,0.08)", line_width=0, layer="below")
    fig.add_shape(type="rect", x0=50, x1=100, y0=y_min, y1=0, fillcolor="rgba(255,82,82,0.07)", line_width=0, layer="below")
    fig.add_shape(type="rect", x0=0, x1=50, y0=y_min, y1=0, fillcolor="rgba(138,147,166,0.07)", line_width=0, layer="below")
    fig.add_vline(x=50, line_dash="dot", line_color="rgba(255,255,255,0.25)")
    fig.add_hline(y=0, line_dash="dot", line_color="rgba(255,255,255,0.25)")
    fig.add_annotation(x=2, y=y_max * 0.94, text="부상 중", showarrow=False, xanchor="left", font=dict(color=QUADRANT_COLOR["EMERGING"], size=12))
    fig.add_annotation(x=98, y=y_max * 0.94, text="주도", showarrow=False, xanchor="right", font=dict(color=QUADRANT_COLOR["LEADING"], size=12))
    fig.add_annotation(x=98, y=y_min * 0.94, text="이탈 중", showarrow=False, xanchor="right", font=dict(color=QUADRANT_COLOR["WEAKENING"], size=12))
    fig.add_annotation(x=2, y=y_min * 0.94, text="관심 밖", showarrow=False, xanchor="left", font=dict(color=QUADRANT_COLOR["OUT_OF_FAVOR"], size=12))

    for _, row in d.iterrows():
        code = str(row["종목코드"]).zfill(6)
        color = QUADRANT_COLOR[row["상태"]]
        tail = stock_tails.get(code, {"x": [row["X"]], "y": [row["Y"]]})
        pairs = [(x, y) for x, y in zip(tail.get("x", []), tail.get("y", []))
                 if pd.notna(x) and pd.notna(y)]
        if len(pairs) >= 2:
            tx, ty = zip(*pairs)
            fig.add_trace(go.Scatter(
                x=list(tx), y=list(ty), mode="lines+markers",
                line=dict(color=color, width=1.2),
                marker=dict(size=5, color=color, opacity=0.35),
                opacity=0.45, showlegend=False, hoverinfo="skip"))

        vs20_txt = "—" if pd.isna(row["20일평균대비"]) else f"{row['20일평균대비']:+.1f}%"
        pct_txt = "—" if pd.isna(row["60일Percentile"]) else f"{row['60일Percentile']:.0f}%"
        r5_txt = "—" if pd.isna(row["가격등락_5일"]) else f"{row['가격등락_5일']:+.1f}%"
        r20_txt = "—" if pd.isna(row["가격등락_20일"]) else f"{row['가격등락_20일']:+.1f}%"
        price_txt = f"{row['현재가']:,.0f}원" if pd.notna(row["현재가"]) else "—"
        share_txt = "—" if pd.isna(row["테마내비중"]) else f"{row['테마내비중']:.1f}%"
        hover = (
            f"<b>{row['종목명']} ({code})</b><br>"
            f"현재가: {price_txt}<br>"
            f"거래대금: {fmt_eok(row['거래대금'])}<br>"
            f"테마 내 비중: {share_txt}<br>"
            f"모멘텀({momentum_days}일): {row['Y']:+.1f}%"
            f"<br>"
            f"20일 평균 대비: {vs20_txt}<br>"
            f"60일 Percentile: {pct_txt}<br>"
            f"상태: {row['상태라벨']}<br>"
            f"최근 이동: {row['방향']}<br>"
            f"가격등락(5일/20일): {r5_txt} / {r20_txt}"
            + ("<br>🔥 Early Rotation 후보" if row["EarlyRotation"] else "")
            + "<extra></extra>"
        )
        label = f"<b>{row['종목명']}</b><br>{share_txt} · {row['Y']:+.1f}%"
        fig.add_trace(go.Scatter(
            x=[row["X"]], y=[row["Y"]], mode="markers+text",
            marker=dict(size=_stock_bubble_size(row["테마내비중"]), color=color,
                        line=dict(width=2 if row["EarlyRotation"] else 1,
                                  color="#ffffff" if row["EarlyRotation"] else color),
                        opacity=0.92),
            text=[label], textposition="middle right", textfont=dict(size=10, color="#e8e8e8"),
            hovertemplate=hover, showlegend=False, name=str(row["종목명"])))

    fig.update_layout(
        template=PLOTLY_TMPL, height=560,
        title=dict(text=f"{theme} 종목 로테이션 (거래대금 기준)", x=0.01, xanchor="left"),
        xaxis=dict(title="← 자기 거래대금 범위 하단        자기 거래대금 범위 상단 →",
                   range=[-3, 118], zeroline=False),
        yaxis=dict(title="거래대금 모멘텀 (%)", range=[y_min, y_max], zeroline=False),
        margin=dict(t=55, b=50), hovermode="closest",
    )
    return fig


def _extract_selected_theme(plot_event) -> Optional[str]:
    """Streamlit Plotly selection event에서 customdata 테마명을 안전하게 추출."""
    if plot_event is None:
        return None
    try:
        selection = getattr(plot_event, "selection", None)
        if selection is None and isinstance(plot_event, dict):
            selection = plot_event.get("selection")
        if selection is None:
            return None
        points = getattr(selection, "points", None)
        if points is None and isinstance(selection, dict):
            points = selection.get("points", [])
        if not points:
            return None
        point = points[-1]
        custom = point.get("customdata") if isinstance(point, dict) else getattr(point, "customdata", None)
        if isinstance(custom, (list, tuple)) and custom:
            return str(custom[0])
        if isinstance(custom, str):
            return custom
    except Exception:
        return None
    return None


def _supports_plotly_selection() -> bool:
    """현재 Streamlit의 st.plotly_chart가 on_select를 지원하는지 확인."""
    try:
        return "on_select" in inspect.signature(st.plotly_chart).parameters
    except Exception:
        return False


# ══════════════════════════════════════════════════════
# 6) 메인 빌드 — 종목수집 → 테마집계 → rotation 위치/모멘텀 계산
# ══════════════════════════════════════════════════════
@st.cache_data(ttl=600, show_spinner=False)
def build_rotation_table(market: str, momentum_days: int, rel_window: int,
                         tail_days: int, x_method: str,
                         top_n_universe: int = UNIVERSE_TOP_N) -> dict:
    snapshot = load_market_data(market)
    if snapshot.empty:
        return {"error": "시세 데이터를 불러오지 못했습니다. 잠시 후 다시 시도하세요."}
    snapshot = apply_theme_overrides(snapshot)

    codes = get_universe(snapshot, top_n_universe)
    if not codes:
        return {"error": "분석 대상 종목이 없습니다."}

    price_hist = load_universe_history(codes)
    if price_hist.empty:
        return {"error": "종목별 거래대금 이력을 불러오지 못했습니다. (yfinance)"}

    code2theme = dict(zip(snapshot["종목코드"], snapshot["섹터"]))
    agg = aggregate_theme_data(price_hist, code2theme)
    tv_theme, ret_theme, n_stocks = agg["tv"], agg["ret"], agg["n_stocks"]
    if tv_theme.empty:
        return {"error": "테마별 거래대금 집계에 실패했습니다. (매핑 누락 가능성)"}

    valid_themes = [t for t in tv_theme.columns
                    if n_stocks.get(t, 0) >= MIN_THEME_STOCKS
                    and tv_theme[t].dropna().shape[0] >= MIN_HISTORY_DAYS]
    excluded = sorted(set(tv_theme.columns) - set(valid_themes))
    tv_theme = tv_theme[valid_themes]
    ret_theme = ret_theme[[c for c in valid_themes if c in ret_theme.columns]]
    if tv_theme.empty:
        return {"error": "60일 이력이 충분한 테마가 없습니다."}

    share = calculate_theme_share(tv_theme)
    pos   = _relative_position_frame(share, rel_window, x_method)
    mom   = _momentum_frame(tv_theme, momentum_days)
    pos_pct_ref = (pos if x_method == "percentile"
                  else _relative_position_frame(share, rel_window, "percentile"))

    rows, tails = [], {}
    for th in valid_themes:
        s, p, m, tv = share[th], pos[th], mom[th], tv_theme[th]
        if s.dropna().empty or p.dropna().empty:
            continue

        cur_share = float(s.iloc[-1]) if pd.notna(s.iloc[-1]) else 0.0
        cur_x     = float(p.iloc[-1]) if pd.notna(p.iloc[-1]) else 50.0
        cur_y     = float(m.iloc[-1]) if pd.notna(m.iloc[-1]) else 0.0
        cur_tv    = float(tv.iloc[-1]) if pd.notna(tv.iloc[-1]) else 0.0
        if cur_tv <= 0:
            continue   # 당일 거래대금 데이터가 없는 테마는 제외 (거래정지/이력 결측 등)

        n_tail = min(tail_days + 1, len(p))
        tail_x = p.tail(n_tail).tolist()
        tail_y = m.tail(n_tail).tolist()
        tails[th] = {"x": tail_x, "y": tail_y}

        state     = detect_rotation_state(cur_x, cur_y)
        direction = compute_direction(tail_x, tail_y)

        rt = ret_theme[th] if th in ret_theme.columns else pd.Series(dtype=float)
        r1  = float(rt.iloc[-1]) if not rt.dropna().empty else np.nan
        r5  = (float((1 + rt.tail(5) / 100).prod() - 1) * 100) if rt.dropna().shape[0] >= 5 else np.nan
        r20 = (float((1 + rt.tail(20) / 100).prod() - 1) * 100) if rt.dropna().shape[0] >= 20 else np.nan

        tv20 = tv.tail(20).mean() if tv.dropna().shape[0] >= 20 else np.nan
        vs20 = ((cur_tv / tv20 - 1) * 100) if pd.notna(tv20) and tv20 > 0 else np.nan
        pct60 = float(pos_pct_ref[th].iloc[-1]) if th in pos_pct_ref.columns and pd.notna(pos_pct_ref[th].iloc[-1]) else np.nan

        price_ref = r20 if pd.notna(r20) else r5
        early = detect_early_rotation(p, tv, cur_y, price_ref, lookback=tail_days)

        rows.append({
            "테마": th, "상태": state, "상태라벨": QUADRANT_LABEL[state],
            "X": round(cur_x, 1), "Y": round(cur_y, 1),
            "거래대금": cur_tv, "거래대금비중": round(cur_share, 2),
            "종목수": n_stocks.get(th, 0),
            "5일평균거래대금": float(tv.tail(5).mean()) if tv.dropna().shape[0] >= 5 else np.nan,
            "직전5일평균거래대금": float(tv.iloc[-10:-5].mean()) if len(tv) >= 10 else np.nan,
            "20일평균대비": round(vs20, 1) if pd.notna(vs20) else np.nan,
            "60일Percentile": round(pct60, 1) if pd.notna(pct60) else np.nan,
            "가격등락_1일": round(r1, 2) if pd.notna(r1) else np.nan,
            "가격등락_5일": round(r5, 2) if pd.notna(r5) else np.nan,
            "가격등락_20일": round(r20, 2) if pd.notna(r20) else np.nan,
            "방향": direction, "EarlyRotation": early,
        })

    if not rows:
        return {"error": "분석 가능한 테마가 없습니다. (데이터 부족)"}

    rot_df = pd.DataFrame(rows).replace([np.inf, -np.inf], np.nan)
    rot_df = rot_df.sort_values("Y", ascending=False).reset_index(drop=True)

    # 테마 내부 종목 Drill-down 데이터도 같은 price_hist로 계산한다.
    # 테마 선택 변경 시 추가 다운로드가 발생하지 않도록 build 단계에서 함께 캐시한다.
    stock_df, stock_tails = build_stock_rotation_table(
        price_hist=price_hist, snapshot=snapshot, code2theme=code2theme,
        valid_themes=valid_themes, tv_theme=tv_theme,
        momentum_days=momentum_days, rel_window=rel_window,
        tail_days=tail_days, x_method=x_method,
    )

    return {
        "df": rot_df, "tails": tails,
        "stock_df": stock_df, "stock_tails": stock_tails,
        "snapshot": snapshot, "excluded": excluded,
        "as_of": price_hist["날짜"].max(), "n_universe": len(codes),
    }


# ══════════════════════════════════════════════════════
# 7) 랭킹 테이블 · 핵심 시그널 요약
# ══════════════════════════════════════════════════════
def make_ranking_table(rot_df: pd.DataFrame) -> pd.DataFrame:
    d = rot_df.sort_values("Y", ascending=False).reset_index(drop=True)
    out = pd.DataFrame({
        "테마": d["테마"],
        "상태": d["상태라벨"] + np.where(d["EarlyRotation"], " 🔥", ""),
        "거래대금(억)": (d["거래대금"] / EOK).round(0),
        "비중(%)": d["거래대금비중"],
        "변화율(%)": d["Y"],
        "20일평균대비(%)": d["20일평균대비"],
        "60일Percentile": d["60일Percentile"],
        "최근방향": d["방향"],
    })
    out.index = out.index + 1
    out.index.name = "순위"
    return out.reset_index()


def create_rotation_summary(rot_df: pd.DataFrame) -> dict:
    """핵심 시그널 한 줄 요약 + 3개 카드(신규 부상/현재 주도/자금 이탈)."""
    d = rot_df.copy()
    outflow_all = d[d["Y"] < 0].sort_values("Y")
    inflow_pref = d[d["상태"].isin(["LEADING", "EMERGING"])].sort_values("Y", ascending=False)
    inflow = inflow_pref["테마"].tolist()
    if len(inflow) < 2:
        inflow = d.sort_values("Y", ascending=False)["테마"].tolist()

    outflow_txt = "·".join(outflow_all["테마"].head(2).tolist())
    inflow_txt  = "·".join(inflow[:3])
    if outflow_txt and inflow_txt:
        headline = f"자금은 {outflow_txt}에서 이탈하고 있으며, {inflow_txt}(으)로 집중되고 있습니다."
    elif inflow_txt:
        headline = f"자금은 {inflow_txt}(으)로 집중되고 있습니다."
    else:
        headline = "뚜렷한 자금 쏠림 없이 테마별로 혼조된 흐름입니다."

    emerging = d[(d["상태"] == "EMERGING") | (d["EarlyRotation"])].sort_values("Y", ascending=False)
    leading  = d[d["상태"] == "LEADING"].sort_values("거래대금비중", ascending=False)
    leaving  = d[d["상태"].isin(["WEAKENING", "OUT_OF_FAVOR"])].sort_values("Y")

    return {
        "headline": headline,
        "emerging": emerging["테마"].head(3).tolist(),
        "leading":  leading["테마"].head(3).tolist(),
        "outflow":  leaving["테마"].head(3).tolist(),
    }


# ══════════════════════════════════════════════════════
# 8) Plotly 차트
# ══════════════════════════════════════════════════════
def create_rotation_chart(rot_df: pd.DataFrame, tails: dict, x_method: str, momentum_days: int,
                          stock_df: Optional[pd.DataFrame] = None):
    df = rot_df.copy()
    y_vals = [v for v in df["Y"].tolist() if pd.notna(v)]
    y_pad = max(10.0, (max(y_vals) - min(y_vals)) * 0.18) if y_vals else 10.0
    y_min = (min(y_vals) - y_pad) if y_vals else -20.0
    y_max = (max(y_vals) + y_pad) if y_vals else 20.0
    if y_min >= 0: y_min = -5.0
    if y_max <= 0: y_max = 5.0

    fig = go.Figure()

    # 4분면 배경 (약한 opacity)
    fig.add_shape(type="rect", x0=0, x1=50, y0=0, y1=y_max, fillcolor="rgba(0,200,150,0.07)", line_width=0, layer="below")
    fig.add_shape(type="rect", x0=50, x1=100, y0=0, y1=y_max, fillcolor="rgba(240,165,0,0.08)", line_width=0, layer="below")
    fig.add_shape(type="rect", x0=50, x1=100, y0=y_min, y1=0, fillcolor="rgba(255,82,82,0.07)", line_width=0, layer="below")
    fig.add_shape(type="rect", x0=0, x1=50, y0=y_min, y1=0, fillcolor="rgba(138,147,166,0.07)", line_width=0, layer="below")
    fig.add_vline(x=50, line_dash="dot", line_color="rgba(255,255,255,0.25)")
    fig.add_hline(y=0, line_dash="dot", line_color="rgba(255,255,255,0.25)")

    fig.add_annotation(x=2, y=y_max * 0.94, text="부상 중", showarrow=False, xanchor="left", font=dict(color="#00c896", size=13))
    fig.add_annotation(x=98, y=y_max * 0.94, text="주도", showarrow=False, xanchor="right", font=dict(color="#f0a500", size=13))
    fig.add_annotation(x=98, y=y_min * 0.94, text="주도에서 이탈 중", showarrow=False, xanchor="right", font=dict(color="#ff5252", size=13))
    fig.add_annotation(x=2, y=y_min * 0.94, text="관심 밖", showarrow=False, xanchor="left", font=dict(color="#8a93a6", size=13))

    for _, row in df.iterrows():
        th = row["테마"]
        color = QUADRANT_COLOR[row["상태"]]
        tail = tails.get(th, {"x": [row["X"]], "y": [row["Y"]]})
        tx, ty = tail["x"], tail["y"]
        older_x, older_y = tx[:-1], ty[:-1]

        if older_x:
            m = len(older_x)
            fig.add_trace(go.Scatter(
                x=older_x + [row["X"]], y=older_y + [row["Y"]], mode="lines",
                line=dict(color=color, width=1.3), opacity=0.45,
                showlegend=False, hoverinfo="skip"))
            fig.add_trace(go.Scatter(
                x=older_x, y=older_y, mode="markers",
                marker=dict(
                    size=[bubble_size(row["거래대금비중"]) * (0.3 + 0.4 * i / max(m - 1, 1)) for i in range(m)],
                    color=color,
                    opacity=[0.35 + 0.35 * i / max(m - 1, 1) for i in range(m)],
                    symbol=(["circle-open"] + ["circle"] * (m - 1)) if m > 1 else ["circle-open"],
                    line=dict(width=1, color=color)),
                showlegend=False, hoverinfo="skip"))

        top_stock_txt = ""
        if stock_df is not None and not stock_df.empty:
            td = stock_df[stock_df["테마"] == th].sort_values("거래대금", ascending=False).head(3)
            if not td.empty:
                lines = []
                for rank, (_, sr) in enumerate(td.iterrows(), start=1):
                    share_txt = "—" if pd.isna(sr["테마내비중"]) else f"{sr['테마내비중']:.1f}%"
                    lines.append(f"{rank}. {sr['종목명']} {share_txt}")
                top_stock_txt = "<br><b>거래대금 상위 종목</b><br>" + "<br>".join(lines)

        vs20_txt = "—" if pd.isna(row["20일평균대비"]) else f"{row['20일평균대비']:+.1f}%"
        pct_txt  = "—" if pd.isna(row["60일Percentile"]) else f"{row['60일Percentile']:.0f}%"
        r5_txt   = "—" if pd.isna(row["가격등락_5일"]) else f"{row['가격등락_5일']:+.1f}%"
        r20_txt  = "—" if pd.isna(row["가격등락_20일"]) else f"{row['가격등락_20일']:+.1f}%"
        hover = (
            f"<b>{th}</b><br>"
            f"현재 거래대금: {fmt_eok(row['거래대금'])}<br>"
            f"시장 거래대금 비중: {row['거래대금비중']:.1f}%<br>"
            f"모멘텀({momentum_days}일): {row['Y']:+.1f}%"
            f"{top_stock_txt}<br>"
            f"20일 평균 대비: {vs20_txt}<br>"
            f"60일 Percentile: {pct_txt}<br>"
            f"현재 상태: {row['상태라벨']} ({row['상태']})<br>"
            f"최근 이동: {row['방향']}<br>"
            f"가격등락(5일/20일): {r5_txt} / {r20_txt}"
            + ("<br>🔥 Early Rotation 후보" if row["EarlyRotation"] else "")
            + "<extra></extra>"
        )
        label = f"<b>{th}</b><br>{row['거래대금비중']:.1f}% · {row['Y']:+.1f}%"
        fig.add_trace(go.Scatter(
            x=[row["X"]], y=[row["Y"]], mode="markers+text",
            marker=dict(size=bubble_size(row["거래대금비중"]), color=color,
                       line=dict(width=2 if row["EarlyRotation"] else 1,
                                 color="#ffffff" if row["EarlyRotation"] else color),
                       opacity=0.92),
            text=[label], textposition="middle right", textfont=dict(size=11, color="#e8e8e8"),
            customdata=[[th]],
            hovertemplate=hover, showlegend=False, name=th))

    fig.update_layout(
        template=PLOTLY_TMPL, height=680,
        xaxis=dict(title="← 자기 거래대금 범위 하단        자기 거래대금 범위 상단 →",
                  range=[-3, 118], zeroline=False),
        yaxis=dict(title="거래대금 모멘텀 (%)", range=[y_min, y_max], zeroline=False),
        margin=dict(t=40, b=50), hovermode="closest",
    )
    return fig


# ══════════════════════════════════════════════════════
# 9) 메인 렌더링
# ══════════════════════════════════════════════════════
def render():
    st.markdown(DARK_CSS, unsafe_allow_html=True)
    st.markdown('<h2 class="sm-title">🔄 테마 로테이션 지도</h2>', unsafe_allow_html=True)
    st.caption("거래대금 흐름으로 보는 시장 자금 이동  |  데이터: 네이버 금융 + Yahoo Finance · 예측 보장이 아닙니다")

    if not PLOTLY_OK:
        st.error("plotly 패키지가 없습니다. requirements.txt를 확인하세요.")
        return

    f1, f2, f3, f4 = st.columns(4)
    market = f1.radio("시장", MARKET_OPTIONS, horizontal=True, index=0, key="rot_market")
    momentum_days = f2.radio("거래대금 모멘텀", MOMENTUM_DAY_OPTIONS, horizontal=True, index=2,
                             key="rot_momentum", format_func=lambda d: f"{d}D")
    rel_window = f3.radio("Relative Range", RELATIVE_RANGE_OPTIONS, horizontal=True, index=1,
                          key="rot_relwin", format_func=lambda d: f"{d}D")
    tail_days = f4.radio("Tail", TAIL_OPTIONS, horizontal=True, index=0,
                         key="rot_tail", format_func=lambda d: f"{d}D")

    f5, f6, f7, f8 = st.columns(4)
    min_amt_label = f5.selectbox("최소 거래대금", list(MIN_AMOUNT_OPTIONS.keys()), index=0, key="rot_minamt")
    theme_count_label = f6.selectbox("테마 개수", list(THEME_COUNT_OPTIONS.keys()), index=1, key="rot_count")
    x_method_label = f7.selectbox(
        "X축 계산 방식", ["percentile (추천)", "minmax"],
        index=(0 if ROTATION_X_METHOD_DEFAULT == "percentile" else 1), key="rot_xmethod")
    x_method = "percentile" if x_method_label.startswith("percentile") else "minmax"
    if f8.button("🔄 데이터 갱신", use_container_width=True, key="rot_refresh"):
        st.cache_data.clear()
        st.rerun()

    with st.spinner("테마·종목 로테이션 데이터 계산 중... (최초 1~2분, 이후 캐시)"):
        result = build_rotation_table(market, momentum_days, rel_window, tail_days, x_method)

    if "error" in result:
        st.warning(result["error"])
        return

    rot_df = result["df"].copy()
    stock_df = result.get("stock_df", pd.DataFrame()).copy()

    min_amt = MIN_AMOUNT_OPTIONS[min_amt_label]
    if min_amt:
        rot_df = rot_df[rot_df["거래대금"] >= min_amt * EOK]

    theme_count = THEME_COUNT_OPTIONS[theme_count_label]
    if theme_count and len(rot_df) > theme_count:
        keep = rot_df.sort_values("거래대금비중", ascending=False).head(theme_count)["테마"].tolist()
        rot_df = rot_df[rot_df["테마"].isin(keep)]

    if rot_df.empty:
        st.info("필터 조건을 만족하는 테마가 없습니다. 최소 거래대금 조건을 낮춰보세요.")
        return
    rot_df = rot_df.sort_values("Y", ascending=False).reset_index(drop=True)

    summary = create_rotation_summary(rot_df)
    st.markdown(f'<div class="sm-ai">💸 <b>오늘의 자금 흐름</b><br>{summary["headline"]}</div>',
                unsafe_allow_html=True)
    st.markdown("")

    c1, c2, c3 = st.columns(3)
    c1.metric("🌱 신규 부상 테마", summary["emerging"][0] if summary["emerging"] else "—",
              " · ".join(summary["emerging"][1:3]))
    c2.metric("🏆 현재 주도 테마", summary["leading"][0] if summary["leading"] else "—",
              " · ".join(summary["leading"][1:3]))
    c3.metric("📉 자금 이탈 테마", summary["outflow"][0] if summary["outflow"] else "—",
              " · ".join(summary["outflow"][1:3]), delta_color="inverse")

    # ── 테마 로테이션 지도 + 클릭(지원 버전) ───────────────────────────────
    theme_fig = create_rotation_chart(rot_df, result["tails"], x_method, momentum_days, stock_df)
    clicked_theme = None
    if _supports_plotly_selection():
        try:
            event = st.plotly_chart(
                theme_fig, use_container_width=True, key="rot_theme_chart",
                on_select="rerun", selection_mode="points")
            clicked_theme = _extract_selected_theme(event)
        except TypeError:
            st.plotly_chart(theme_fig, use_container_width=True, key="rot_theme_chart_fallback")
    else:
        st.plotly_chart(theme_fig, use_container_width=True, key="rot_theme_chart")

    visible_themes = rot_df["테마"].tolist()
    if clicked_theme in visible_themes:
        st.session_state["rot_selected_theme"] = clicked_theme
        st.session_state["rot_selected_theme_select"] = clicked_theme

    # ── 선택 테마 종목 Drill-down ───────────────────────────────────────────
    st.markdown("---")
    st.subheader("🎯 선택 테마 종목 분석")
    st.caption("테마를 선택하면 구성 종목의 자금 흐름·집중도·확산도와 종목 로테이션을 확인할 수 있습니다.")

    leading_default = rot_df[rot_df["상태"] == "LEADING"].sort_values("거래대금", ascending=False)
    if not leading_default.empty:
        default_theme = str(leading_default.iloc[0]["테마"])
    else:
        default_theme = str(rot_df.sort_values("거래대금", ascending=False).iloc[0]["테마"])

    if st.session_state.get("rot_selected_theme") not in visible_themes:
        st.session_state["rot_selected_theme"] = default_theme
    if st.session_state.get("rot_selected_theme_select") not in visible_themes:
        st.session_state["rot_selected_theme_select"] = st.session_state["rot_selected_theme"]

    sel1, sel2 = st.columns([3, 1])
    with sel1:
        selected_theme = st.selectbox(
            "테마 선택", visible_themes,
            index=visible_themes.index(st.session_state["rot_selected_theme"]),
            key="rot_selected_theme_select")
    # selectbox 변경값을 session에도 동기화
    st.session_state["rot_selected_theme"] = selected_theme
    with sel2:
        stock_count_label = st.selectbox(
            "종목 표시 개수", list(STOCK_COUNT_OPTIONS.keys()), index=1, key="rot_stock_count")
    stock_count = STOCK_COUNT_OPTIONS[stock_count_label]

    theme_stocks = stock_df[stock_df["테마"] == selected_theme].copy() if not stock_df.empty else pd.DataFrame()
    if theme_stocks.empty:
        st.info("선택한 테마의 종목별 이력 데이터가 없습니다. 분석 유니버스 또는 테마 매핑을 확인하세요.")
    else:
        # 테마 내 비중은 현재 stock_df 기준으로 한 번 더 정규화해 TOP3/집중도를 안정적으로 계산
        recalculated = calculate_stock_theme_share(stock_df, selected_theme)
        if not recalculated.empty:
            theme_stocks.loc[recalculated.index, "테마내비중"] = recalculated.values

        theme_stocks = theme_stocks.sort_values("거래대금", ascending=False).reset_index(drop=True)
        ts = create_theme_stock_summary(theme_stocks, selected_theme)

        # 선택 테마 상태 배지
        tr = rot_df[rot_df["테마"] == selected_theme]
        if not tr.empty:
            tr0 = tr.iloc[0]
            early_badge = " · 🔥 Early Rotation" if bool(tr0["EarlyRotation"]) else ""
            st.markdown(
                f"### {selected_theme}  ·  **{tr0['상태라벨']}**{early_badge}  "
                f"<span style='font-size:0.85em;color:#9aa4b2'>거래대금 모멘텀 {tr0['Y']:+.1f}%</span>",
                unsafe_allow_html=True)

        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("테마 거래대금", fmt_eok(ts["theme_tv"]))
        m2.metric("TOP1 종목", ts["top1_name"], f"비중 {ts['top1_share']:.1f}%")
        m3.metric("TOP3 집중도", f"{ts['top3_share']:.1f}%", f"TOP5 {ts['top5_share']:.1f}%")
        m4.metric("자금 확산도 (Breadth)", f"{ts['breadth']:.0f}%", ts["breadth_label"])
        m5.metric("자금유입 1위", ts["inflow_name"])

        st.markdown(
            f'<div class="sm-ai">💡 <b>테마 분석 코멘트</b><br>{ts["comment"]}</div>',
            unsafe_allow_html=True)
        st.caption("※ 종목·테마 거래대금은 동일한 가격이력 기준일로 계산합니다. 네이버 장중 누적 거래대금은 현재가 확인용 보조 데이터입니다.")

        st.plotly_chart(
            create_stock_rotation_chart(
                theme_stocks, result.get("stock_tails", {}), selected_theme,
                momentum_days, stock_count),
            use_container_width=True, key="rot_stock_chart")

        # Early Rotation 후보를 별도 노출
        early_stocks = theme_stocks[theme_stocks["EarlyRotation"]].sort_values("Y", ascending=False)
        if not early_stocks.empty:
            st.success("🔥 Early Rotation 후보: " + " · ".join(early_stocks["종목명"].head(5).tolist()))

        st.subheader(f"📋 {selected_theme} 종목 자금 흐름")
        stock_quick = st.radio(
            "종목 빠른 필터", ["전체", "자금유입 TOP", "자금이탈 TOP", "거래대금 TOP", "Early Rotation"],
            horizontal=True, key="rot_stock_quick")
        sv = theme_stocks.copy()
        if stock_quick == "자금유입 TOP":
            sv = sv.sort_values("Y", ascending=False).head(10)
        elif stock_quick == "자금이탈 TOP":
            sv = sv.sort_values("Y").head(10)
        elif stock_quick == "거래대금 TOP":
            sv = sv.sort_values("거래대금", ascending=False).head(10)
        elif stock_quick == "Early Rotation":
            sv = sv[sv["EarlyRotation"]].sort_values("Y", ascending=False)
        else:
            sv = sv.sort_values("Y", ascending=False)

        stock_table = make_stock_ranking_table(sv)
        st.dataframe(
            stock_table, use_container_width=True, hide_index=True,
            column_config={
                "현재가": st.column_config.NumberColumn("현재가", format="%.0f원"),
                "거래대금(억)": st.column_config.NumberColumn("거래대금(억)", format="%.0f"),
                "테마내비중(%)": st.column_config.NumberColumn("테마내비중(%)", format="%.1f%%"),
                "거래대금변화율(%)": st.column_config.NumberColumn("거래대금변화율(%)", format="%+.1f%%"),
                "20일평균대비(%)": st.column_config.NumberColumn("20일평균대비(%)", format="%+.1f%%"),
                "60일Percentile": st.column_config.ProgressColumn(
                    "60일Percentile", min_value=0, max_value=100, format="%.0f"),
                "1일등락(%)": st.column_config.NumberColumn("1일등락(%)", format="%+.1f%%"),
                "5일등락(%)": st.column_config.NumberColumn("5일등락(%)", format="%+.1f%%"),
                "20일등락(%)": st.column_config.NumberColumn("20일등락(%)", format="%+.1f%%"),
            },
        )

    if result.get("excluded"):
        with st.expander(f"⚠️ 데이터 부족으로 제외된 테마 ({len(result['excluded'])}개)", expanded=False):
            st.caption(" · ".join(result["excluded"]))

    # ── 기존 테마 랭킹 유지 ────────────────────────────────────────────────
    st.markdown("---")
    st.subheader("📋 테마 자금 흐름 Ranking")
    quick = st.radio("빠른 필터", ["전체", "자금유입 TOP", "자금이탈 TOP", "거래대금 TOP", "부상테마"],
                     horizontal=True, key="rot_quick")
    view = rot_df.copy()
    if quick == "자금유입 TOP":
        view = view.sort_values("Y", ascending=False).head(10)
    elif quick == "자금이탈 TOP":
        view = view.sort_values("Y").head(10)
    elif quick == "거래대금 TOP":
        view = view.sort_values("거래대금", ascending=False).head(10)
    elif quick == "부상테마":
        view = view[(view["상태"] == "EMERGING") | (view["EarlyRotation"])].sort_values("Y", ascending=False)
    else:
        view = view.sort_values("Y", ascending=False)

    table = make_ranking_table(view)
    st.dataframe(
        table, use_container_width=True, hide_index=True,
        column_config={
            "거래대금(억)": st.column_config.NumberColumn("거래대금(억)", format="%.0f"),
            "비중(%)": st.column_config.NumberColumn("비중(%)", format="%.1f%%"),
            "변화율(%)": st.column_config.NumberColumn("변화율(%)", format="%+.1f%%"),
            "20일평균대비(%)": st.column_config.NumberColumn("20일평균대비(%)", format="%+.1f%%"),
            "60일Percentile": st.column_config.ProgressColumn("60일Percentile", min_value=0, max_value=100, format="%.0f"),
        },
    )

    as_of = result.get("as_of")
    try:
        as_of_txt = f"{pd.to_datetime(as_of):%Y-%m-%d}"
    except Exception:
        as_of_txt = str(as_of)
    st.caption(
        f"분석대상: {market} 거래대금 상위 {result['n_universe']}종목 · 테마 매핑: sectors.py 대표 섹터(중복 없음) · "
        f"기준일: {as_of_txt} · X축 {rel_window}D {x_method} · Y축 모멘텀 {momentum_days}D · "
        f"Tail {tail_days}D · 시세 10분 캐시 · 가격이력 60분 캐시 · 투자 참고용이며 수익을 보장하지 않습니다.")

