# -*- coding: utf-8 -*-
"""
KOSPI/KOSDAQ 전종목 레거시 테마 분류기

기준
- 네이버 구(레거시) 증권 '테마별 시세'의 인포스탁 테마 분류를 원본으로 사용
- 테마가 없는 종목은 네이버 업종 분류로 보완
- 우선주/종류주는 보통주 테마를 가능한 경우 승계
- 이벤트성 테마(신규상장/정치/정책/환율 등)는 별도 태그로 분리

출력
1) stock_theme_master.csv   : 종목당 1행, 주테마/부테마/대분류 포함
2) theme_membership.csv     : 테마-종목 long format (다대다)
3) theme_catalog.csv        : 레거시 테마 목록 및 구성종목 수

주의
- 공개 웹페이지 구조에 의존하므로 네이버 HTML 구조 변경 시 selector 보완이 필요할 수 있습니다.
- 서버 부하를 줄이기 위해 결과를 로컬 캐시하고, 기본적으로 24시간 내 캐시는 재사용합니다.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import parse_qs, urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://finance.naver.com"
THEME_LIST_URL = BASE + "/sise/theme.naver?&page={page}"
THEME_DETAIL_URL = BASE + "/sise/sise_group_detail.naver?type=theme&no={no}"
UPJONG_LIST_URL = BASE + "/sise/sise_group.naver?type=upjong"
UPJONG_DETAIL_URL = BASE + "/sise/sise_group_detail.naver?type=upjong&no={no}"
MARKET_URL = BASE + "/sise/sise_market_sum.naver?sosok={sosok}&page={page}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0 Safari/537.36"
    ),
    "Referer": BASE + "/",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.7,en;q=0.6",
}

# 구조적 테마가 이벤트성 테마보다 우선되도록 가중치 부여
STRUCTURAL_KEYWORDS = [
    "반도체", "HBM", "CXL", "소캠", "온디바이스 AI", "뉴로모픽", "유리 기판",
    "AI", "인공지능", "데이터센터", "클라우드", "냉각", "로봇", "스마트팩토리",
    "원자력", "원전", "SMR", "전력", "변압기", "스마트그리드", "핵융합",
    "2차전지", "리튬", "전고체", "나트륨이온", "전기차", "충전",
    "방위산업", "우주", "항공기부품", "드론", "조선", "조선기자재", "해운",
    "바이오", "제약", "의료기기", "비만", "치매", "mRNA", "오가노이드",
    "5G", "통신장비", "광통신", "양자", "사이버보안", "보안",
    "자동차", "자율주행", "전장", "OLED", "디스플레이", "LED",
    "건설", "GTX", "철도", "시멘트", "피팅", "밸브", "철강", "비철",
    "태양광", "풍력", "수소", "연료전지", "탄소", "신재생",
    "증권", "은행", "보험", "카드", "핀테크", "가상화폐",
    "게임", "엔터", "영화", "웹툰", "미디어", "광고",
    "화장품", "면세점", "백화점", "소매유통", "음식료", "농업", "여행",
]

EVENT_KEYWORDS = [
    "신규상장", "대선", "총선", "정치", "정책", "대통령", "당선", "공약",
    "밸류업", "환율", "수혜", "재개발 수혜", "지수(", "Korea Value-up",
    "코리아 밸류업", "지주회사", "품절주", "고배당", "재난", "코로나19",
]

# 대분류: 사용자가 로테이션 지도에서 쓰기 좋은 20개 안팎의 상위 버킷
BUCKET_RULES: List[Tuple[str, List[str]]] = [
    ("반도체·AI", ["반도체", "HBM", "CXL", "소캠", "SOCAMM", "유리 기판", "온디바이스", "뉴로모픽", "AI", "인공지능", "데이터센터", "냉각"]),
    ("전력·원전·에너지", ["원자력", "원전", "SMR", "핵융합", "전력", "변압기", "스마트그리드", "전선", "가스", "LNG", "셰일가스"]),
    ("2차전지·전기차", ["2차전지", "리튬", "전고체", "나트륨이온", "전기차", "충전", "폐배터리"]),
    ("로봇·자동화", ["로봇", "스마트팩토리", "공장자동화", "3D 프린터", "협동로봇"]),
    ("우주·방산·항공", ["방위산업", "전쟁", "테러", "우주", "스페이스X", "항공기부품", "드론", "위성"]),
    ("조선·해운", ["조선", "조선기자재", "해운", "선박", "LNG선"]),
    ("자동차·부품", ["자동차", "자율주행", "전장", "타이어", "자동차부품"]),
    ("바이오·헬스케어", ["바이오", "제약", "의료기기", "비만", "치매", "mRNA", "오가노이드", "탈모", "진단", "백신", "의료AI"]),
    ("인터넷·SW·클라우드", ["인터넷", "소프트웨어", "클라우드", "SaaS", "핀테크", "전자결제", "NFT", "블록체인"]),
    ("통신·네트워크", ["5G", "6G", "통신장비", "광통신", "광케이블", "NI(", "네트워크", "양자암호"]),
    ("디스플레이·전자부품", ["OLED", "디스플레이", "LED", "마이크로 LED", "플렉서블", "PCB", "MLCC", "카메라모듈", "스마트폰"]),
    ("건설·인프라", ["건설", "GTX", "철도", "터널", "도로", "수자원", "시멘트", "레미콘", "모듈러주택", "피팅", "밸브"]),
    ("철강·비철·소재", ["철강", "비철", "알루미늄", "구리", "희토류", "니켈", "시멘트", "소재"]),
    ("화학·정유", ["정유", "석유", "화학", "석유화학", "윤활유", "플라스틱"]),
    ("친환경·신재생", ["태양광", "풍력", "수소", "연료전지", "SOFC", "탄소", "신재생", "친환경", "폐기물"]),
    ("금융", ["증권", "은행", "생명보험", "손해보험", "보험", "카드", "창투사", "벤처캐피탈"]),
    ("소비·유통·패션", ["백화점", "면세점", "소매유통", "홈쇼핑", "화장품", "패션", "의류", "명품", "편의점"]),
    ("콘텐츠·엔터·게임", ["게임", "엔터", "영화", "웹툰", "미디어", "음원", "K-POP", "광고", "방송"]),
    ("음식료·농업", ["음식료", "식품", "주류", "농업", "비료", "사료", "수산", "육계"]),
    ("여행·레저·운송", ["여행", "항공/저가", "LCC", "카지노", "호텔", "레저", "택배", "물류", "고속버스"]),
    ("기타·이벤트", []),
]

# 업종 fallback을 대분류로 연결하는 보조 규칙
INDUSTRY_BUCKET_RULES: List[Tuple[str, List[str]]] = [
    ("반도체·AI", ["반도체"]),
    ("바이오·헬스케어", ["의약", "의료", "바이오"]),
    ("자동차·부품", ["자동차", "운송장비"]),
    ("조선·해운", ["조선", "해운"]),
    ("건설·인프라", ["건설", "건축", "토목"]),
    ("철강·비철·소재", ["철강", "금속", "비금속"]),
    ("화학·정유", ["화학", "석유"]),
    ("금융", ["은행", "증권", "보험", "금융"]),
    ("소비·유통·패션", ["유통", "섬유", "의류"]),
    ("콘텐츠·엔터·게임", ["오락", "문화", "방송", "출판"]),
    ("음식료·농업", ["음식료", "식품", "농업"]),
    ("인터넷·SW·클라우드", ["소프트웨어", "IT 서비스"]),
    ("통신·네트워크", ["통신"]),
    ("디스플레이·전자부품", ["전자부품", "전기전자"]),
    ("여행·레저·운송", ["운수", "창고", "항공"]),
]


@dataclass(frozen=True)
class ThemeInfo:
    theme_id: str
    theme_name: str


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=12, pool_maxsize=12))
    return s


def _get_html(url: str, timeout: int = 15) -> str:
    s = _session()
    r = s.get(url, timeout=timeout)
    r.raise_for_status()
    # finance.naver.com은 일부 페이지가 EUC-KR 계열로 내려올 수 있음
    if not r.encoding or r.encoding.lower() in {"iso-8859-1", "ascii"}:
        r.encoding = r.apparent_encoding or "euc-kr"
    # 차단/빈 응답 진단용 마지막 응답 정보 저장
    _LAST_RESPONSE.update({
        "url": url,
        "status": r.status_code,
        "final_url": r.url,
        "length": len(r.text),
        "head": re.sub(r"\s+", " ", r.text[:300]),
    })
    # 연속 요청 간 최소 간격 (봇 차단 완화)
    time.sleep(REQUEST_DELAY_SEC)
    return r.text


_LAST_RESPONSE: Dict[str, object] = {}
REQUEST_DELAY_SEC = float(os.environ.get("NAVER_REQUEST_DELAY", "0.3"))


def _diagnose(label: str) -> None:
    """수집 실패 시 원인(차단 vs 구조 변경) 판별용 로그."""
    print(f"[진단] {label}")
    for k in ("url", "status", "final_url", "length", "head"):
        print(f"  - {k}: {_LAST_RESPONSE.get(k)}")


def _extract_no(href: str, expected_type: str) -> Optional[str]:
    if not href:
        return None
    p = urlparse(urljoin(BASE, href))
    q = parse_qs(p.query)
    if q.get("type", [None])[0] != expected_type:
        return None
    return q.get("no", [None])[0]


def _extract_stock_code(href: str) -> Optional[str]:
    if not href:
        return None
    p = urlparse(urljoin(BASE, href))
    q = parse_qs(p.query)
    code = q.get("code", [None])[0]
    if code and re.fullmatch(r"\d{6}", code):
        return code
    return None


API_BASE = "https://m.stock.naver.com/api/stocks"
API_PAGE_SIZE = 100  # 네이버 API 최대 pageSize
# 기존 결과와 동일한 범위 유지: 주식 + ETF (ETN 제외)
STOCK_END_TYPES = {"stock", "etf"}


def _get_json(url: str, timeout: int = 15) -> dict:
    s = _session()
    r = s.get(url, timeout=timeout)
    r.raise_for_status()
    _LAST_RESPONSE.update({
        "url": url, "status": r.status_code, "final_url": r.url,
        "length": len(r.text), "head": re.sub(r"\s+", " ", r.text[:300]),
    })
    time.sleep(REQUEST_DELAY_SEC)
    try:
        return r.json()
    except ValueError:
        _diagnose("JSON 응답 아님")
        raise


def _paged(path: str, key: str, max_pages: int = 60) -> List[dict]:
    """네이버 증권 API 페이지네이션 공통 처리."""
    out: List[dict] = []
    for page in range(1, max_pages + 1):
        j = _get_json(f"{API_BASE}/{path}?page={page}&pageSize={API_PAGE_SIZE}")
        items = j.get(key) or []
        out.extend(items)
        total = int(j.get("totalCount") or 0)
        if not items or len(out) >= total:
            break
    return out


def get_all_stocks(max_pages: int = 60) -> pd.DataFrame:
    """네이버 증권 API(시가총액 순)로 KOSPI/KOSDAQ 전체 종목(우선주·ETF 포함) 수집.

    2026-10 네이버 구 시세 페이지(finance.naver.com/sise/*)가 stock.naver.com으로
    이관되어 HTML 스크래핑이 불가해짐 → 모바일 JSON API로 전환.
    """
    rows: Dict[str, Dict[str, str]] = {}
    for market in ("KOSPI", "KOSDAQ"):
        for it in _paged(f"marketValue/{market}", "stocks", max_pages):
            code = str(it.get("itemCode") or "")
            name = str(it.get("stockName") or "").strip()
            if it.get("stockEndType") not in STOCK_END_TYPES:
                continue
            if re.fullmatch(r"\d{6}|\d{5}[A-Z]|\d[0-9A-Z]{5}", code) and name and code not in rows:
                rows[code] = {"code": code, "name": name, "market": market}
    df = pd.DataFrame(rows.values())
    if df.empty:
        _diagnose("시가총액 API에서 종목 0건")
        raise RuntimeError("KOSPI/KOSDAQ 종목 목록을 수집하지 못했습니다. 네이버 API 응답을 확인하세요.")
    return df.sort_values(["market", "code"]).reset_index(drop=True)


def get_theme_catalog(max_pages: int = 20) -> List[ThemeInfo]:
    """레거시(인포스탁) 테마 목록 및 theme no 수집. 테마 번호는 신규 사이트에서도 동일."""
    themes: Dict[str, ThemeInfo] = {}
    for g in _paged("theme", "groups", max_pages):
        no, name = str(g.get("no") or ""), str(g.get("name") or "").strip()
        if no and name:
            themes[no] = ThemeInfo(no, name)
    if not themes:
        _diagnose("테마 목록 API에서 테마 0건")
        raise RuntimeError("레거시 테마 목록을 수집하지 못했습니다.")
    return list(themes.values())


def get_upjong_catalog() -> Dict[str, str]:
    result: Dict[str, str] = {}
    for g in _paged("industry", "groups", 10):
        no, name = str(g.get("no") or ""), str(g.get("name") or "").strip()
        if no and name:
            result[no] = name
    return result


def _members_from_group(kind: str, no: str) -> Set[Tuple[str, str]]:
    """kind: 'theme' | 'industry'"""
    out: Set[Tuple[str, str]] = set()
    for it in _paged(f"{kind}/{no}", "stocks", 20):
        code = str(it.get("itemCode") or "")
        name = str(it.get("stockName") or "").strip()
        if code and name:
            out.add((code, name))
    return out


def get_theme_memberships(themes: Iterable[ThemeInfo], workers: int = 6) -> pd.DataFrame:
    themes = list(themes)

    def work(t: ThemeInfo):
        members = _members_from_group("theme", t.theme_id)
        return t, members

    rows: List[Dict[str, str]] = []
    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = [ex.submit(work, t) for t in themes]
        for fut in cf.as_completed(futures):
            t, members = fut.result()
            for code, name in members:
                rows.append({
                    "theme_id": t.theme_id,
                    "theme_name": t.theme_name,
                    "code": code,
                    "name": name,
                })
    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError("테마 구성종목을 수집하지 못했습니다.")
    return df.drop_duplicates().sort_values(["theme_name", "code"]).reset_index(drop=True)


def get_industry_memberships(workers: int = 6) -> pd.DataFrame:
    catalog = get_upjong_catalog()

    def work(item: Tuple[str, str]):
        no, industry = item
        members = _members_from_group("industry", no)
        return no, industry, members

    rows: List[Dict[str, str]] = []
    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = [ex.submit(work, x) for x in catalog.items()]
        for fut in cf.as_completed(futures):
            no, industry, members = fut.result()
            for code, name in members:
                rows.append({"industry_id": no, "industry": industry, "code": code, "name": name})
    if not rows:
        return pd.DataFrame(columns=["industry_id", "industry", "code", "name"])
    return pd.DataFrame(rows).drop_duplicates().sort_values(["industry", "code"]).reset_index(drop=True)


def is_event_theme(name: str) -> bool:
    s = name.lower()
    return any(k.lower() in s for k in EVENT_KEYWORDS) or bool(re.search(r"20\d{2}.*신규상장", name))


def bucket_theme(theme_name: str, industry: str = "") -> str:
    s = theme_name or ""
    for bucket, keys in BUCKET_RULES[:-1]:
        if any(k.lower() in s.lower() for k in keys):
            return bucket
    if industry:
        for bucket, keys in INDUSTRY_BUCKET_RULES:
            if any(k.lower() in industry.lower() for k in keys):
                return bucket
    return "기타·이벤트"


def theme_score(theme_name: str, member_count: int) -> float:
    """주테마 선정 점수. 구조적/구체적 테마 우선, 이벤트성/초광범위 테마 후순위."""
    score = 0.0
    low = theme_name.lower()
    if any(k.lower() in low for k in STRUCTURAL_KEYWORDS):
        score += 50
    if is_event_theme(theme_name):
        score -= 70
    # 구성종목이 적을수록 상대적으로 구체적인 테마로 간주 (최대 +20)
    score += max(0.0, 20.0 - min(member_count, 100) * 0.2)
    # 지나치게 포괄적인 명칭은 소폭 감점
    if theme_name in {"방위산업/전쟁 및 테러", "밸류업(24년 기업가치 제고계획 발표)"}:
        score -= 5
    return score


def _preferred_base_name(name: str) -> str:
    """우선주/종류주 이름을 보통주 이름 후보로 정규화."""
    x = re.sub(r"\s+", "", name)
    # 예: 현대차2우B, 두산2우B, 유유제약1우, 삼성전자우
    x = re.sub(r"(?:\d+)?우(?:B|C)?$", "", x)
    return x


def build_master(
    stocks: pd.DataFrame,
    memberships: pd.DataFrame,
    industries: pd.DataFrame,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    theme_counts = memberships.groupby("theme_name")["code"].nunique().to_dict()
    by_code: Dict[str, List[str]] = (
        memberships.groupby("code")["theme_name"].apply(lambda s: sorted(set(s))).to_dict()
    )
    industry_by_code: Dict[str, str] = {}
    if not industries.empty:
        # 보통 한 종목은 하나의 업종. 혹시 중복이면 첫 값을 사용.
        industry_by_code = industries.groupby("code")["industry"].first().to_dict()

    name_to_code = {str(r["name"]).replace(" ", ""): str(r["code"]) for _, r in stocks.iterrows()}

    out: List[Dict[str, object]] = []
    for _, row in stocks.iterrows():
        code = str(row["code"])
        name = str(row["name"])
        market = str(row["market"])
        themes = list(by_code.get(code, []))
        inherited = False

        # 우선주가 테마 목록에 직접 안 잡히면 보통주에서 승계
        if not themes:
            base = _preferred_base_name(name)
            base_code = name_to_code.get(base)
            if base_code and base_code != code and by_code.get(base_code):
                themes = list(by_code[base_code])
                inherited = True

        industry = industry_by_code.get(code, "")
        if not industry and inherited:
            base = _preferred_base_name(name)
            base_code = name_to_code.get(base)
            if base_code:
                industry = industry_by_code.get(base_code, "")

        structural = [t for t in themes if not is_event_theme(t)]
        event = [t for t in themes if is_event_theme(t)]
        ranked = sorted(
            structural or themes,
            key=lambda t: (theme_score(t, int(theme_counts.get(t, 999))), t),
            reverse=True,
        )

        special = None
        if "스팩" in name or "기업인수목적" in name:
            special = "SPAC/기업인수목적회사"
        elif "리츠" in name or "REIT" in name.upper():
            special = "리츠/부동산"

        if special:
            primary = special
            bucket = "금융" if special.startswith("SPAC") else "기타·이벤트"
            source = "name_rule"
            confidence = "높음"
        elif ranked:
            primary = ranked[0]
            bucket = bucket_theme(primary, industry)
            source = "legacy_theme_inherited" if inherited else "legacy_theme"
            confidence = "높음" if not inherited else "중간"
        elif industry:
            primary = f"업종:{industry}"
            bucket = bucket_theme("", industry)
            source = "industry_fallback"
            confidence = "중간"
        else:
            primary = "기타/미분류"
            bucket = "기타·이벤트"
            source = "unclassified"
            confidence = "낮음"

        secondary = [t for t in ranked[1:4] if t != primary]
        out.append({
            "code": code,
            "name": name,
            "market": market,
            "big_theme": bucket,
            "primary_theme": primary,
            "secondary_theme_1": secondary[0] if len(secondary) > 0 else "",
            "secondary_theme_2": secondary[1] if len(secondary) > 1 else "",
            "secondary_theme_3": secondary[2] if len(secondary) > 2 else "",
            "industry": industry,
            "legacy_theme_count": len(themes),
            "legacy_theme_all": " | ".join(sorted(themes)),
            "event_theme_all": " | ".join(sorted(event)),
            "classification_source": source,
            "confidence": confidence,
        })

    master = pd.DataFrame(out).sort_values(["market", "big_theme", "primary_theme", "code"]).reset_index(drop=True)

    # long format에도 대분류/주테마 여부를 붙여 바로 조인 가능하게 함
    long_df = memberships.merge(
        master[["code", "market", "big_theme", "primary_theme"]], on="code", how="left"
    )
    long_df["is_primary"] = long_df["theme_name"] == long_df["primary_theme"]
    long_df["is_event_theme"] = long_df["theme_name"].map(is_event_theme)
    return master, long_df


def cache_fresh(path: Path, hours: int = 24) -> bool:
    if not path.exists():
        return False
    return (time.time() - path.stat().st_mtime) < hours * 3600


def run(output_dir: str, refresh: bool = False, workers: int = 6) -> Dict[str, str]:
    outdir = Path(output_dir)
    outdir.mkdir(parents=True, exist_ok=True)

    p_stocks = outdir / "_cache_stocks.csv"
    p_members = outdir / "_cache_theme_membership.csv"
    p_industry = outdir / "_cache_industry_membership.csv"

    if not refresh and cache_fresh(p_stocks) and cache_fresh(p_members) and cache_fresh(p_industry):
        stocks = pd.read_csv(p_stocks, dtype={"code": str})
        memberships = pd.read_csv(p_members, dtype={"code": str, "theme_id": str})
        industries = pd.read_csv(p_industry, dtype={"code": str, "industry_id": str})
    else:
        print("[1/4] KOSPI/KOSDAQ 전체 종목 수집")
        stocks = get_all_stocks()
        stocks.to_csv(p_stocks, index=False, encoding="utf-8-sig")

        print("[2/4] 네이버 레거시/인포스탁 테마 목록 및 구성종목 수집")
        themes = get_theme_catalog()
        memberships = get_theme_memberships(themes, workers=workers)
        memberships.to_csv(p_members, index=False, encoding="utf-8-sig")

        print("[3/4] 업종 fallback 수집")
        industries = get_industry_memberships(workers=workers)
        industries.to_csv(p_industry, index=False, encoding="utf-8-sig")

    print("[4/4] 주테마/부테마/대분류 생성")
    master, long_df = build_master(stocks, memberships, industries)

    p_master = outdir / "stock_theme_master.csv"
    p_long = outdir / "theme_membership.csv"
    p_catalog = outdir / "theme_catalog.csv"

    master.to_csv(p_master, index=False, encoding="utf-8-sig")
    long_df.to_csv(p_long, index=False, encoding="utf-8-sig")

    catalog = (
        memberships.groupby(["theme_id", "theme_name"], as_index=False)
        .agg(stock_count=("code", "nunique"))
        .sort_values(["stock_count", "theme_name"], ascending=[False, True])
    )
    catalog["big_theme"] = catalog["theme_name"].map(bucket_theme)
    catalog["is_event_theme"] = catalog["theme_name"].map(is_event_theme)
    catalog.to_csv(p_catalog, index=False, encoding="utf-8-sig")

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "stocks": str(len(master)),
        "kospi": str((master["market"] == "KOSPI").sum()),
        "kosdaq": str((master["market"] == "KOSDAQ").sum()),
        "legacy_themes": str(memberships["theme_name"].nunique()),
        "unclassified": str((master["classification_source"] == "unclassified").sum()),
        "master": str(p_master),
        "membership": str(p_long),
        "catalog": str(p_catalog),
    }
    print(summary)
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description="KOSPI/KOSDAQ 전종목 레거시 테마 분류")
    ap.add_argument("--output-dir", default="data/themes", help="CSV 출력 디렉터리")
    ap.add_argument("--refresh", action="store_true", help="24시간 캐시를 무시하고 새로 수집")
    ap.add_argument("--workers", type=int, default=6, help="테마/업종 상세 수집 동시 요청 수")
    ap.add_argument(
        "--keep-last-on-fail",
        action="store_true",
        help="수집 실패 시 직전 성공 CSV를 유지하고 경고만 남긴 채 정상 종료 (CI용)",
    )
    args = ap.parse_args()

    status_path = Path(args.output_dir) / "_status.json"
    status: Dict[str, object] = {}
    if status_path.exists():
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except Exception:
            status = {}
    now = datetime.now().isoformat(timespec="seconds")

    try:
        summary = run(args.output_dir, refresh=args.refresh, workers=args.workers)
        status.update({"last_success_at": now, "last_result": "success",
                       "last_error": "", "consecutive_failures": 0,
                       "stocks": summary.get("stocks")})
        status_path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        status.update({"last_failure_at": now, "last_result": "failure",
                       "last_error": f"{type(e).__name__}: {e}",
                       "consecutive_failures": int(status.get("consecutive_failures", 0) or 0) + 1})
        master = Path(args.output_dir) / "stock_theme_master.csv"
        if args.keep_last_on_fail and master.exists():
            status_path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
            msg = (f"테마 마스터 수집 실패 - 직전 데이터 유지 "
                   f"(마지막 성공: {status.get('last_success_at', '기록 없음')}, "
                   f"연속 실패 {status['consecutive_failures']}회): {e}")
            print(f"::warning title=테마 마스터 갱신 실패::{msg}")
            return
        raise


if __name__ == "__main__":
    main()
