#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
📊 Smart Money Dashboard
KOSPI 전체 종목의 거래대금 · 외국인/기관 순매수 데이터를 종합해
시장 자금 흐름(Smart Money)을 시각화하는 전문 대시보드.

데이터 소스: 네이버 금융 (KRX가 클라우드 서버 IP를 차단하여 대체)
- 전 종목 시세/거래대금/시가총액: m.stock.naver.com API (정확)
- 시장 전체 투자자별 순매수(억원): finance.naver.com 일별 동향 (정확)
- 종목별 외국인/기관 순매수: 일별 순매수수량 × 종가 근사 (거래대금 상위 250종목)
- 52주 신고가/신저가: yfinance (거래대금 상위 200종목)
"""
import io
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import numpy as np
import pandas as pd
import requests
import streamlit as st

try:
    import plotly.express as px
    import plotly.graph_objects as go
    PLOTLY_OK = True
except ImportError:
    PLOTLY_OK = False

EOK = 1e8   # 억 원
MM = 1e6    # 백만 원

NAVER_HDR = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Referer": "https://m.stock.naver.com/",
}
TOP_FLOW_N = 250   # 종목별 수급을 계산할 거래대금 상위 종목 수

# ── 기간 정의 (거래일 기준) ───────────────────────────
PERIODS = {"일별": 1, "주별": 5, "월별": 22, "3개월": 66}

# ── 섹터(테마) 분류 키워드 ────────────────────────────
THEME_KEYWORDS = {
    "반도체":   ["하이닉스", "반도체", "한미반", "DB하이", "리노공업", "이오테크", "원익", "테스", "유진테크", "솔브레인", "동진쎄미", "주성엔지"],
    "AI":       ["AI", "에이아이", "네이버", "NAVER", "카카오", "더존비즈온", "루닛", "솔트룩스"],
    "전력":     ["ELECTRIC", "일렉트릭", "전력", "변압기", "효성중공업", "HD현대일렉", "제룡전기", "산일전기", "대한전선", "가온전선", "LS "],
    "원전":     ["원전", "두산에너빌리티", "한전기술", "한전KPS", "우리기술", "비에이치아이"],
    "방산":     ["한화에어로", "한화시스템", "LIG넥스원", "현대로템", "한국항공우주", "풍산", "휴니드", "방산"],
    "자동차":   ["현대차", "기아", "모비스", "현대위아", "만도", "HL만도", "에스엘", "한온시스템", "현대오토에버"],
    "조선":     ["조선", "HD현대중공업", "한화오션", "삼성중공업", "HD한국조선", "HD현대미포", "HD현대마린", "STX엔진"],
    "2차전지":  ["LG에너지솔루션", "삼성SDI", "에코프로", "포스코퓨처엠", "엘앤에프", "SK아이이테크", "코스모신소재", "배터리"],
    "금융":     ["금융", "은행", "증권", "보험", "지주", "카드", "캐피탈", "KB", "신한", "하나", "우리금융", "미래에셋", "삼성생명", "삼성화재", "한국금융"],
    "통신":     ["SK텔레콤", "KT", "LG유플러스", "통신"],
    "건설":     ["건설", "대우건설", "GS건설", "DL이앤씨", "현대건설", "HDC", "태영"],
    "바이오":   ["바이오", "제약", "셀트리온", "삼성바이오", "유한양행", "한미약품", "녹십자", "대웅", "종근당", "SK바이오", "HLB", "파마"],
    "엔터":     ["하이브", "에스엠", "JYP", "와이지", "엔터", "CJ ENM", "스튜디오드래곤"],
    "철강":     ["POSCO", "포스코", "철강", "현대제철", "동국제강", "세아", "고려아연", "풍산홀딩스"],
    "유통":     ["이마트", "롯데쇼핑", "신세계", "현대백화점", "GS리테일", "BGF리테일", "유통", "홈쇼핑"],
    "화장품":   ["아모레", "LG생활건강", "화장품", "코스맥스", "한국콜마", "애경산업", "토니모리", "클리오"],
}

def classify_sector(name: str) -> str:
    for theme, kws in THEME_KEYWORDS.items():
        for kw in kws:
            if kw in name:
                return theme
    return "기타"

# ── 숫자 파싱 ─────────────────────────────────────────
def _n(v) -> float:
    """'1,234' / '+1,234' / '-716,994' / 'N/A' → float"""
    try:
        s = str(v).replace(",", "").replace("+", "").replace("%", "").strip()
        if s in ("", "N/A", "-", "nan"):
            return 0.0
        return float(s)
    except Exception:
        return 0.0

# ═══════════════════════════════════════════════════════
# 데이터 로딩 (네이버 금융)
# ═══════════════════════════════════════════════════════
@st.cache_data(ttl=600, show_spinner=False)
def load_all_stocks() -> pd.DataFrame:
    """KOSPI 전 종목 시세 (JSON API, 페이지네이션)"""
    rows, page = [], 1
    while page <= 30:
        r = requests.get(
            f"https://m.stock.naver.com/api/stocks/marketValue/KOSPI"
            f"?page={page}&pageSize=100", headers=NAVER_HDR, timeout=15)
        data = r.json()
        stocks = data.get("stocks", [])
        if not stocks:
            break
        rows.extend(stocks)
        if page * 100 >= int(data.get("totalCount", 0)):
            break
        page += 1

    recs = []
    for s in rows:
        if s.get("stockEndType") != "stock":   # ETF/ETN/리츠 등 제외
            continue
        try:
            chg = _n(s.get("fluctuationsRatio"))
            code = s.get("compareToPreviousPrice", {}).get("code", "")
            if code in ("4", "5") and chg > 0:   # 하락인데 부호 없으면 보정
                chg = -chg
            recs.append({
                "종목코드": s["itemCode"],
                "종목명":   s["stockName"],
                "현재가":   _n(s["closePrice"]),
                "등락률":   chg,
                "거래량":   _n(s["accumulatedTradingVolume"]),
                "거래대금": _n(s["accumulatedTradingValue"]) * MM,   # 백만원 → 원
                "시가총액": _n(s["marketValue"]) * EOK,              # 억원 → 원
            })
        except Exception:
            continue
    df = pd.DataFrame(recs)
    if not df.empty:
        try:
            from sectors import attach_sectors
            df = attach_sectors(df)          # 대표 섹터 + 연관 테마 (업종 기반)
        except Exception:
            df["섹터"] = df["종목명"].map(classify_sector)
            df["테마"] = ""
    return df

def _fetch_trend_rows(code: str, days: int) -> list:
    """종목별 일별 투자자 동향 (최신순). days<=120"""
    url = f"https://m.stock.naver.com/api/stock/{code}/trend"
    rows = requests.get(url + "?pageSize=60", headers=NAVER_HDR, timeout=15).json()
    if days > 60 and rows:
        oldest = rows[-1]["bizdate"]
        more = requests.get(url + f"?bizdate={oldest}&pageSize=60",
                            headers=NAVER_HDR, timeout=15).json()
        rows = rows + more
    return rows[:days]

@st.cache_data(ttl=600, show_spinner=False)
def load_flows(codes: tuple, days: int) -> pd.DataFrame:
    """상위 종목의 기간 순매수 금액(근사: 일별 수량×종가 합산) + 외국인 보유율"""
    def one(code):
        try:
            rows = _fetch_trend_rows(code, days)
            frg = sum(_n(x["foreignerPureBuyQuant"]) * _n(x["closePrice"]) for x in rows)
            org = sum(_n(x["organPureBuyQuant"]) * _n(x["closePrice"]) for x in rows)
            ind = sum(_n(x["individualPureBuyQuant"]) * _n(x["closePrice"]) for x in rows)
            hold = _n(rows[0].get("foreignerHoldRatio")) if rows else np.nan
            return code, frg, org, ind, hold
        except Exception:
            return code, 0.0, 0.0, 0.0, np.nan

    with ThreadPoolExecutor(max_workers=12) as ex:
        res = list(ex.map(one, codes))
    return (pd.DataFrame(res, columns=["종목코드", "외국인순매수", "기관순매수",
                                       "개인순매수", "외국인보유율"])
            .set_index("종목코드"))

@st.cache_data(ttl=600, show_spinner=False)
def load_market_trend(days: int) -> pd.DataFrame:
    """시장 전체 투자자별 일별 순매수 (억원) — finance.naver.com 스크레이핑"""
    collected = {}
    bizdate = datetime.now().strftime("%Y%m%d")
    for _ in range(days // 10 + 3):
        r = requests.get(
            f"https://finance.naver.com/sise/investorDealTrendDay.naver"
            f"?bizdate={bizdate}&sosok=01", headers=NAVER_HDR, timeout=15)
        r.encoding = "euc-kr"
        try:
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
        oldest = min(collected)                       # 'YY.MM.DD'
        y, m, dd = oldest.split(".")
        prev = pd.Timestamp(2000 + int(y), int(m), int(dd)) - pd.Timedelta(days=1)
        bizdate = prev.strftime("%Y%m%d")

    if not collected:
        return pd.DataFrame()
    df = pd.DataFrame(
        [(pd.Timestamp(2000 + int(k[:2]), int(k[3:5]), int(k[6:8])), *v)
         for k, v in collected.items()],
        columns=["날짜", "개인", "외국인", "기관"]).sort_values("날짜")
    return df.tail(days).set_index("날짜")

@st.cache_data(ttl=600, show_spinner=False)
def load_index_value(days: int) -> pd.DataFrame:
    """KOSPI 지수 일별 거래대금(백만원) — 최대 20페이지"""
    frames = []
    pages = min(days // 6 + 2, 20)
    for p in range(1, pages + 1):
        r = requests.get(
            f"https://finance.naver.com/sise/sise_index_day.naver?code=KOSPI&page={p}",
            headers=NAVER_HDR, timeout=15)
        r.encoding = "euc-kr"
        try:
            t = pd.read_html(io.StringIO(r.text))[0].dropna()
            frames.append(t)
        except Exception:
            break
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df.columns = ["날짜", "체결가", "전일비", "등락률", "거래량", "거래대금"]
    df["날짜"] = pd.to_datetime(df["날짜"], format="%Y.%m.%d", errors="coerce")
    df = df.dropna(subset=["날짜"]).drop_duplicates("날짜").sort_values("날짜")
    df["거래대금"] = pd.to_numeric(df["거래대금"], errors="coerce") * MM   # 백만원 → 원
    return df.set_index("날짜")

@st.cache_data(ttl=3600, show_spinner=False)
def load_52w_flags(tickers: tuple):
    """상위 종목의 52주 신고가/신저가 (yfinance 배치)"""
    try:
        import yfinance as yf
        symbols = [t + ".KS" for t in tickers]
        raw = yf.download(symbols, period="1y", interval="1d",
                          progress=False, auto_adjust=True, group_by="ticker")
        hi, lo = {}, {}
        for t, s in zip(tickers, symbols):
            try:
                sub = raw[s] if isinstance(raw.columns, pd.MultiIndex) else raw
                close = float(sub["Close"].dropna().iloc[-1])
                hi[t] = close >= float(sub["High"].max()) * 0.995
                lo[t] = close <= float(sub["Low"].min()) * 1.005
            except Exception:
                hi[t], lo[t] = False, False
        return hi, lo
    except Exception:
        return {}, {}

@st.cache_data(ttl=600, show_spinner=False)
def load_ticker_detail(code: str, days: int):
    """종목 상세: 일별 투자자 동향(근사금액) + yfinance 가격/거래대금"""
    rows = _fetch_trend_rows(code, days)
    trend = pd.DataFrame([{
        "날짜": pd.to_datetime(x["bizdate"]),
        "외국인": _n(x["foreignerPureBuyQuant"]) * _n(x["closePrice"]),
        "기관":   _n(x["organPureBuyQuant"]) * _n(x["closePrice"]),
        "개인":   _n(x["individualPureBuyQuant"]) * _n(x["closePrice"]),
        "종가":   _n(x["closePrice"]),
    } for x in rows]).sort_values("날짜").set_index("날짜")

    price = pd.DataFrame()
    try:
        import yfinance as yf
        raw = yf.download(code + ".KS", period="1y", interval="1d",
                          progress=False, auto_adjust=True)
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        price = raw.tail(days)
    except Exception:
        pass
    return trend, price

# ── Smart Money Score ─────────────────────────────────
def add_smart_score(df: pd.DataFrame) -> pd.DataFrame:
    p_amt = df["거래대금"].rank(pct=True) * 100
    p_chg = df["등락률"].rank(pct=True) * 100
    p_frg = df["외국인순매수"].rank(pct=True) * 100
    p_ins = df["기관순매수"].rank(pct=True) * 100
    base = 0.30 * p_amt + 0.10 * p_chg + 0.25 * p_frg + 0.25 * p_ins
    bonus = df["52주신고가"].astype(int) * 10
    df["Smart Score"] = (base + bonus).clip(0, 100).round(1)
    df["등급"] = df["Smart Score"].map(score_stars)
    return df

def score_stars(s: float) -> str:
    if s >= 90: return "★★★★★"
    if s >= 80: return "★★★★☆"
    if s >= 70: return "★★★☆☆"
    if s >= 60: return "★★☆☆☆"
    return "★☆☆☆☆"

# ── 포맷 헬퍼 ─────────────────────────────────────────
def fmt_eok(v):
    if abs(v) >= 1e12:
        return f"{v/1e12:,.1f}조"
    return f"{v/EOK:,.0f}억"

# ── AI 시장 분석 (규칙 기반 자동 생성) ────────────────
def build_ai_summary(df: pd.DataFrame, sec: pd.DataFrame,
                     mkt_frg: float, mkt_ins: float) -> str:
    both = df[(df["외국인순매수"] > 0) & (df["기관순매수"] > 0)].copy()
    both["합산"] = both["외국인순매수"] + both["기관순매수"]
    top_stocks = both.nlargest(3, "합산")["종목명"].tolist()

    sec2 = sec.sort_values("외국인+기관", ascending=False)
    top_sec = sec2.head(2).index.tolist()
    worst = sec2.tail(1)
    worst_name = worst.index[0]
    worst_amt_down = worst["평균등락률"].iloc[0] < 0

    frg_txt = "순매수" if mkt_frg > 0 else "순매도"
    ins_txt = "순매수" if mkt_ins > 0 else "순매도"

    msg = (
        f"이번 기간 시장에서는 **{' · '.join(top_sec)}** 섹터에 기관·외국인 자금이 집중되었습니다. "
        f"외국인은 시장 전체 {fmt_eok(abs(mkt_frg))} {frg_txt}, "
        f"기관은 {fmt_eok(abs(mkt_ins))} {ins_txt}를 기록했습니다. "
    )
    if top_stocks:
        msg += f"특히 **{', '.join(top_stocks)}** 종목으로 강한 동반 자금 유입이 확인됩니다. "
    msg += (
        f"반면 **{worst_name}** 섹터는 "
        + ("평균 등락률이 마이너스를 보이며 차익실현 압력이 나타나고 있습니다."
           if worst_amt_down else "상대적으로 자금 유입 강도가 약합니다.")
    )
    return msg

# ── 스타일 ────────────────────────────────────────────
DARK_CSS = """
<style>
[data-testid="stMetric"] {
    background: linear-gradient(135deg, #0e1117 0%, #1a1f2e 100%);
    border: 1px solid #2a3f5f; border-radius: 8px; padding: 12px;
}
[data-testid="stMetricLabel"] { color: #f0a500 !important; font-size: 0.8rem; }
[data-testid="stMetricValue"] { color: #e8e8e8 !important; }
.sm-title { color: #f0a500; font-family: monospace; letter-spacing: 1px; }
.sm-ai { background: #101720; border-left: 4px solid #f0a500;
         padding: 14px 18px; border-radius: 6px; color: #d8dee9; line-height: 1.7; }
</style>
"""
PLOTLY_TMPL = "plotly_dark"

def style_table(d: pd.DataFrame):
    money_cols = [c for c in d.columns if any(k in c for k in ["거래대금", "순매수", "합계", "시가총액"])]
    cfg = {c: st.column_config.NumberColumn(c + "(억)", format="%.0f") for c in money_cols}
    if "등락률" in d.columns:
        cfg["등락률"] = st.column_config.NumberColumn("등락률(%)", format="%.2f%%")
    if "Smart Score" in d.columns:
        cfg["Smart Score"] = st.column_config.ProgressColumn("Smart Score", min_value=0, max_value=100, format="%.0f")
    return cfg

def eokify(d: pd.DataFrame) -> pd.DataFrame:
    d = d.copy()
    for c in d.columns:
        if any(k in c for k in ["거래대금", "순매수", "합계", "시가총액"]) and "증가율" not in c:
            d[c] = (d[c] / EOK).round(1)
    return d

# ═══════════════════════════════════════════════════════
# 메인 렌더링
# ═══════════════════════════════════════════════════════
def render():
    st.markdown(DARK_CSS, unsafe_allow_html=True)
    st.markdown('<h2 class="sm-title">📊 SMART MONEY DASHBOARD</h2>', unsafe_allow_html=True)
    st.caption("KOSPI 전 종목 · 거래대금 × 외국인 × 기관 자금 흐름 종합 분석  |  데이터: 네이버 금융")

    if not PLOTLY_OK:
        st.error("plotly 패키지가 없습니다. requirements.txt를 확인하세요.")
        return

    # ── 상단 컨트롤 ──
    c1, c2, c3 = st.columns([3, 2, 2])
    with c1:
        period = st.radio("기간", list(PERIODS.keys()), horizontal=True, index=0, key="sm_period")
    with c2:
        refresh = st.selectbox("자동 새로고침", ["끄기", "5분", "10분", "30분"], key="sm_refresh")
    with c3:
        if st.button("🔄 데이터 갱신", use_container_width=True):
            st.cache_data.clear()
            st.rerun()
    if refresh != "끄기":
        sec_map = {"5분": 300, "10분": 600, "30분": 1800}
        st.markdown(f'<meta http-equiv="refresh" content="{sec_map[refresh]}">', unsafe_allow_html=True)

    n = PERIODS[period]

    # ── 데이터 로딩 ──
    try:
        with st.spinner("전 종목 시세 로딩 중..."):
            df = load_all_stocks()
        if df.empty:
            st.error("시세 데이터를 불러오지 못했습니다. 잠시 후 '데이터 갱신'으로 재시도하세요.")
            return
        top_codes = tuple(df.nlargest(TOP_FLOW_N, "거래대금")["종목코드"].tolist())
        with st.spinner(f"외국인·기관 수급 로딩 중... (상위 {TOP_FLOW_N}종목 × {n}일)"):
            flows = load_flows(top_codes, n)
        with st.spinner("시장 수급 동향 로딩 중..."):
            mtrend = load_market_trend(max(n, 20))
            idxval = load_index_value(min(2 * n, 60))
    except Exception as e:
        st.error(f"데이터 로딩 실패: {e}")
        st.info("잠시 후 '데이터 갱신' 버튼으로 재시도하세요.")
        return

    df = df.merge(flows, left_on="종목코드", right_index=True, how="left")
    for c in ["외국인순매수", "기관순매수", "개인순매수"]:
        df[c] = df[c].fillna(0)

    # 52주 신고가/신저가 — 거래대금 상위 200개 종목 대상
    top200 = tuple(df.nlargest(200, "거래대금")["종목코드"].tolist())
    hi, lo = load_52w_flags(top200)
    df["52주신고가"] = df["종목코드"].map(hi).fillna(False).astype(bool)
    df["52주신저가"] = df["종목코드"].map(lo).fillna(False).astype(bool)

    df = add_smart_score(df)

    # ── ⑮ 실시간 필터 ──
    with st.expander("🎛️ 실시간 필터", expanded=False):
        f1, f2, f3, f4 = st.columns(4)
        amt_min = f1.selectbox("거래대금", ["전체", "50억 이상", "100억 이상", "300억 이상", "500억 이상", "1000억 이상"])
        frg_min = f2.selectbox("외국인 순매수", ["전체", "10억 이상", "30억 이상", "50억 이상", "100억 이상"])
        ins_min = f3.selectbox("기관 순매수", ["전체", "10억 이상", "30억 이상", "50억 이상", "100억 이상"])
        chg_min = f4.selectbox("등락률", ["전체", "3% 이상", "5% 이상", "10% 이상", "15% 이상"])
        f5, f6 = st.columns(2)
        sec_pickf = f5.multiselect("대표 섹터", sorted(df["섹터"].dropna().unique()), key="flt_sec")
        theme_q = f6.text_input("연관 테마 검색", key="flt_theme",
                                placeholder="예: HBM, 수소, 우주항공")

    def th(s):
        return 0 if s == "전체" else float(s.split("억")[0].replace("% 이상", ""))
    fdf = df.copy()
    if amt_min != "전체": fdf = fdf[fdf["거래대금"] >= th(amt_min) * EOK]
    if frg_min != "전체": fdf = fdf[fdf["외국인순매수"] >= th(frg_min) * EOK]
    if ins_min != "전체": fdf = fdf[fdf["기관순매수"] >= th(ins_min) * EOK]
    if chg_min != "전체": fdf = fdf[fdf["등락률"] >= float(chg_min.split("%")[0])]
    if sec_pickf: fdf = fdf[fdf["섹터"].isin(sec_pickf)]
    if theme_q:
        q_ = theme_q.strip()
        fdf = fdf[fdf["테마"].str.contains(q_, na=False) | fdf["섹터"].str.contains(q_, na=False)]

    # ── 시장 전체 순매수 (정확치, 억원 → 원) ──
    if not mtrend.empty:
        recent = mtrend.tail(n)
        mkt_frg = recent["외국인"].sum() * EOK
        mkt_ins = recent["기관"].sum() * EOK
        mkt_ind = recent["개인"].sum() * EOK
    else:
        mkt_frg = df["외국인순매수"].sum()
        mkt_ins = df["기관순매수"].sum()
        mkt_ind = df["개인순매수"].sum()

    # 거래대금 증가율 (지수 일별 거래대금 기준)
    growth_txt = "—"
    if not idxval.empty and len(idxval) >= 2 * n:
        cur = idxval["거래대금"].tail(n).sum()
        prv = idxval["거래대금"].tail(2 * n).head(n).sum()
        if prv > 0:
            growth_txt = f"{(cur / prv - 1) * 100:+.1f}%"

    # ── ① 시장 요약 KPI ──
    k = st.columns(5)
    k[0].metric("전체 거래대금(당일)", fmt_eok(df["거래대금"].sum()))
    k[1].metric("외국인 순매수", fmt_eok(mkt_frg))
    k[2].metric("기관 순매수", fmt_eok(mkt_ins))
    k[3].metric("개인 순매수", fmt_eok(mkt_ind))
    k[4].metric("거래대금 증가율", growth_txt)
    k2 = st.columns(5)
    k2[0].metric("상승 종목", f"{(df['등락률'] > 0).sum()}개")
    k2[1].metric("하락 종목", f"{(df['등락률'] < 0).sum()}개")
    k2[2].metric("52주 신고가", f"{int(df['52주신고가'].sum())}개")
    k2[3].metric("52주 신저가", f"{int(df['52주신저가'].sum())}개")
    k2[4].metric("분석 종목 수", f"{len(df)}개")

    # 섹터 집계 (여러 섹션에서 공용)
    sec = df.groupby("섹터").agg(
        거래대금=("거래대금", "sum"),
        외국인순매수=("외국인순매수", "sum"),
        기관순매수=("기관순매수", "sum"),
        평균등락률=("등락률", "mean"),
        상승비율=("등락률", lambda s: (s > 0).mean() * 100),
        평균Score=("Smart Score", "mean"),
    )
    sec["외국인+기관"] = sec["외국인순매수"] + sec["기관순매수"]

    # ── ⑭ AI 시장 분석 ──
    st.markdown(
        f'<div class="sm-ai">🤖 <b>AI 시장 분석</b><br>'
        f'{build_ai_summary(df, sec, mkt_frg, mkt_ins)}</div>',
        unsafe_allow_html=True)
    st.markdown("")

    # ── 내부 탭 구성 ──
    t_rank, t_chart, t_trend, t_detail, t_fav = st.tabs(
        ["🏆 자금 랭킹", "📈 자금 지도", "📉 Smart Money Trend", "🔍 종목·섹터 상세", "⭐ 관심종목"])

    # ═══ 🏆 자금 랭킹 ═══
    with t_rank:
        st.subheader("💎 외국인 + 기관 동시 순매수")
        both = fdf[(fdf["외국인순매수"] > 0) & (fdf["기관순매수"] > 0)].copy()
        both["합계"] = both["외국인순매수"] + both["기관순매수"]
        both = both.sort_values("합계", ascending=False)
        cols6 = ["종목명", "외국인순매수", "기관순매수", "합계", "거래대금", "등락률", "섹터", "Smart Score", "등급"]
        d6 = eokify(both[cols6].head(100)).reset_index(drop=True)
        d6.index += 1
        st.dataframe(d6, use_container_width=True, column_config=style_table(both[cols6]))

        st.subheader("🧠 Smart Money Score TOP100")
        cols2 = ["종목명", "Smart Score", "등급", "거래대금", "외국인순매수", "기관순매수", "등락률", "52주신고가", "섹터"]
        d2 = eokify(fdf.sort_values("Smart Score", ascending=False)[cols2].head(100)).reset_index(drop=True)
        d2.index += 1
        st.dataframe(d2, use_container_width=True, column_config=style_table(fdf[cols2]))

        c_a, c_f, c_i = st.tabs(["💰 거래대금 TOP100", "🌍 외국인 순매수 TOP100", "🏛️ 기관 순매수 TOP100"])
        with c_a:
            sort_key = st.selectbox("정렬 기준", ["거래대금", "외국인순매수", "기관순매수", "Smart Score"], key="sort3")
            cols3 = ["종목명", "거래대금", "등락률", "외국인순매수", "기관순매수", "Smart Score", "섹터"]
            d3 = eokify(fdf.sort_values(sort_key, ascending=False)[cols3].head(100)).reset_index(drop=True)
            d3.index += 1
            st.dataframe(d3, use_container_width=True, column_config=style_table(fdf[cols3]))
        with c_f:
            cols4 = ["종목명", "외국인순매수", "거래대금", "등락률", "기관순매수", "섹터"]
            d4 = eokify(fdf.sort_values("외국인순매수", ascending=False)[cols4].head(100)).reset_index(drop=True)
            d4.index += 1
            st.dataframe(d4, use_container_width=True, column_config=style_table(fdf[cols4]))
        with c_i:
            cols5 = ["종목명", "기관순매수", "거래대금", "등락률", "외국인순매수", "섹터"]
            d5 = eokify(fdf.sort_values("기관순매수", ascending=False)[cols5].head(100)).reset_index(drop=True)
            d5.index += 1
            st.dataframe(d5, use_container_width=True, column_config=style_table(fdf[cols5]))
        st.caption(f"※ 종목별 외국인/기관 순매수는 거래대금 상위 {TOP_FLOW_N}종목 대상 · 일별 순매수수량 × 종가 근사치")

    # ═══ 📈 자금 지도 ═══
    with t_chart:
        st.subheader("🎯 외국인 vs 기관 순매수")
        sc = fdf.nlargest(300, "거래대금").copy()
        for c in ["외국인순매수", "기관순매수", "거래대금"]:
            sc[c + "_억"] = sc[c] / EOK
        fig = px.scatter(
            sc, x="외국인순매수_억", y="기관순매수_억", size=sc["거래대금_억"].clip(lower=1),
            color="섹터", hover_name="종목명",
            hover_data={"등락률": ":.2f", "Smart Score": True, "거래대금_억": ":,.0f"},
            template=PLOTLY_TMPL, height=560,
            labels={"외국인순매수_억": "외국인 순매수(억)", "기관순매수_억": "기관 순매수(억)"})
        fig.add_hline(y=0, line_dash="dot", line_color="gray")
        fig.add_vline(x=0, line_dash="dot", line_color="gray")
        fig.add_annotation(x=0.98, y=0.98, xref="paper", yref="paper", text="🔥 강한 매수",
                           showarrow=False, font=dict(color="#00e676", size=14))
        fig.add_annotation(x=0.02, y=0.02, xref="paper", yref="paper", text="🧊 강한 매도",
                           showarrow=False, font=dict(color="#ff5252", size=14))
        st.plotly_chart(fig, use_container_width=True)

        st.subheader("🗺️ Money Flow HeatMap (Treemap)")
        tm = fdf.nlargest(300, "거래대금").copy()
        tm["거래대금_억"] = tm["거래대금"] / EOK
        tm["외국인_억"] = tm["외국인순매수"] / EOK
        fig_tm = px.treemap(
            tm, path=["섹터", "종목명"], values="거래대금_억",
            color="등락률", color_continuous_scale=["#d32f2f", "#1a1f2e", "#00c853"],
            color_continuous_midpoint=0, template=PLOTLY_TMPL, height=620,
            hover_data={"외국인_억": ":,.0f", "Smart Score": True})
        st.plotly_chart(fig_tm, use_container_width=True)
        st.caption("사각형 크기 = 거래대금 · 색상 = 등락률 · 섹터 클릭 시 Drill-down")

        st.subheader("🌡️ Sector Money Flow")
        hm_disp = pd.DataFrame({
            "거래대금(억)": sec["거래대금"] / EOK,
            "외국인(억)": sec["외국인순매수"] / EOK,
            "기관(억)": sec["기관순매수"] / EOK,
            "평균등락률(%)": sec["평균등락률"],
            "상승비율(%)": sec["상승비율"],
        })
        z = (hm_disp - hm_disp.mean()) / hm_disp.std().replace(0, 1)
        fig_hm = px.imshow(
            z.T, text_auto=False, aspect="auto", template=PLOTLY_TMPL,
            color_continuous_scale=["#d32f2f", "#1a1f2e", "#00c853"], height=420)
        fig_hm.update_traces(
            customdata=hm_disp.T.values,
            hovertemplate="섹터=%{x}<br>%{y}=%{customdata:,.1f}<extra></extra>")
        st.plotly_chart(fig_hm, use_container_width=True)

        st.subheader("🏅 Sector Ranking TOP20")
        rank = sec.sort_values("외국인+기관", ascending=False).head(20)
        st.dataframe(eokify(rank.reset_index()), use_container_width=True,
                     column_config=style_table(rank.reset_index()))
        sb = fdf.nlargest(200, "거래대금").copy()
        sb["거래대금_억"] = sb["거래대금"] / EOK
        fig_sb = px.sunburst(sb, path=["섹터", "종목명"], values="거래대금_억",
                             color="등락률", color_continuous_scale=["#d32f2f", "#1a1f2e", "#00c853"],
                             color_continuous_midpoint=0, template=PLOTLY_TMPL, height=560)
        st.plotly_chart(fig_sb, use_container_width=True)

    # ═══ 📉 Smart Money Trend ═══
    with t_trend:
        st.subheader("📉 Smart Money Trend — 시장 수급 추이")
        win = st.radio("기간(거래일)", [20, 60, 120], horizontal=True, key="trend_win")
        try:
            mt = load_market_trend(win)
            iv = load_index_value(win)
            if mt.empty:
                st.warning("시장 수급 데이터를 불러오지 못했습니다.")
            else:
                fig_tr = go.Figure()
                fig_tr.add_trace(go.Scatter(x=mt.index, y=mt["외국인"].cumsum(),
                                            name="외국인 누적(억)", line=dict(color="#00b0ff", width=2)))
                fig_tr.add_trace(go.Scatter(x=mt.index, y=mt["기관"].cumsum(),
                                            name="기관 누적(억)", line=dict(color="#f0a500", width=2)))
                fig_tr.add_trace(go.Scatter(x=mt.index, y=mt["개인"].cumsum(),
                                            name="개인 누적(억)", line=dict(color="#9e9e9e", width=1, dash="dot")))
                if not iv.empty:
                    fig_tr.add_trace(go.Bar(x=iv.index, y=iv["거래대금"] / EOK,
                                            name="시장 거래대금(억)", yaxis="y2", opacity=0.25,
                                            marker_color="#4caf50"))
                fig_tr.update_layout(
                    template=PLOTLY_TMPL, height=520, hovermode="x unified",
                    yaxis=dict(title="누적 순매수(억)"),
                    yaxis2=dict(title="거래대금(억)", overlaying="y", side="right", showgrid=False),
                    legend=dict(orientation="h", y=1.08))
                st.plotly_chart(fig_tr, use_container_width=True)
        except Exception as e:
            st.warning(f"추이 데이터 로딩 실패: {e}")

    # ═══ 🔍 종목·섹터 상세 ═══
    with t_detail:
        cA, cB = st.columns(2)
        with cA:
            st.subheader("🔍 종목 상세")
            pick = st.selectbox("종목 검색", sorted(df["종목명"].tolist()), key="stock_pick",
                                index=None, placeholder="종목명을 입력하세요")
            win2 = st.radio("조회 기간(거래일)", [20, 60, 120], horizontal=True, key="detail_win")
            if pick:
                code = df.loc[df["종목명"] == pick, "종목코드"].iloc[0]
                try:
                    trend, price = load_ticker_detail(code, win2)
                    fig_p = go.Figure()
                    if not price.empty:
                        fig_p.add_trace(go.Scatter(x=price.index, y=price["Close"], name="종가",
                                                   line=dict(color="#e8e8e8")))
                        if "Volume" in price.columns:
                            fig_p.add_trace(go.Bar(x=price.index,
                                                   y=price["Volume"] * price["Close"] / EOK,
                                                   name="거래대금(억, 근사)", yaxis="y2", opacity=0.3,
                                                   marker_color="#4caf50"))
                    elif not trend.empty:
                        fig_p.add_trace(go.Scatter(x=trend.index, y=trend["종가"], name="종가",
                                                   line=dict(color="#e8e8e8")))
                    fig_p.update_layout(template=PLOTLY_TMPL, height=340, hovermode="x unified",
                                        yaxis2=dict(overlaying="y", side="right", showgrid=False),
                                        legend=dict(orientation="h", y=1.12),
                                        title=f"{pick} — 주가 · 거래대금")
                    st.plotly_chart(fig_p, use_container_width=True)

                    if not trend.empty:
                        fig_f = go.Figure()
                        for col, cc in [("외국인", "#00b0ff"), ("기관", "#f0a500"), ("개인", "#9e9e9e")]:
                            fig_f.add_trace(go.Scatter(x=trend.index, y=trend[col].cumsum() / EOK,
                                                       name=f"{col} 누적", line=dict(color=cc)))
                        fig_f.update_layout(template=PLOTLY_TMPL, height=300, hovermode="x unified",
                                            title="투자자별 누적 순매수(억, 근사)",
                                            legend=dict(orientation="h", y=1.15))
                        st.plotly_chart(fig_f, use_container_width=True)

                    row = df[df["종목코드"] == code].iloc[0]
                    m = st.columns(4)
                    m[0].metric("현재가", f"{int(row['현재가']):,}원", f"{row['등락률']:.2f}%")
                    m[1].metric("Smart Score", f"{row['Smart Score']:.0f}", row["등급"])
                    m[2].metric("외국인 순매수", fmt_eok(row["외국인순매수"]))
                    m[3].metric("기관 순매수", fmt_eok(row["기관순매수"]))
                except Exception as e:
                    st.warning(f"종목 데이터 로딩 실패: {e}")

        with cB:
            st.subheader("🏭 섹터 상세")
            all_secs = sorted(df["섹터"].dropna().unique())
            sec_pick = st.selectbox("대표 섹터 선택", all_secs, key="sec_pick")
            theme_q2 = st.text_input("또는 연관 테마 검색", key="sec_theme_q",
                                     placeholder="예: HBM, SMR, 수소 (입력 시 테마 기준 조회)")
            sec_sort = st.radio("정렬", ["거래대금", "외국인순매수", "기관순매수", "Smart Score"],
                                horizontal=True, key="sec_sort")
            if theme_q2:
                tq = theme_q2.strip()
                sdf = df[df["테마"].str.contains(tq, na=False)
                         | (df["섹터"].str.contains(tq, na=False))]
            else:
                sdf = df[df["섹터"] == sec_pick]
            sdf = sdf.sort_values(sec_sort, ascending=False)
            cols_s = ["종목명", "테마", "현재가", "등락률", "거래대금", "외국인순매수", "기관순매수",
                      "외국인보유율", "Smart Score", "등급"]
            ds = eokify(sdf[cols_s]).reset_index(drop=True)
            ds.index += 1
            st.dataframe(ds, use_container_width=True, height=520,
                         column_config=style_table(sdf[cols_s]))

    # ═══ ⭐ 관심종목 ═══
    with t_fav:
        st.subheader("⭐ 관심종목")
        favs = st.multiselect("종목 추가", sorted(df["종목명"].tolist()),
                              default=st.session_state.get("sm_favs", []), key="sm_favs")
        if favs:
            fav_df = df[df["종목명"].isin(favs)]
            cols_f = ["종목명", "현재가", "등락률", "거래대금", "외국인순매수", "기관순매수",
                      "Smart Score", "등급", "섹터", "52주신고가"]
            st.dataframe(eokify(fav_df[cols_f]), use_container_width=True, hide_index=True,
                         column_config=style_table(fav_df[cols_f]))
            alert = fav_df[(fav_df["외국인순매수"] > 0) & (fav_df["기관순매수"] > 0)]
            if not alert.empty:
                st.success(f"🔔 알림: {', '.join(alert['종목명'])} — 외국인·기관 동시 순매수 발생")
        else:
            st.info("관심종목을 추가하면 수급 현황과 동시 순매수 알림을 확인할 수 있습니다.")

        st.markdown("---")
        dl_target = fdf if favs == [] else df[df["종목명"].isin(favs)]
        exp = eokify(dl_target)
        c_csv, c_xlsx = st.columns(2)
        c_csv.download_button(
            "📥 CSV 다운로드",
            exp.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig"),
            file_name=f"smart_money_{datetime.now():%Y%m%d_%H%M}.csv", mime="text/csv",
            use_container_width=True)
        buf = io.BytesIO()
        try:
            with pd.ExcelWriter(buf, engine="openpyxl") as w:
                exp.to_excel(w, index=False, sheet_name="SmartMoney")
            c_xlsx.download_button(
                "📥 Excel 다운로드", buf.getvalue(),
                file_name=f"smart_money_{datetime.now():%Y%m%d_%H%M}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True)
        except Exception:
            c_xlsx.caption("Excel 다운로드는 openpyxl 설치 시 활성화됩니다.")

    st.caption(
        f"데이터: 네이버 금융 · 기간: {period} · "
        f"종목별 수급은 거래대금 상위 {TOP_FLOW_N}종목(수량×종가 근사) · "
        f"52주 신고가/신저가는 상위 200종목 기준 · 10분 캐시")
