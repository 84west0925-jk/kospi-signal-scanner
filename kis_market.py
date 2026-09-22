#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kis_market.py — 한국투자증권 KIS 분봉 데이터 공급기
────────────────────────────────────────────────────────────────────────────
목적
  · 기존 단타 RSI 전략/화면/포지션 구조는 건드리지 않는다.
  · 텔레그램 알림 봇에서만 yfinance 대신 KIS 공식 국내주식 분봉을 사용한다.
  · 1분봉을 KRX 정규장 기준 30/60분봉으로 직접 합성한다.
  · 과거 30/60분봉은 로컬 캐시에 보관하여 장중 API 호출량을 줄인다.

필수 환경변수
  KIS_APP_KEY       한국투자증권 Open API App Key
  KIS_APP_SECRET    한국투자증권 Open API App Secret

선택 환경변수
  KIS_ENV=real                  real(실전, 기본) / demo(모의)
  KIS_CALLS_PER_SECOND=4       REST 호출 속도 제한(안전 여유값, 직렬 호출)
  KIS_HISTORY_BARS=50          RSI 계산에 유지할 최소 과거 봉 수
  KIS_WORKERS=1                조회 작업 수(REST 요청은 항상 직렬 처리)

※ 계좌 조회/주문 API를 사용하지 않는다. App Key/App Secret만으로 시세 조회한다.
※ 실주문 POST는 전혀 없다.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Iterable

import pandas as pd
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

KST = timezone(timedelta(hours=9))
BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / ".kis_cache"
TOKEN_FILE = CACHE_DIR / "access_token.json"
BAR_CACHE_FILE = CACHE_DIR / "rsi_hybrid_bar_cache.json"

REAL_BASE_URL = "https://openapi.koreainvestment.com:9443"
DEMO_BASE_URL = "https://openapivts.koreainvestment.com:29443"
TOKEN_PATH = "/oauth2/tokenP"
TODAY_MINUTE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
DAILY_MINUTE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice"

TR_TODAY_MINUTE = "FHKST03010200"
TR_DAILY_MINUTE = "FHKST03010230"

SESSION_OPEN = dt_time(9, 0)
SESSION_CLOSE = dt_time(15, 30)


class KISError(RuntimeError):
    """KIS 설정/통신/응답 오류."""


class _RateLimiter:
    """
    여러 스레드가 공유하는 KIS REST 호출 제한기.

    기존 방식처럼 초당 N건을 한꺼번에 허용하지 않고,
    요청 사이를 1/N초 이상 벌려서 KIS 게이트웨이에 순간적으로
    요청이 몰리지 않도록 한다. EGW00201이 발생하면 penalize()로
    모든 스레드의 다음 호출을 함께 늦춘다.
    """

    def __init__(self, per_second: float) -> None:
        self.per_second = max(float(per_second), 1.0)
        self.min_interval = 1.0 / self.per_second
        self.lock = threading.Lock()
        self.next_allowed = 0.0

    def acquire(self) -> None:
        with self.lock:
            now = time.monotonic()
            wait = max(0.0, self.next_allowed - now)
            # 다음 스레드는 이번 요청이 실제로 나갈 시각을 기준으로
            # min_interval 뒤에만 호출할 수 있다.
            scheduled = max(now, self.next_allowed)
            self.next_allowed = scheduled + self.min_interval
        if wait > 0:
            time.sleep(wait)

    def penalize(self, seconds: float) -> None:
        """호출 제한 발생 시 모든 스레드에 공통 쿨다운을 적용한다."""
        seconds = max(float(seconds), 0.0)
        with self.lock:
            self.next_allowed = max(self.next_allowed, time.monotonic() + seconds)


@dataclass
class FetchSummary:
    frames: dict[str, pd.DataFrame]
    failed: dict[str, str]
    expected_close: datetime


class KISClient:
    def __init__(self) -> None:
        self.app_key = _first_env("KIS_APP_KEY", "KIS_APPKEY", "APP_KEY")
        self.app_secret = _first_env("KIS_APP_SECRET", "KIS_APPSECRET", "APP_SECRET")
        if not self.app_key or not self.app_secret:
            raise KISError(
                "KIS_APP_KEY / KIS_APP_SECRET 이 설정되지 않았습니다. "
                ".env.example을 복사해 .env에 입력하세요."
            )

        env = os.getenv("KIS_ENV", "real").strip().lower()
        self.env = "demo" if env in ("demo", "paper", "virtual", "vts") else "real"
        self.base_url = os.getenv(
            "KIS_BASE_URL",
            DEMO_BASE_URL if self.env == "demo" else REAL_BASE_URL,
        ).rstrip("/")

        cps = float(os.getenv("KIS_CALLS_PER_SECOND", "4"))
        self.rate = _RateLimiter(cps)
        self.session = requests.Session()
        # KIS REST 호출은 앱키 단위 호출 제한에 걸릴 수 있으므로 실제 HTTP 요청 자체를 직렬화한다.
        # RateLimiter.acquire()만 직렬화하면 이미 대기열을 통과한 여러 스레드가 동시에 요청될 수 있어
        # EGW00201이 연속 발생할 수 있다. 따라서 요청-응답 전체를 하나의 잠금으로 감싼다.
        self._request_lock = threading.Lock()
        self._token_lock = threading.Lock()
        self._token: str | None = None
        self._token_expires_at = 0.0
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._load_token_cache()

    # ── 인증 ────────────────────────────────────────────────────────────────
    def _load_token_cache(self) -> None:
        try:
            obj = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
            if obj.get("app_key") != self.app_key or obj.get("env") != self.env:
                return
            exp = float(obj.get("expires_at", 0))
            if exp > time.time() + 300:
                self._token = str(obj.get("access_token") or "") or None
                self._token_expires_at = exp
        except Exception:
            pass

    def _save_token_cache(self) -> None:
        if not self._token:
            return
        obj = {
            "app_key": self.app_key,
            "env": self.env,
            "access_token": self._token,
            "expires_at": self._token_expires_at,
        }
        TOKEN_FILE.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

    def access_token(self, force: bool = False) -> str:
        with self._token_lock:
            if not force and self._token and self._token_expires_at > time.time() + 300:
                return self._token

            r = self.session.post(
                f"{self.base_url}{TOKEN_PATH}",
                json={
                    "grant_type": "client_credentials",
                    "appkey": self.app_key,
                    "appsecret": self.app_secret,
                },
                timeout=15,
            )
            if r.status_code != 200:
                raise KISError(f"KIS Access Token 발급 실패 {r.status_code}: {r.text[:300]}")
            js = r.json()
            token = js.get("access_token")
            if not token:
                raise KISError(f"KIS Access Token 응답에 access_token이 없습니다: {js}")
            expires_in = int(js.get("expires_in") or 60 * 60 * 23)
            self._token = str(token)
            self._token_expires_at = time.time() + max(expires_in - 60, 600)
            self._save_token_cache()
            return self._token

    # ── REST 공통 ───────────────────────────────────────────────────────────
    def _get(self, path: str, tr_id: str, params: dict, retries: int = 3) -> dict:
        """
        KIS REST 공통 GET.

        중요:
        - KIS는 EGW00201(초당 거래건수 초과)을 HTTP 500 본문으로도 반환할 수 있다.
          따라서 HTTP 상태코드보다 먼저 가능한 경우 JSON 본문을 읽어 오류코드를 확인한다.
        - EGW00201이면 글로벌 rate limiter에 쿨다운을 걸고 자동 재시도한다.
        """
        last_error = ""
        for attempt in range(retries):
            # IMPORTANT: acquire + 실제 HTTP 요청 + 응답 확인까지 한 번에 한 요청만 실행한다.
            # 이렇게 해야 호출 제한 발생 직후 다른 스레드가 이미 예약된 요청을 계속 보내지 않는다.
            with self._request_lock:
                self.rate.acquire()
                token = self.access_token(force=False)
                headers = {
                    "authorization": f"Bearer {token}",
                    "appkey": self.app_key,
                    "appsecret": self.app_secret,
                    "tr_id": tr_id,
                    "custtype": "P",
                    "content-type": "application/json; charset=utf-8",
                }
                try:
                    r = self.session.get(
                        f"{self.base_url}{path}", headers=headers, params=params, timeout=12
                    )
                except requests.RequestException as e:
                    last_error = str(e)
                    r = None

            if r is None:
                time.sleep(min(0.8 * (attempt + 1), 4.0))
                continue

            # HTTP 500이라도 KIS가 JSON 오류코드를 내려주는 경우가 있으므로
            # 먼저 JSON 파싱을 시도한다.
            js = None
            try:
                js = r.json()
            except Exception:
                js = None

            if isinstance(js, dict):
                rt_cd = str(js.get("rt_cd", ""))
                msg_cd = str(js.get("msg_cd") or "")
                msg1 = str(js.get("msg1") or js.get("message") or "")

                if msg_cd == "EGW00201":
                    # 정시 알림 경로는 당일 분봉을 저속 직렬 호출한다.
                    # 일시적인 게이트웨이 제한이면 짧은 단계적 쿨다운 후 재시도한다.
                    base_cooldown = max(0.5, float(os.getenv("KIS_RATE_LIMIT_COOLDOWN_SEC", "2")))
                    cooldown = base_cooldown * (attempt + 1)
                    self.rate.penalize(cooldown)
                    last_error = f"{msg_cd} {msg1}".strip()
                    print(
                        f"[KIS 호출제한] {tr_id} {last_error} — "
                        f"{cooldown:.1f}초 후 재시도 ({attempt + 1}/{retries})"
                    )
                    continue

                if r.status_code == 200 and rt_cd == "0":
                    return js

                if r.status_code == 401:
                    self.access_token(force=True)
                    last_error = "HTTP 401 인증 갱신"
                    continue

                if r.status_code == 200:
                    last_error = f"{msg_cd} {msg1}".strip() or str(js)[:250]
                    time.sleep(min(0.5 * (attempt + 1), 3.0))
                    continue

            if r.status_code == 401:
                self.access_token(force=True)
                last_error = "HTTP 401 인증 갱신"
                continue

            if r.status_code != 200:
                last_error = f"HTTP {r.status_code}: {r.text[:250]}"
                time.sleep(min(0.8 * (attempt + 1), 4.0))
                continue

            if not isinstance(js, dict):
                last_error = f"JSON 파싱 실패: {r.text[:250]}"
                time.sleep(0.5)
                continue

            # 200 응답이지만 성공코드가 아닌 예외 상황
            msg_cd = str(js.get("msg_cd") or "")
            msg1 = str(js.get("msg1") or "")
            last_error = f"{msg_cd} {msg1}".strip()
            time.sleep(min(0.5 * (attempt + 1), 3.0))

        raise KISError(last_error or f"KIS 요청 실패: {tr_id}")

    # ── 1분봉 조회 ──────────────────────────────────────────────────────────
    def fetch_today_slice(self, code: str, end_hhmmss: str) -> pd.DataFrame:
        """당일 1분봉 최대 30건. end_hhmmss 시각을 기준으로 과거 방향 조회."""
        js = self._get(
            TODAY_MINUTE_PATH,
            TR_TODAY_MINUTE,
            {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": code,
                "FID_INPUT_HOUR_1": end_hhmmss,
                "FID_PW_DATA_INCU_YN": "Y",
                "FID_ETC_CLS_CODE": "",
            },
        )
        return _rows_to_minute_df(js.get("output2") or [], date_hint=datetime.now(KST).date())

    def fetch_day_minutes(self, code: str, target_date: date,
                          end_hhmmss: str = "153000", max_pages: int = 6) -> pd.DataFrame:
        """특정 일자의 1분봉을 과거일자 분봉 API로 조회한다."""
        all_frames: list[pd.DataFrame] = []
        cursor = end_hhmmss
        for _ in range(max_pages):
            js = self._get(
                DAILY_MINUTE_PATH,
                TR_DAILY_MINUTE,
                {
                    "FID_COND_MRKT_DIV_CODE": "J",
                    "FID_INPUT_ISCD": code,
                    "FID_INPUT_HOUR_1": cursor,
                    "FID_INPUT_DATE_1": target_date.strftime("%Y%m%d"),
                    "FID_PW_DATA_INCU_YN": "Y",
                    "FID_FAKE_TICK_INCU_YN": "",
                },
            )
            df = _rows_to_minute_df(js.get("output2") or [], date_hint=target_date)
            if df.empty:
                break
            df = df[df.index.date == target_date]
            if df.empty:
                break
            all_frames.append(df)
            earliest = df.index.min()
            if earliest.time() <= SESSION_OPEN:
                break
            nxt = earliest - timedelta(minutes=1)
            if nxt.time() < SESSION_OPEN:
                break
            cursor = nxt.strftime("%H%M%S")

        if not all_frames:
            return _empty_ohlcv()
        out = pd.concat(all_frames).sort_index()
        return out[~out.index.duplicated(keep="last")]

    def fetch_interval_bar(self, code: str, start: datetime, end: datetime,
                           interval: str) -> pd.DataFrame:
        """오늘의 정확한 신호 대상 구간만 1분봉으로 조회해 1개의 30/60분봉으로 합성."""
        if start.date() != datetime.now(KST).date():
            # 수동 과거 테스트용
            minutes = self.fetch_day_minutes(code, start.date(), end.strftime("%H%M%S"))
        else:
            frames: list[pd.DataFrame] = []
            step = int(interval.rstrip("m"))
            # 당일분봉 API는 1회 최대 30개. 필요한 구간을 30분씩 뒤에서 가져온다.
            query_end = end - timedelta(minutes=1)
            while query_end >= start:
                frames.append(self.fetch_today_slice(code, query_end.strftime("%H%M%S")))
                query_end -= timedelta(minutes=30)

            # 15:30 정규장 종가는 다음 봉이 없으므로 15:30 시각 조회가 필요하다.
            # 30분봉은 호출량을 줄이기 위해 마지막 조회 시각 자체를 15:30으로 사용한다.
            if end.time() == SESSION_CLOSE and step == 30:
                frames = [self.fetch_today_slice(code, end.strftime("%H%M%S"))]

            minutes = pd.concat([f for f in frames if not f.empty]).sort_index() \
                if any(not f.empty for f in frames) else _empty_ohlcv()
            if not minutes.empty:
                minutes = minutes[~minutes.index.duplicated(keep="last")]

        if minutes.empty:
            return _empty_ohlcv()

        final_close = end.time() == SESSION_CLOSE
        if final_close:
            mask = (minutes.index >= start) & (minutes.index <= end)
        else:
            mask = (minutes.index >= start) & (minutes.index < end)
        minutes = minutes.loc[mask]
        if minutes.empty:
            return _empty_ohlcv()

        bar = pd.DataFrame({
            "Open": [float(minutes["Open"].iloc[0])],
            "High": [float(minutes["High"].max())],
            "Low": [float(minutes["Low"].min())],
            "Close": [float(minutes["Close"].iloc[-1])],
            "Volume": [float(minutes["Volume"].sum())],
        }, index=pd.DatetimeIndex([start]))
        return bar


def _first_env(*keys: str) -> str:
    for key in keys:
        val = os.getenv(key)
        if val and val.strip():
            return val.strip()
    return ""


def _empty_ohlcv() -> pd.DataFrame:
    return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])


def _num(row: dict, *keys: str) -> float:
    for key in keys:
        val = row.get(key)
        if val not in (None, ""):
            try:
                return float(str(val).replace(",", ""))
            except Exception:
                pass
    return 0.0


def _rows_to_minute_df(rows: list[dict], date_hint: date) -> pd.DataFrame:
    parsed: list[dict] = []
    for row in rows:
        hhmmss = str(row.get("stck_cntg_hour") or "").zfill(6)
        day = str(row.get("stck_bsop_date") or date_hint.strftime("%Y%m%d"))
        if len(hhmmss) != 6 or len(day) != 8:
            continue
        try:
            ts = datetime.strptime(day + hhmmss, "%Y%m%d%H%M%S").replace(tzinfo=KST)
        except Exception:
            continue
        close = _num(row, "stck_prpr", "stck_clpr")
        if close <= 0:
            continue
        parsed.append({
            "time": ts,
            "Open": _num(row, "stck_oprc") or close,
            "High": _num(row, "stck_hgpr") or close,
            "Low": _num(row, "stck_lwpr") or close,
            "Close": close,
            "Volume": _num(row, "cntg_vol", "acml_vol"),
        })
    if not parsed:
        return _empty_ohlcv()
    df = pd.DataFrame(parsed).set_index("time").sort_index()
    return df[~df.index.duplicated(keep="last")]


def aggregate_session(minutes: pd.DataFrame, interval: str) -> pd.DataFrame:
    """1분봉을 09:00 기준 정규장 30/60분봉으로 합성."""
    if minutes.empty:
        return _empty_ohlcv()
    step = int(interval.rstrip("m"))
    if step not in (30, 60):
        raise ValueError("KIS 알림 데이터는 30m/60m만 지원합니다.")

    work = minutes.copy().sort_index()
    labels: list[datetime | None] = []
    for ts in work.index:
        local = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        if local.tzinfo is None:
            local = local.replace(tzinfo=KST)
        mins = local.hour * 60 + local.minute - 9 * 60
        if mins < 0 or mins > 390:
            labels.append(None)
            continue
        # 15:30 정규장 종가는 마지막 구간(15:00~15:30)에 포함한다.
        if mins == 390:
            mins = 389
        bucket = (mins // step) * step
        labels.append(datetime.combine(local.date(), SESSION_OPEN, tzinfo=KST) + timedelta(minutes=bucket))

    work["_bucket"] = labels
    work = work.dropna(subset=["_bucket"])
    if work.empty:
        return _empty_ohlcv()

    out = work.groupby("_bucket", sort=True).agg(
        Open=("Open", "first"),
        High=("High", "max"),
        Low=("Low", "min"),
        Close=("Close", "last"),
        Volume=("Volume", "sum"),
    )
    out.index = pd.DatetimeIndex(out.index)
    return out


class KISRSIDataSource:
    """RSI 알림용 하이브리드 캐시.

    과거 RSI 워밍업 데이터는 기존 시스템(yfinance)으로 1회 준비하고,
    장중 새로 확정되는 30/60분봉만 KIS 공식 당일분봉 API로 추가한다.

    이렇게 하면 전략/RSI 계산은 기존과 동일하게 유지하면서도, 실제 알림을
    발생시키는 최신 가격과 시각은 KIS 데이터로 확정할 수 있다.
    """

    CACHE_VERSION = 2

    def __init__(self, client: KISClient | None = None) -> None:
        self.client = client or KISClient()
        self.min_history = max(int(os.getenv("KIS_HISTORY_BARS", "50")), 20)
        self.workers = max(1, int(os.getenv("KIS_WORKERS", "1")))
        self.cache = self._load_cache()
        self.cache_lock = threading.Lock()

    def _load_cache(self) -> dict:
        try:
            obj = json.loads(BAR_CACHE_FILE.read_text(encoding="utf-8"))
            if isinstance(obj, dict) and obj.get("version") == self.CACHE_VERSION:
                return obj
        except Exception:
            pass
        return {"version": self.CACHE_VERSION, "intervals": {}}

    def _save_cache(self) -> None:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = BAR_CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.cache, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(BAR_CACHE_FILE)

    def _get_cached_df(self, ticker: str, interval: str) -> pd.DataFrame:
        rows = self.cache.get("intervals", {}).get(interval, {}).get(ticker, [])
        if not rows:
            return _empty_ohlcv()
        df = pd.DataFrame(rows)
        try:
            idx = pd.to_datetime(df.pop("time"), utc=True).dt.tz_convert("Asia/Seoul")
            df.index = pd.DatetimeIndex(idx)
            for c in ("Open", "High", "Low", "Close", "Volume"):
                df[c] = pd.to_numeric(df[c], errors="coerce")
            return df.dropna(subset=["Close"]).sort_index()
        except Exception:
            return _empty_ohlcv()

    def _set_cached_df(self, ticker: str, interval: str, df: pd.DataFrame) -> None:
        if df.empty:
            return
        df = _normalize_kst_index(df).sort_index()
        df = df[~df.index.duplicated(keep="last")].tail(180)
        rows = []
        for ts, r in df.iterrows():
            t = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
            if t.tzinfo is None:
                t = t.replace(tzinfo=KST)
            rows.append({
                "time": t.astimezone(timezone.utc).isoformat(),
                "Open": float(r.get("Open", r["Close"])),
                "High": float(r.get("High", r["Close"])),
                "Low": float(r.get("Low", r["Close"])),
                "Close": float(r["Close"]),
                "Volume": float(r.get("Volume", 0)),
            })
        with self.cache_lock:
            self.cache.setdefault("intervals", {}).setdefault(interval, {})[ticker] = rows

    def missing_history(self, tickers: Iterable[str], interval: str, before: datetime) -> list[str]:
        if before.tzinfo is None:
            before = before.replace(tzinfo=KST)
        before = before.astimezone(KST)
        missing = []
        for ticker in tickers:
            df = self._get_cached_df(ticker, interval)
            if len(df[df.index < before]) < self.min_history:
                missing.append(ticker)
        return missing

    def seed_history_from_frames(self, frames: dict[str, pd.DataFrame], interval: str,
                                 before: datetime) -> dict[str, str]:
        """기존 yfinance 분봉을 RSI 워밍업 캐시에 넣는다.

        KIS 과거분봉 API는 호출하지 않는다. 기존 캐시에 이미 들어간 KIS 확정봉이
        있으면 그 값을 우선 유지한다.
        """
        if before.tzinfo is None:
            before = before.replace(tzinfo=KST)
        before = before.astimezone(KST)
        failed: dict[str, str] = {}
        for ticker, raw in frames.items():
            try:
                hist = _normalize_kst_index(raw)
                hist = hist[hist.index < before]
                if hist.empty:
                    failed[ticker] = "과거 분봉 없음"
                    continue
                cached = self._get_cached_df(ticker, interval)
                # 뒤에 둔 cached(KIS 누적봉)가 같은 시각의 yfinance 값보다 우선한다.
                merged = _merge_frames([hist, cached])
                self._set_cached_df(ticker, interval, merged)
                if len(merged[merged.index < before]) < 16:
                    failed[ticker] = f"RSI 워밍업 부족({len(merged)}개)"
            except Exception as e:
                failed[ticker] = str(e)
        self._save_cache()
        return failed

    def get_signal_frames(self, tickers: Iterable[str], interval: str,
                          expected_close: datetime) -> FetchSummary:
        step = int(interval.rstrip("m"))
        if step not in (30, 60):
            raise KISError("KIS 알림 데이터는 30m 또는 60m만 지원합니다.")
        if expected_close.tzinfo is None:
            expected_close = expected_close.replace(tzinfo=KST)
        expected_close = expected_close.astimezone(KST).replace(second=0, microsecond=0)
        expected_start = expected_close - timedelta(minutes=step)

        tickers = list(tickers)
        failed: dict[str, str] = {}
        missing = self.missing_history(tickers, interval, expected_start)
        for t in missing:
            failed[t] = "RSI 과거 캐시 미준비 — bootstrap 필요"

        def fetch_one(ticker: str) -> tuple[str, pd.DataFrame | None, str | None]:
            try:
                code = ticker.split(".")[0]
                bar = self.client.fetch_interval_bar(code, expected_start, expected_close, interval)
                if bar.empty:
                    return ticker, None, "대상 구간 KIS 분봉 없음"
                return ticker, bar, None
            except Exception as e:
                return ticker, None, str(e)

        latest: dict[str, pd.DataFrame] = {}
        targets = [t for t in tickers if t not in failed]
        # 기본값은 1개 직렬 처리. KIS 호출 제한 안정성이 정시성보다 우선이다.
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(fetch_one, t): t for t in targets}
            done = 0
            for fut in as_completed(futures):
                ticker = futures[fut]
                done += 1
                try:
                    t, bar, err = fut.result()
                    if err or bar is None:
                        failed[t] = err or "알 수 없는 오류"
                    else:
                        latest[t] = bar
                except Exception as e:
                    failed[ticker] = str(e)
                if done % 25 == 0 or done == len(futures):
                    print(f"[KIS] 최신 확정봉 조회 {done}/{len(futures)}")

        frames: dict[str, pd.DataFrame] = {}
        for ticker, bar in latest.items():
            cached = self._get_cached_df(ticker, interval)
            merged = _merge_frames([cached, bar])
            merged = merged[merged.index <= expected_start]
            self._set_cached_df(ticker, interval, merged)
            if len(merged) >= 16 and merged.index[-1] == pd.Timestamp(expected_start):
                frames[ticker] = merged.tail(180)
            else:
                failed[ticker] = "정확한 대상 30/60분봉을 구성하지 못함"

        self._save_cache()
        return FetchSummary(frames=frames, failed=failed, expected_close=expected_close)

def _normalize_kst_index(df: pd.DataFrame) -> pd.DataFrame:
    """yfinance/KIS 분봉 인덱스를 Asia/Seoul aware DatetimeIndex로 통일한다."""
    if df is None or df.empty:
        return _empty_ohlcv()
    out = df.copy()
    idx = pd.DatetimeIndex(pd.to_datetime(out.index))
    if idx.tz is None:
        idx = idx.tz_localize("Asia/Seoul")
    else:
        idx = idx.tz_convert("Asia/Seoul")
    out.index = idx
    # 필요한 OHLCV 열만 남기되 Close는 반드시 있어야 한다.
    if "Close" not in out.columns:
        return _empty_ohlcv()
    for c in ("Open", "High", "Low"):
        if c not in out.columns:
            out[c] = out["Close"]
    if "Volume" not in out.columns:
        out["Volume"] = 0.0
    return out[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])

def _merge_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    valid = [_normalize_kst_index(f) for f in frames if f is not None and not f.empty]
    valid = [f for f in valid if not f.empty]
    if not valid:
        return _empty_ohlcv()
    out = pd.concat(valid).sort_index()
    out = out[~out.index.duplicated(keep="last")]
    return out[[c for c in ("Open", "High", "Low", "Close", "Volume") if c in out.columns]]


def scheduled_closes(day: date, interval: str = "30m") -> list[datetime]:
    """KRX 정규장 기준 신호 확정 시각 목록."""
    step = int(interval.rstrip("m"))
    if step not in (30, 60):
        raise ValueError("30m/60m만 지원합니다.")
    start = datetime.combine(day, SESSION_OPEN, tzinfo=KST)
    close = datetime.combine(day, SESSION_CLOSE, tzinfo=KST)
    cur = start + timedelta(minutes=step)
    out = []
    while cur < close:
        out.append(cur)
        cur += timedelta(minutes=step)
    if not out or out[-1] != close:
        out.append(close)
    return out


def latest_scheduled_close(now: datetime, interval: str = "30m") -> datetime | None:
    if now.tzinfo is None:
        now = now.replace(tzinfo=KST)
    now = now.astimezone(KST)
    closes = [x for x in scheduled_closes(now.date(), interval) if x <= now]
    return closes[-1] if closes else None
