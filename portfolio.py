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


def recalc(pos: dict) -> dict:
    """entries·exits 를 원본으로 삼아 차수·평단·기준가를 전면 재계산."""
    entries = sorted(pos.get("entries", []), key=lambda e: str(e.get("time") or ""))
    exits = sorted(pos.get("exits", []), key=lambda x: str(x.get("time") or ""))
    for i, e in enumerate(entries, 1):
        e["stage"] = i
    for i, x in enumerate(exits, 1):
        x["stage"] = i

    pos["entries"] = entries
    pos["exits"] = exits
    pos["buy_stage"] = min(len(entries), 3)
    pos["sell_stage"] = min(len(exits), 3)
    pos["first_buy"] = float(entries[0]["price"]) if entries else None
    pos["first_sell"] = float(exits[0]["price"]) if exits else None
    pos["opened"] = entries[0].get("time") if entries else None

    tot_qty = sum(int(e.get("qty", 0)) for e in entries)
    tot_amt = sum(float(e["price"]) * int(e.get("qty", 0)) for e in entries)
    pos["avg_price"] = round(tot_amt / tot_qty, 2) if tot_qty else None
    return pos


def held_qty(pos: dict) -> int:
    return sum(int(e.get("qty", 0)) for e in pos.get("entries", [])) - \
           sum(int(x.get("qty", 0)) for x in pos.get("exits", []))


def archive(state: dict, ticker: str, ts: str, note: str) -> dict:
    """잔량 0 포지션을 거래 이력으로 이관."""
    pos = state["positions"][ticker]
    invested = sum(buy_cost(e["price"], e.get("qty", 0)) for e in pos["entries"])
    recovered = sum(sell_proceeds(x["price"], x.get("qty", 0)) for x in pos.get("exits", []))
    state.setdefault("history", []).append({
        "종목": pos["name"], "코드": ticker.replace(".KS", ""),
        "진입": (pos.get("opened") or "")[:16], "청산": ts[:16],
        "분할차수": pos["buy_stage"],
        "평단": round(pos.get("avg_price") or 0, 1),
        "총수량": sum(e.get("qty", 0) for e in pos["entries"]),
        "투입금액": round(invested), "회수금액": round(recovered),
        "실현손익": round(recovered - invested),
        "수익률%": round((recovered - invested) / invested * 100, 2) if invested else 0,
        "사유": note,
        # 복원용 원본 스냅샷(화면·CSV에는 표시하지 않음)
        "_ticker": ticker,
        "_entries": json.loads(json.dumps(pos.get("entries", []), ensure_ascii=False)),
        "_exits": json.loads(json.dumps(pos.get("exits", []), ensure_ascii=False)),
        "_source": pos.get("source"),
        "_memo": pos.get("memo"),
    })
    state["positions"].pop(ticker, None)
    return state


HIST_COLS = ["종목", "코드", "진입", "청산", "분할차수", "평단", "총수량",
             "투입금액", "회수금액", "실현손익", "수익률%", "사유"]


def hist_view(history: list[dict]) -> pd.DataFrame:
    """내부 키(_로 시작)를 제외한 표시용 데이터프레임."""
    if not history:
        return pd.DataFrame(columns=HIST_COLS)
    df = pd.DataFrame(history)
    return df[[c for c in df.columns if not str(c).startswith("_")]]


def close_out(state: dict, ticker: str, price: float, qty: int, ts: str, note: str) -> dict:
    """매도 반영. 잔량 0이면 이력으로 이관."""
    pos = state["positions"][ticker]
    pos.setdefault("exits", []).append(
        {"stage": pos.get("sell_stage", 0) + 1, "price": price, "qty": qty, "time": ts})
    pos["sell_stage"] = min(pos.get("sell_stage", 0) + 1, 3)
    if pos["sell_stage"] == 1:
        pos["first_sell"] = price

    if held_qty(pos) <= 0:
        state = archive(state, ticker, ts, note)
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
    hist = hist_view(state.get("history", []))
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("보유 종목", len(positions))
    c2.metric("평가손익", f"{sum(r.get('평가손익', 0) or 0 for r in rows):,.0f}원")
    c3.metric("누적 실현손익",
              f"{hist['실현손익'].sum():,.0f}원" if not hist.empty and '실현손익' in hist else "0원")
    if not hist.empty and "수익률%" in hist:
        c4.metric("승률", f"{(hist['수익률%'] > 0).mean() * 100:.0f}%  ({len(hist)}건)")
    else:
        c4.metric("승률", "-")

    tab_hold, tab_add, tab_sell, tab_edit, tab_hist = st.tabs(
        ["📋 보유 현황", "➕ 매수 등록", "➖ 매도 등록", "✏️ 거래내역 수정", "📜 거래 이력"])

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

    # ── 거래내역 수정 ────────────────────────────────────────────────────────
    with tab_edit:
        if not positions:
            st.info("수정할 보유 종목이 없습니다.")
        else:
            st.caption(
                "잘못 입력한 매수·매도 건을 직접 고칩니다. 셀을 클릭해 값을 수정하고, "
                "행을 지우려면 행 왼쪽을 선택한 뒤 휴지통 아이콘을 누르세요. "
                "저장하면 차수·평단·분할 기준가가 자동 재계산됩니다.")

            emap = {p["name"]: t for t, p in positions.items()}
            ename = st.selectbox("수정할 종목", list(emap.keys()), key="edit_sel")
            etkr = emap[ename]
            epos = positions[etkr]

            num_cfg = {
                "단가": st.column_config.NumberColumn("단가(원)", min_value=1.0, step=100.0,
                                                     format="%.1f", required=True),
                "수량": st.column_config.NumberColumn("수량(주)", min_value=1, step=1,
                                                     format="%d", required=True),
                "일시": st.column_config.TextColumn("일시", required=True,
                                                   help="예: 2026-08-07 09:30"),
            }

            st.markdown("**매수 내역**")
            e_df = pd.DataFrame(
                [{"차수": e.get("stage"), "단가": float(e.get("price", 0)),
                  "수량": int(e.get("qty", 0)), "일시": str(e.get("time") or "")}
                 for e in epos.get("entries", [])],
                columns=["차수", "단가", "수량", "일시"])
            e_new = st.data_editor(
                e_df, num_rows="dynamic", use_container_width=True, hide_index=True,
                disabled=["차수"], key=f"edit_entries_{etkr}", column_config=num_cfg)

            st.markdown("**매도 내역**")
            x_df = pd.DataFrame(
                [{"차수": x.get("stage"), "단가": float(x.get("price", 0)),
                  "수량": int(x.get("qty", 0)), "일시": str(x.get("time") or "")}
                 for x in epos.get("exits", [])],
                columns=["차수", "단가", "수량", "일시"])
            x_new = st.data_editor(
                x_df, num_rows="dynamic", use_container_width=True, hide_index=True,
                disabled=["차수"], key=f"edit_exits_{etkr}", column_config=num_cfg)

            def _rows(df) -> tuple[list[dict], list[str]]:
                out, errs = [], []
                for i, r in df.iterrows():
                    price, qty, when = r.get("단가"), r.get("수량"), r.get("일시")
                    if pd.isna(price) or pd.isna(qty) or not str(when or "").strip():
                        errs.append(f"{i + 1}행 — 단가·수량·일시를 모두 입력하세요.")
                        continue
                    if float(price) <= 0 or int(qty) <= 0:
                        errs.append(f"{i + 1}행 — 단가와 수량은 0보다 커야 합니다.")
                        continue
                    out.append({"price": float(price), "qty": int(qty),
                                "time": str(when).strip()})
                return out, errs

            if st.button("💾 수정 내용 저장", type="primary", use_container_width=True,
                         disabled=not writable(), key="edit_save"):
                entries, err1 = _rows(e_new)
                exits, err2 = _rows(x_new)
                errs = err1 + err2

                buy_q = sum(e["qty"] for e in entries)
                sell_q = sum(x["qty"] for x in exits)
                if not entries:
                    errs.append("매수 내역이 비었습니다. 종목 전체를 지우려면 "
                                "‘보유 현황 → 오등록 정정’을 쓰세요.")
                elif sell_q > buy_q:
                    errs.append(f"매도 수량({sell_q}주)이 매수 수량({buy_q}주)을 초과합니다.")

                if errs:
                    st.error("\n\n".join(f"· {e}" for e in errs))
                else:
                    new_pos = dict(epos)
                    new_pos["entries"] = entries
                    new_pos["exits"] = exits
                    new_pos = recalc(new_pos)
                    state["positions"][etkr] = new_pos

                    closed = held_qty(new_pos) <= 0
                    if closed:
                        last_ts = exits[-1]["time"] if exits else \
                            datetime.now(sw.KST).strftime("%Y-%m-%d %H:%M")
                        state = archive(state, etkr, last_ts, "수정 반영 청산")

                    ok, msg = save(state, f"portfolio: {ename} 거래내역 수정")
                    if ok:
                        st.success(
                            f"{ename} 수정 완료 — "
                            + (f"잔량 0 → 거래 이력으로 이관 · {msg}" if closed else
                               f"평단 {new_pos['avg_price']:,.0f}원 / 잔량 "
                               f"{held_qty(new_pos)}주 · {msg}"))
                        st.rerun()
                    else:
                        st.error(msg)

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

            records = state.get("history", [])

            # ── 이력 수정·삭제 ────────────────────────────────────────────
            with st.expander("✏️ 이력 수정·삭제"):
                st.caption(
                    "실현손익·수익률%는 투입금액·회수금액에서 자동 재계산되므로 직접 고칠 수 없습니다. "
                    "잘못 들어간 건은 ‘삭제’를 체크하고 저장하세요.")

                h_edit = hist.copy()
                h_edit.insert(0, "삭제", False)
                h_new = st.data_editor(
                    h_edit, num_rows="fixed", use_container_width=True, hide_index=True,
                    disabled=["실현손익", "수익률%"], key="hist_editor",
                    column_config={
                        "삭제": st.column_config.CheckboxColumn("삭제", default=False),
                        "평단": st.column_config.NumberColumn(format="%.1f"),
                        "총수량": st.column_config.NumberColumn(format="%d"),
                        "투입금액": st.column_config.NumberColumn(format="%d"),
                        "회수금액": st.column_config.NumberColumn(format="%d"),
                    })

                if st.button("💾 이력 저장", type="primary", use_container_width=True,
                             disabled=not writable(), key="hist_save"):
                    errs, kept = [], []
                    for i, r in h_new.iterrows():
                        if bool(r.get("삭제")):
                            continue
                        inv, rec = r.get("투입금액"), r.get("회수금액")
                        if pd.isna(inv) or pd.isna(rec) or float(inv) <= 0:
                            errs.append(f"{i + 1}행 — 투입금액·회수금액을 확인하세요"
                                        " (투입금액은 0보다 커야 합니다).")
                            continue
                        base = dict(records[i])          # 내부 스냅샷 유지
                        for c in hist.columns:
                            v = r.get(c)
                            base[c] = None if pd.isna(v) else (
                                v.item() if hasattr(v, "item") else v)
                        base["실현손익"] = round(float(rec) - float(inv))
                        base["수익률%"] = round((float(rec) - float(inv)) / float(inv) * 100, 2)
                        kept.append(base)

                    removed = len(records) - len(kept)
                    if errs:
                        st.error("\n\n".join(f"· {e}" for e in errs))
                    else:
                        state["history"] = kept
                        ok, msg = save(state, "portfolio: 거래 이력 수정")
                        if ok:
                            st.success(f"이력 저장 완료 — 삭제 {removed}건 / 잔여 "
                                       f"{len(kept)}건 · {msg}")
                            st.rerun()
                        else:
                            st.error(msg)

            # ── 보유 포지션으로 복원 ──────────────────────────────────────
            with st.expander("↩️ 보유 포지션으로 복원"):
                st.caption("실수로 청산 처리된 거래를 매수·매도 내역 그대로 되살립니다. "
                           "복원 후 ‘거래내역 수정’ 탭에서 잘못된 매도 건을 지우세요.")
                restorable = {
                    f"{i + 1}. {r.get('종목')} ({r.get('진입','')[:10]} → "
                    f"{r.get('청산','')[:10]})": i
                    for i, r in enumerate(records) if r.get("_entries")}

                if not restorable:
                    st.info("복원 가능한 이력이 없습니다. "
                            "(이번 업데이트 이후 청산된 거래부터 원본 스냅샷이 보관됩니다.)")
                else:
                    rlabel = st.selectbox("복원할 거래", list(restorable.keys()),
                                          key="hist_restore_sel")
                    ridx = restorable[rlabel]
                    rrec = records[ridx]
                    rtkr = rrec.get("_ticker") or f"{rrec.get('코드','')}.KS"
                    dup = rtkr in state.get("positions", {})
                    if dup:
                        st.warning(f"{rrec.get('종목')}은(는) 이미 보유 중입니다. "
                                   "복원하면 기존 포지션과 충돌하므로 먼저 정리하세요.", icon="⚠️")

                    if st.button("복원", type="primary", use_container_width=True,
                                 disabled=not writable() or dup, key="hist_restore_btn"):
                        pos = sw.new_position(rrec.get("종목"))
                        pos["entries"] = rrec.get("_entries", [])
                        pos["exits"] = rrec.get("_exits", [])
                        if rrec.get("_source"):
                            pos["source"] = rrec["_source"]
                        if rrec.get("_memo"):
                            pos["memo"] = rrec["_memo"]
                        state.setdefault("positions", {})[rtkr] = recalc(pos)
                        state["history"] = [r for j, r in enumerate(records) if j != ridx]

                        ok, msg = save(state, f"portfolio: {rrec.get('종목')} 이력 복원")
                        if ok:
                            st.success(f"{rrec.get('종목')} 복원 완료 — 잔량 "
                                       f"{held_qty(pos)}주 · {msg}")
                            st.rerun()
                        else:
                            st.error(msg)
