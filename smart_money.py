#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
📊 Smart Money Dashboard
KOSPI 전체 종목의 거래대금 · 외국인/기관 순매수 데이터를 종합해
시장 자금 흐름(Smart Money)을 시각화하는 전문 대시보드.
데이터 소스: KRX (pykrx), 52주 신고가/신저가: yfinance
"""
import io
import os
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import streamlit as st

# KRX 데이터포털은 2025년부터 로그인 필수 → Streamlit Secrets에서 계정 주입
# (Streamlit Cloud: Settings > Secrets 에 KRX_ID / KRX_PW 등록)
try:
    if "KRX_ID" in st.secrets:
        os.environ["KRX_ID"] = str(st.secrets["KRX_ID"])
        os.environ["KRX_PW"] = str(st.secrets["KRX_PW"])
except Exception:
    pass

try:
    from pykrx import stock
    PYKRX_OK = True
except ImportError:
    PYKRX_OK = False

try:
    import plotly.express as px
    import plotly.graph_objects as go
    PLOTLY_OK = True
except ImportError:
    PLOTLY_OK = False

EOK = 1e8  # 억 원

# ── KRX 로그인 진단 ───────────────────────────────────
def krx_login_diagnosis():
    """KRX 로그인을 직접 시도해 (코드, 메시지) 반환. CD001=정상"""
    kid, kpw = os.environ.get("KRX_ID"), os.environ.get("KRX_PW")
    if not (kid and kpw):
        return "NOID", "KRX_ID/KRX_PW 미설정"
    try:
        import requests
        from pykrx.website.comm import auth as _a
        s = requests.Session()
        _a.warmup_krx_session(s)
        hdr = {"User-Agent": _a.USER_AGENT, "Referer": _a.LOGIN_PAGE}
        payload = {"mbrNm": "", "telNo": "", "di": "", "certType": "",
                   "mbrId": kid, "pw": kpw}
        d = s.post(_a.LOGIN_URL, data=payload, headers=hdr, timeout=15).json()
        code, msg = d.get("_error_code", ""), d.get("_error_message", "")
        if code == "CD011":  # 중복 로그인 → 재시도
            payload["skipDup"] = "Y"
            d = s.post(_a.LOGIN_URL, data=payload, headers=hdr, timeout=15).json()
            code, msg = d.get("_error_code", ""), d.get("_error_message", "")
        return code, msg
    except Exception as e:
        return "EXC", str(e)

def show_krx_help(code, msg):
    if code == "CD001":
        st.info("✅ KRX 로그인은 정상입니다. 일시적 데이터 오류일 수 있으니 '데이터 갱신'을 눌러 재시도하세요.")
    elif code == "NOID":
        st.warning(
            "⚠️ **KRX 계정 미설정** — Streamlit Cloud → Settings → Secrets에 "
            "`KRX_ID`, `KRX_PW`를 등록하세요.")
    elif code == "CD010":
        st.warning(
            "⚠️ **KRX 비밀번호 변경 필요** — [data.krx.co.kr](https://data.krx.co.kr)에서 "
            "직접 로그인해 비밀번호를 변경한 뒤, Secrets의 KRX_PW도 새 비밀번호로 갱신하세요.")
    else:
        st.warning(
            f"⚠️ **KRX 로그인 실패** (코드: {code or '없음'}) — {msg or '자격 증명을 확인하세요.'}\n\n"
            "확인 사항:\n"
            "1. KRX는 **이메일이 아닌 회원 아이디**로 로그인합니다. "
            "[data.krx.co.kr](https://data.krx.co.kr)에서 직접 로그인해 아이디/비밀번호를 확인하세요.\n"
            "2. 확인한 아이디/비밀번호를 Streamlit Cloud → Settings → **Secrets**의 "
            "`KRX_ID`, `KRX_PW`에 정확히 입력 후 저장하세요.\n"
            "3. 저장 후 앱이 재시작되면 다시 시도하세요.")

# ── 기간 정의 (거래일 기준) ───────────────────────────
PERIODS = {
    "일별": 1, "주별": 5, "월별": 22, "3개월": 66, "6개월": 126, "1년": 248,
}

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

# ── 데이터 로딩 (KRX) ─────────────────────────────────
@st.cache_data(ttl=600, show_spinner=False)
def load_trading_dates():
    """KOSPI 지수 OHLCV로 거래일 캘린더 + 시장 거래대금 추이 확보"""
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=800)).strftime("%Y%m%d")
    idx = stock.get_index_ohlcv(start, end, "1001")
    return idx

@st.cache_data(ttl=600, show_spinner=False)
def load_market_data(from_d: str, to_d: str, prev_from: str, prev_to: str):
    """기간별 가격변동 · 투자자별 순매수 · 시총 · 외국인 보유율"""
    chg = stock.get_market_price_change(from_d, to_d, market="KOSPI")
    prev = stock.get_market_price_change(prev_from, prev_to, market="KOSPI")

    frg = stock.get_market_net_purchases_of_equities(from_d, to_d, "KOSPI", "외국인")
    ins = stock.get_market_net_purchases_of_equities(from_d, to_d, "KOSPI", "기관합계")
    ind = stock.get_market_net_purchases_of_equities(from_d, to_d, "KOSPI", "개인")

    cap = stock.get_market_cap(to_d, market="KOSPI")
    try:
        fr = stock.get_exhaustion_rates_of_foreign_investment(to_d, "KOSPI")
    except Exception:
        fr = pd.DataFrame()

    df = chg.copy()
    df["전기간_거래대금"] = prev["거래대금"].reindex(df.index)
    df["외국인순매수"] = frg["순매수거래대금"].reindex(df.index).fillna(0)
    df["기관순매수"]   = ins["순매수거래대금"].reindex(df.index).fillna(0)
    df["개인순매수"]   = ind["순매수거래대금"].reindex(df.index).fillna(0)
    df["시가총액"]     = cap["시가총액"].reindex(df.index)
    if not fr.empty and "지분율" in fr.columns:
        df["외국인보유율"] = fr["지분율"].reindex(df.index)
    else:
        df["외국인보유율"] = np.nan

    df = df.reset_index().rename(columns={"티커": "종목코드", "index": "종목코드"})
    df["섹터"] = df["종목명"].map(classify_sector)
    df["거래대금증가율"] = np.where(
        df["전기간_거래대금"] > 0,
        (df["거래대금"] / df["전기간_거래대금"] - 1) * 100, 0.0)
    return df

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
def load_investor_trend(from_d: str, to_d: str):
    """시장 전체 투자자별 일별 순매수"""
    return stock.get_market_trading_value_by_date(from_d, to_d, "KOSPI")

@st.cache_data(ttl=600, show_spinner=False)
def load_ticker_detail(ticker: str, from_d: str, to_d: str):
    ohlcv = stock.get_market_ohlcv(from_d, to_d, ticker)
    flow = stock.get_market_trading_value_by_date(from_d, to_d, ticker)
    return ohlcv, flow

# ── Smart Money Score ─────────────────────────────────
def add_smart_score(df: pd.DataFrame) -> pd.DataFrame:
    p_amt  = df["거래대금"].rank(pct=True) * 100
    p_grw  = df["거래대금증가율"].rank(pct=True) * 100
    p_frg  = df["외국인순매수"].rank(pct=True) * 100
    p_ins  = df["기관순매수"].rank(pct=True) * 100
    base = 0.25 * p_amt + 0.15 * p_grw + 0.25 * p_frg + 0.25 * p_ins
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
def to_eok(v):  # 원 → 억
    return round(v / EOK, 1)

def fmt_eok(v):
    if abs(v) >= 1e12:
        return f"{v/1e12:,.1f}조"
    return f"{v/EOK:,.0f}억"

# ── AI 시장 분석 (규칙 기반 자동 생성) ────────────────
def build_ai_summary(df: pd.DataFrame, sec: pd.DataFrame) -> str:
    both = df[(df["외국인순매수"] > 0) & (df["기관순매수"] > 0)].copy()
    both["합산"] = both["외국인순매수"] + both["기관순매수"]
    top_stocks = both.nlargest(3, "합산")["종목명"].tolist()

    sec2 = sec.sort_values("외국인+기관", ascending=False)
    top_sec = sec2.head(2).index.tolist()
    worst = sec2.tail(1)
    worst_name = worst.index[0]
    worst_amt_down = worst["평균등락률"].iloc[0] < 0

    frg_tot, ins_tot = df["외국인순매수"].sum(), df["기관순매수"].sum()
    frg_txt = "순매수" if frg_tot > 0 else "순매도"
    ins_txt = "순매수" if ins_tot > 0 else "순매도"

    msg = (
        f"이번 기간 시장에서는 **{' · '.join(top_sec)}** 섹터에 기관·외국인 자금이 집중되었습니다. "
        f"외국인은 전체 {fmt_eok(abs(frg_tot))} {frg_txt}, 기관은 {fmt_eok(abs(ins_tot))} {ins_txt}를 기록했습니다. "
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
    st.caption("KOSPI 전 종목 · 거래대금 × 외국인 × 기관 자금 흐름 종합 분석  |  데이터: KRX")

    if not PYKRX_OK or not PLOTLY_OK:
        st.error("필수 패키지가 없습니다. `pip install pykrx plotly openpyxl` 후 다시 실행하세요.")
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
    with st.spinner("KRX 데이터 로딩 중..."):
        try:
            idx = load_trading_dates()
            dates = idx.index
            if len(dates) < 2 * n + 1:
                st.error("거래일 데이터가 부족합니다.")
                return
            to_d      = dates[-1].strftime("%Y%m%d")
            from_d    = dates[-n].strftime("%Y%m%d")
            prev_to   = dates[-n - 1].strftime("%Y%m%d")
            prev_from = dates[-2 * n].strftime("%Y%m%d")
            df = load_market_data(from_d, to_d, prev_from, prev_to)
        except Exception as e:
            st.error(f"KRX 데이터 로딩 실패: {e}")
            with st.spinner("KRX 로그인 상태 진단 중..."):
                code, msg = krx_login_diagnosis()
            show_krx_help(code, msg)
            return

    # 52주 신고가/신저가 — 거래대금 상위 200개 종목 대상
    top200 = tuple(df.nlargest(200, "거래대금")["종목코드"].tolist())
    hi, lo = load_52w_flags(top200)
    df["52주신고가"] = df["종목코드"].map(hi).fillna(False)
    df["52주신저가"] = df["종목코드"].map(lo).fillna(False)

    df = add_smart_score(df)
    df["현재가"] = df["종가"]

    # ── ⑮ 실시간 필터 ──
    with st.expander("🎛️ 실시간 필터", expanded=False):
        f1, f2, f3, f4 = st.columns(4)
        amt_min = f1.selectbox("거래대금", ["전체", "50억 이상", "100억 이상", "300억 이상", "500억 이상", "1000억 이상"])
        frg_min = f2.selectbox("외국인 순매수", ["전체", "10억 이상", "30억 이상", "50억 이상", "100억 이상"])
        ins_min = f3.selectbox("기관 순매수", ["전체", "10억 이상", "30억 이상", "50억 이상", "100억 이상"])
        chg_min = f4.selectbox("등락률", ["전체", "3% 이상", "5% 이상", "10% 이상", "15% 이상"])

    def th(s):
        return 0 if s == "전체" else float(s.split("억")[0].replace("% 이상", ""))
    fdf = df.copy()
    if amt_min != "전체": fdf = fdf[fdf["거래대금"] >= th(amt_min) * EOK]
    if frg_min != "전체": fdf = fdf[fdf["외국인순매수"] >= th(frg_min) * EOK]
    if ins_min != "전체": fdf = fdf[fdf["기관순매수"] >= th(ins_min) * EOK]
    if chg_min != "전체": fdf = fdf[fdf["등락률"] >= float(chg_min.split("%")[0])]

    # ── ① 시장 요약 KPI ──
    k = st.columns(5)
    k[0].metric("전체 거래대금", fmt_eok(df["거래대금"].sum()))
    k[1].metric("외국인 순매수", fmt_eok(df["외국인순매수"].sum()))
    k[2].metric("기관 순매수", fmt_eok(df["기관순매수"].sum()))
    k[3].metric("개인 순매수", fmt_eok(df["개인순매수"].sum()))
    tot_growth = (df["거래대금"].sum() / max(df["전기간_거래대금"].sum(), 1) - 1) * 100
    k[4].metric("거래대금 증가율", f"{tot_growth:+.1f}%")
    k2 = st.columns(5)
    k2[0].metric("상승 종목", f"{(df['등락률'] > 0).sum()}개")
    k2[1].metric("하락 종목", f"{(df['등락률'] < 0).sum()}개")
    k2[2].metric("52주 신고가", f"{df['52주신고가'].sum()}개")
    k2[3].metric("52주 신저가", f"{df['52주신저가'].sum()}개")
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
    st.markdown(f'<div class="sm-ai">🤖 <b>AI 시장 분석</b><br>{build_ai_summary(df, sec)}</div>',
                unsafe_allow_html=True)
    st.markdown("")

    # ── 내부 탭 구성 ──
    t_rank, t_chart, t_trend, t_detail, t_fav = st.tabs(
        ["🏆 자금 랭킹", "📈 자금 지도", "📉 Smart Money Trend", "🔍 종목·섹터 상세", "⭐ 관심종목"])

    # ═══ 🏆 자금 랭킹 ═══
    with t_rank:
        # ⑥ 동시 순매수 (가장 중요 → 최상단)
        st.subheader("💎 외국인 + 기관 동시 순매수")
        both = fdf[(fdf["외국인순매수"] > 0) & (fdf["기관순매수"] > 0)].copy()
        both["합계"] = both["외국인순매수"] + both["기관순매수"]
        both = both.sort_values("합계", ascending=False)
        cols6 = ["종목명", "외국인순매수", "기관순매수", "합계", "거래대금", "등락률", "섹터", "Smart Score", "등급"]
        d6 = eokify(both[cols6].head(100)).reset_index(drop=True)
        d6.index += 1
        st.dataframe(d6, use_container_width=True, column_config=style_table(both[cols6]))

        # ② Smart Money Score
        st.subheader("🧠 Smart Money Score TOP100")
        cols2 = ["종목명", "Smart Score", "등급", "거래대금", "거래대금증가율", "외국인순매수", "기관순매수", "등락률", "52주신고가", "섹터"]
        d2 = eokify(fdf.sort_values("Smart Score", ascending=False)[cols2].head(100)).reset_index(drop=True)
        d2.index += 1
        st.dataframe(d2, use_container_width=True, column_config=style_table(fdf[cols2]))

        # ③④⑤ TOP100
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

    # ═══ 📈 자금 지도 ═══
    with t_chart:
        # ⑦ 외국인 vs 기관 Scatter
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

        # ⑩ Money Flow Treemap
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

        # ⑧ Sector Money Flow HeatMap
        st.subheader("🌡️ Sector Money Flow")
        hm = sec.copy()
        hm_disp = pd.DataFrame({
            "거래대금(억)": hm["거래대금"] / EOK,
            "외국인(억)": hm["외국인순매수"] / EOK,
            "기관(억)": hm["기관순매수"] / EOK,
            "평균등락률(%)": hm["평균등락률"],
            "상승비율(%)": hm["상승비율"],
        })
        z = (hm_disp - hm_disp.mean()) / hm_disp.std().replace(0, 1)
        fig_hm = px.imshow(
            z.T, text_auto=False, aspect="auto", template=PLOTLY_TMPL,
            color_continuous_scale=["#d32f2f", "#1a1f2e", "#00c853"], height=420)
        fig_hm.update_traces(
            customdata=hm_disp.T.values,
            hovertemplate="섹터=%{x}<br>%{y}=%{customdata:,.1f}<extra></extra>")
        st.plotly_chart(fig_hm, use_container_width=True)

        # ⑨ Sector Ranking + Sunburst
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
            tf = dates[-win].strftime("%Y%m%d")
            flow = load_investor_trend(tf, to_d)
            idx_win = idx.loc[dates[-win]:]
            fig_tr = go.Figure()
            if "외국인합계" in flow.columns:
                fig_tr.add_trace(go.Scatter(x=flow.index, y=flow["외국인합계"].cumsum() / EOK,
                                            name="외국인 누적(억)", line=dict(color="#00b0ff", width=2)))
            if "기관합계" in flow.columns:
                fig_tr.add_trace(go.Scatter(x=flow.index, y=flow["기관합계"].cumsum() / EOK,
                                            name="기관 누적(억)", line=dict(color="#f0a500", width=2)))
            if "개인" in flow.columns:
                fig_tr.add_trace(go.Scatter(x=flow.index, y=flow["개인"].cumsum() / EOK,
                                            name="개인 누적(억)", line=dict(color="#9e9e9e", width=1, dash="dot")))
            if "거래대금" in idx_win.columns:
                fig_tr.add_trace(go.Bar(x=idx_win.index, y=idx_win["거래대금"] / EOK,
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
        # ⑫ 종목 상세
        with cA:
            st.subheader("🔍 종목 상세")
            pick = st.selectbox("종목 검색", sorted(df["종목명"].tolist()), key="stock_pick",
                                index=None, placeholder="종목명을 입력하세요")
            win2 = st.radio("조회 기간(거래일)", [20, 60, 120], horizontal=True, key="detail_win")
            if pick:
                code = df.loc[df["종목명"] == pick, "종목코드"].iloc[0]
                try:
                    d_from = dates[-win2].strftime("%Y%m%d")
                    ohlcv, flw = load_ticker_detail(code, d_from, to_d)
                    fig_p = go.Figure()
                    fig_p.add_trace(go.Scatter(x=ohlcv.index, y=ohlcv["종가"], name="종가",
                                               line=dict(color="#e8e8e8")))
                    fig_p.add_trace(go.Bar(x=ohlcv.index, y=ohlcv["거래대금"] / EOK,
                                           name="거래대금(억)", yaxis="y2", opacity=0.3,
                                           marker_color="#4caf50"))
                    fig_p.update_layout(template=PLOTLY_TMPL, height=340, hovermode="x unified",
                                        yaxis2=dict(overlaying="y", side="right", showgrid=False),
                                        legend=dict(orientation="h", y=1.12),
                                        title=f"{pick} — 주가 · 거래대금")
                    st.plotly_chart(fig_p, use_container_width=True)

                    fig_f = go.Figure()
                    for col, cname, cc in [("외국인합계", "외국인 누적", "#00b0ff"),
                                           ("기관합계", "기관 누적", "#f0a500"),
                                           ("개인", "개인 누적", "#9e9e9e")]:
                        if col in flw.columns:
                            fig_f.add_trace(go.Scatter(x=flw.index, y=flw[col].cumsum() / EOK,
                                                       name=cname, line=dict(color=cc)))
                    fig_f.update_layout(template=PLOTLY_TMPL, height=300, hovermode="x unified",
                                        title="투자자별 누적 순매수(억)",
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

        # ⑬ 섹터 상세
        with cB:
            st.subheader("🏭 섹터 상세")
            sec_pick = st.selectbox("섹터 선택", list(THEME_KEYWORDS.keys()) + ["기타"], key="sec_pick")
            sec_sort = st.radio("정렬", ["거래대금", "외국인순매수", "기관순매수", "Smart Score"],
                                horizontal=True, key="sec_sort")
            sdf = df[df["섹터"] == sec_pick].sort_values(sec_sort, ascending=False)
            cols_s = ["종목명", "현재가", "등락률", "거래대금", "외국인순매수", "기관순매수",
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
            # 간단 알림: 관심종목 중 동시 순매수 발생 시
            alert = fav_df[(fav_df["외국인순매수"] > 0) & (fav_df["기관순매수"] > 0)]
            if not alert.empty:
                st.success(f"🔔 알림: {', '.join(alert['종목명'])} — 외국인·기관 동시 순매수 발생")
        else:
            st.info("관심종목을 추가하면 수급 현황과 동시 순매수 알림을 확인할 수 있습니다.")

        # 다운로드
        st.markdown("---")
        dl_target = fdf if favs == [] else df[df["종목명"].isin(favs)]
        exp = eokify(dl_target.drop(columns=["전기간_거래대금"], errors="ignore"))
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

    st.caption(f"기준일: {to_d} · 기간: {period}({from_d}~{to_d}) · 52주 신고가/신저가는 거래대금 상위 200종목 기준")
