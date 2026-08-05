#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
portfolio.py — 보유 종목 등록·추적·거래 이력 관리
────────────────────────────────────────────────────────────────────────────
저장소  : GitHub 저장소의 swing_positions.json (단일 원본)
  · Streamlit 앱   → GitHub Contents API 로 직접 읽고 씀 (PAT 필요)
  · GitHub Actions → 체크아웃된 로컬 파일 사용 후 워크플로가 커밋
두 경로가 같은 파일을 보므로, 웹에서 등록한 종목을 알림 봇이 그대로 추적한다.

Streamlit Secrets 설정 (Manage app > Settings > Secrets)
  GITHUB_TOKEN = "github_pat_..."          # Contents Read/Write 권한
  GITHUB_REPO  = "84west0925-jk/kospi-signal-scanner"
토큰이 없으면 읽기 전용으로 동작한다(등록·수정 불가).
"""
from __future__ import annotations

import base64
import json
import os
from datetime import datetime

import pandas as pd
import requests

import swing_rsi as sw

STATE_PATH = "swing_positions.json"
DEFAULT_REPO = "84west0925-jk/kospi-signal-scanner"

# ── 매매 비용 (2026년 기준) ───────────────────────────────────────────────────
BUY_FEE_PCT = 0.015     # 매수 수수료 %
SELL_FEE_PCT = 0.015    # 매도 수수료 %
SELL_TAX_PCT = 0.20     # 코스피 매도세 (증권거래세 0.05% + 농특세 0.15%)

IN_ACTIONS = os.getenv("GITHUB_ACTIONS", "").lower() == "true"


# ══════════════════════════════════════════════════════════════════════════════
# GitHub 연동
# ══════════════════════════════════════════════════════════════════════════════
def _secret(key: str, default: str = "") -> str:
    val = os.getenv(key)
    if val:
        return val.strip()
    try:
        import streamlit as st
        return str(st.secrets[key]).strip()
    except Exception:
        return default


def _repo() -> str:
    return _secret("GITHUB_REPO", DEFAULT_REPO)


def _token() -> str:
    return _secret("GITHUB_TOKEN", "")


def writable() -> bool:
    """등록·수정이 가능한 상태인지."""
    return IN_ACTIONS or bool(_token())


def load() -> dict:
    """상태 로드. Actions 안에서는 로컬 파일, 앱에서는 GitHub 원본."""
    if IN_ACTIONS:
        return sw.load_state()
    try:
        url = f"https://api.github.com/repos/{_repo()}/contents/{STATE_PATH}"
        headers = {"Accept": "application/vnd.github+json"}
        if _token():
            headers["Authorization"] = f"Bearer {_token()}"
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        js = r.json()
        state = json.loads(base64.b64decode(js["content"]).decode("utf-8"))
        state["_sha"] = js["sha"]
        return state
    except Exception as e:
        print(f"[portfolio] GitHub 조회 실패 → 로컬 파일 사용: {e}")
        return sw.load_state()


def save(state: dict, message: str) -> tuple[bool, str]:
    """상태 저장. 성공 여부와 메시지를 반환."""
    if IN_ACTIONS:
        sw.save_state(state)
        return True, "로컬 저장 완료"
    if not _token():
        return False, "GITHUB_TOKEN 미설정 — 읽기 전용 모드입니다."

    state = dict(state)
    sha = state.pop("_sha", None)
    state["updated"] = datetime.now(sw.KST).strftime("%Y-%m-%d %H:%M:%S")
    body = {
        "message": message,
        "content": base64.b64encode(
            json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")).decode(),
        "branch": "main",
    }
    if sha:
        body["sha"] = sha
    try:
        r = requests.put(
            f"https://api.github.com/repos/{_repo()}/contents/{STATE_PATH}",
            headers={"Authorization": f"Bearer {_token()}",
                     "Accept": "application/vnd.github+json"},
            json=body, timeout=15)
        if r.status_code in (200, 201):
            return True, "GitHub 저장 완료"
        return False, f"저장 실패 {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, f"저장 예외: {e}"


# ══════════════════════════════════════════════════════════════════════════════
# 손익 계산
# ══════════════════════════════════════════════════════════════════════════════
def buy_cost(price: float, qty: int) -> float:
    return price * qty * (1 + BUY_FEE_PCT / 100)


def sell_proceeds(price: float, qty: int) -> float:
    return price * qty * (1 - (SELL_FEE_PCT + SELL_TAX_PCT) / 100)


def position_summary(pos: dict, cur_price: float | None) -> dict:
    qty = sum(e.get("qty", 0) for e in pos["entries"]) - \
          sum(x.get("qty", 0) for x in pos.get("exits", []))
    invested = sum(buy_cost(e["price"], e.get("qty", 0)) for e in pos["entries"])
    recovered = sum(sell_proceeds(x["price"], x.get("qty", 0)) for x in pos.get("exits", []))
    avg = pos.get("avg_price") or 0
    row = {
        "종목": pos["name"],
        "매수단계": f"{pos['buy_stage']}/3",
        "매도단계": f"{pos['sell_stage']}/3" if pos.get("sell_stage") else "-",
        "보유수량": qty,
        "평단": round(avg, 1),
        "투입금액": round(invested),
        "회수금액": round(recovered),
        "진입일": (pos.get("opened") or "")[:16],
    }
    if cur_price:
        value = sell_proceeds(cur_price, qty)
        pnl = value + recovered - invested
        row |= {
            "현재가": round(cur_price, 1),
            "평가금액": round(value),
            "평가손익": round(pnl),
            "수익률%": round(pnl / invested * 100, 2) if invested else None,
        }
    if pos.get("first_buy"):
        row |= {
            "2차기준(-5%)": round(pos["first_buy"] * 0.95, 1),
            "3차기준(-10%)": round(pos["first_buy"] * 0.90, 1),
        }
    row["출처"] = "직접등록" if pos.get("source") == "manual" else "봇신호"
    return row


def close_out(state: dict, ticker: str, price: float, qty: int, ts: str, note: str) -> dict:
    """매도 반영. 잔량 0이면 이력으로 이관."""
    pos = state["positions"][ticker]
    pos.setdefault("exits", []).append(
        {"stage": pos.get("sell_stage", 0) + 1, "price": price, "qty": qty, "time": ts})
    pos["sell_stage"] = min(pos.get("sell_stage", 0) + 1, 3)
    if pos["sell_stage"] == 1:
        pos["first_sell"] = price

    held = sum(e.get("qty", 0) for e in pos["entries"]) - \
           sum(x.get("qty", 0) for x in pos["exits"])
    if held <= 0:
        invested = sum(buy_cost(e["price"], e.get("qty", 0)) for e in pos["entries"])
        recovered = sum(sell_proceeds(x["price"], x.get("qty", 0)) for x in pos["exits"])
        state["history"].append({
            "종목": pos["name"], "코드": ticker.replace(".KS", ""),
            "진입": (pos.get("opened") or "")[:16], "청산": ts[:16],
            "분할차수": pos["buy_stage"],
            "평단": round(pos.get("avg_price") or 0, 1),
            "총수량": sum(e.get("qty", 0) for e in pos["entries"]),
            "투입금액": round(invested), "회수금액": round(recovered),
            "실현손익": round(recovered - invested),
            "수익률%": round((recovered - invested) / invested * 100, 2) if invested else 0,
            "사유": note,
        })
        state["positions"].pop(ticker, None)
    return state


# ══════════════════════════════════════════════════════════════════════════════
# Streamlit 화면
# ══════════════════════════════════════════════════════════════════════════════
def render(st):
    st.title("💼 보유 종목 추적")
    st.caption("직접 매수한 종목을 등록하면 알림 봇이 분할 매수·매도 시점을 함께 추적합니다.")

    if not writable():
        st.warning(
            "**읽기 전용 모드** — 등록·수정하려면 Streamlit Secrets에 `GITHUB_TOKEN`이 필요합니다. "
            "(Manage app → Settings → Secrets)", icon="🔒")

    state = load()
    universe = sw.get_universe(100, 10)
    positions = state.get("positions", {})

    # ── 현재가 조회 ──────────────────────────────────────────────────────────
    prices: dict[str, float] = {}
    if positions:
        try:
            data = sw.fetch_intraday(list(positions.keys()), "30m")
            prices = {t: float(d["Close"].iloc[-1]) for t, d in data.items()}
        except Exception:
            pass

    # ── 요약 지표 ────────────────────────────────────────────────────────────
    rows = [position_summary(p, prices.get(t)) for t, p in positions.items()]
    hist = pd.DataFrame(state.get("history", []))
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("보유 종목", len(positions))
    c2.metric("평가손익", f"{sum(r.get('평가손익', 0) or 0 for r in rows):,.0f}원")
    c3.metric("누적 실현손익",
              f"{hist['실현손익'].sum():,.0f}원" if not hist.empty and '실현손익' in hist else "0원")
    if not hist.empty and "수익률%" in hist:
        c4.metric("승률", f"{(hist['수익률%'] > 0).mean() * 100:.0f}%  ({len(hist)}건)")
    else:
        c4.metric("승률", "-")

    tab_hold, tab_add, tab_sell, tab_hist = st.tabs(
        ["📋 보유 현황", "➕ 매수 등록", "➖ 매도 등록", "📜 거래 이력"])

    # ── 보유 현황 ────────────────────────────────────────────────────────────
    with tab_hold:
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
            st.caption(
                f"※ 손익은 매매비용 반영 — 매수 수수료 {BUY_FEE_PCT}% / "
                f"매도 수수료 {SELL_FEE_PCT}% + 매도세 {SELL_TAX_PCT}%(코스피 2026년 기준)")

            with st.expander("🗑 오등록 정정 — 포지션 삭제"):
                st.caption("잘못 등록한 종목을 이력 없이 지웁니다. 실제 매도는 ‘매도 등록’을 쓰세요.")
                dmap = {p["name"]: t for t, p in positions.items()}
                dcol1, dcol2 = st.columns([3, 1])
                dname = dcol1.selectbox("삭제할 종목", list(dmap.keys()), key="del_sel")
                if dcol2.button("삭제", type="secondary", use_container_width=True,
                                disabled=not writable()):
                    state["positions"].pop(dmap[dname], None)
                    ok, msg = save(state, f"portfolio: {dname} 포지션 삭제")
                    (st.success if ok else st.error)(f"{dname} 삭제됨 · {msg}" if ok else msg)
                    if ok:
                        st.rerun()
        else:
            st.info("등록된 보유 종목이 없습니다. ‘매수 등록’ 탭에서 추가하세요.")

    # ── 매수 등록 ────────────────────────────────────────────────────────────
    with tab_add:
        with st.form("add_pos"):
            col1, col2 = st.columns(2)
            with col1:
                name = st.selectbox("종목", list(universe.values()))
                price = st.number_input("매수 단가(원)", 1.0, 100_000_000.0, 100000.0, 100.0)
            with col2:
                qty = st.number_input("수량(주)", 1, 1_000_000, 1)
                when = st.text_input("매수 일시", datetime.now(sw.KST).strftime("%Y-%m-%d %H:%M"))
            memo = st.text_input("메모(선택)", placeholder="예: RSI 28에서 1차 진입")
            submitted = st.form_submit_button("등록", type="primary", use_container_width=True,
                                              disabled=not writable())

        if submitted:
            ticker = [k for k, v in universe.items() if v == name][0]
            pos = positions.get(ticker) or sw.new_position(name)
            stage = min(pos["buy_stage"] + 1, 3)
            pos["buy_stage"] = stage
            pos["entries"].append({"stage": stage, "price": float(price),
                                   "qty": int(qty), "time": when})
            if stage == 1:
                pos["first_buy"] = float(price)
                pos["opened"] = when
            tot_qty = sum(e.get("qty", 0) for e in pos["entries"])
            tot_amt = sum(e["price"] * e.get("qty", 0) for e in pos["entries"])
            pos["avg_price"] = round(tot_amt / tot_qty, 2) if tot_qty else float(price)
            pos["source"] = "manual"
            if memo:
                pos["memo"] = memo
            state.setdefault("positions", {})[ticker] = pos

            ok, msg = save(state, f"portfolio: {name} {stage}차 매수 등록")
            (st.success if ok else st.error)(
                f"{name} {stage}차 매수 등록 — 평단 {pos['avg_price']:,.0f}원 / {tot_qty}주 · {msg}"
                if ok else msg)
            if ok:
                st.rerun()

    # ── 매도 등록 ────────────────────────────────────────────────────────────
    with tab_sell:
        if not positions:
            st.info("보유 중인 종목이 없습니다.")
        else:
            opts = {p["name"]: t for t, p in positions.items()}
            with st.form("sell_pos"):
                col1, col2 = st.columns(2)
                with col1:
                    sname = st.selectbox("종목", list(opts.keys()))
                    sprice = st.number_input("매도 단가(원)", 1.0, 100_000_000.0, 100000.0, 100.0)
                with col2:
                    tkr = opts[sname]
                    held = sum(e.get("qty", 0) for e in positions[tkr]["entries"]) - \
                           sum(x.get("qty", 0) for x in positions[tkr].get("exits", []))
                    sqty = st.number_input("수량(주)", 1, max(held, 1), max(held, 1))
                    swhen = st.text_input("매도 일시",
                                          datetime.now(sw.KST).strftime("%Y-%m-%d %H:%M"))
                note = st.text_input("사유(선택)", placeholder="예: RSI 72 도달 1차 청산")
                sold = st.form_submit_button("매도 등록", type="primary",
                                             use_container_width=True, disabled=not writable())

            if sold:
                state = close_out(state, opts[sname], float(sprice), int(sqty),
                                  swhen, note or "수동 매도")
                ok, msg = save(state, f"portfolio: {sname} 매도 등록")
                (st.success if ok else st.error)(f"{sname} {sqty}주 매도 반영 · {msg}" if ok else msg)
                if ok:
                    st.rerun()

    # ── 거래 이력 ────────────────────────────────────────────────────────────
    with tab_hist:
        if hist.empty:
            st.info("청산 완료된 거래가 없습니다.")
        else:
            st.dataframe(hist, use_container_width=True, hide_index=True)
            st.download_button("📥 거래 이력 CSV 저장",
                               hist.to_csv(index=False).encode("utf-8-sig"),
                               file_name=f"거래이력_{datetime.now(sw.KST):%Y%m%d}.csv",
                               mime="text/csv")
