# -*- coding: utf-8 -*-
"""
코스닥 단타 자동매매 프로그램
- 키움증권 차세대 REST API (HTTP) 기반
- tkinter GUI 대시보드 + 백그라운드 매매 스레드
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import sys
import threading
import time
import queue
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

import requests

import config


# ──────────────────────────────────────────────────────────────
# 1. 키움 REST API 래퍼
# ──────────────────────────────────────────────────────────────
class KiwoomAPI:
    """키움증권 차세대 REST API 통신 클래스"""

    def __init__(self):
        self.base_url = config.BASE_URL
        self.access_token: Optional[str] = None
        self.account_no = config.ACCOUNT_NO

    # ---------- 인증 ----------
    def get_access_token(self) -> str:
        url = f"{self.base_url}/oauth2/token"
        headers = {"Content-Type": "application/json;charset=UTF-8"}
        payload = {
            "grant_type": "client_credentials",
            "appkey": config.APP_KEY,
            "secretkey": config.SECRET_KEY,
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        data = resp.json()
        self.access_token = data["token"]
        return self.access_token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json;charset=UTF-8",
        }

    # ---------- 시세 조회 ----------
    def fetch_volume_rank(self) -> List[dict]:
        """당일 거래대금 상위 종목 조회 (TR: ka10032)"""
        url = f"{self.base_url}/api/dostk/rkinfo"
        headers = {
            **self._headers(),
            "api-id": "ka10032",
        }
        payload = {
            "w_mkt_gb": "101",
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        data = resp.json()
        raw_list = data.get("output1", data.get("output", []))
        if isinstance(raw_list, list):
            return raw_list
        return []

    # ---------- 분봉 차트 조회 ----------
    def fetch_minute_chart(self, stock_code: str, ncnt: int = 1) -> List[dict]:
        """개별 종목 분봉 차트 조회 (TR: ka10081)"""
        url = f"{self.base_url}/api/dostk/chart"
        headers = {
            **self._headers(),
            "api-id": "ka10081",
        }
        payload = {
            "stk_code": stock_code,
            "ncnt": ncnt,
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        data = resp.json()
        raw = data.get("output1", data.get("output", []))
        return raw if isinstance(raw, list) else []

    # ---------- 주문 ----------
    def place_order(self, isin_code: str, quantity: int, side: str) -> dict:
        """시장가 주문 (side='BUY' 또는 'SELL')"""
        url = f"{self.base_url}/v1/orders"
        payload = {
            "account_no": self.account_no,
            "order_type": "03",
            "isin_code": isin_code,
            "quantity": quantity,
            "price": 0,
            "side": side,
        }
        resp = requests.post(url, headers=self._headers(), json=payload, timeout=10)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        return resp.json()


# ──────────────────────────────────────────────────────────────
# 2. 종목 스크리너 / 2단계 필터링 엔진
# ──────────────────────────────────────────────────────────────
class StockScreener:
    """
    Two-Stage Filtering Engine
    - Stage 1: 로컬 if문만으로 100 → 10~15개 압축 (API 0회)
    - Stage 2: 압축된 후보군만 분봉 차트 API 조회 → 점수 누적
    """

    UPPER_LIMIT_PCT = 30
    STAGE1_MAX = 15

    def __init__(self):
        self.scores: Dict[str, int] = defaultdict(int)
        self.stock_info: Dict[str, dict] = {}

    def reset(self):
        self.scores.clear()
        self.stock_info.clear()

    # ── Stage 1: 로컬 필터링 (API 호출 0회) ──
    def stage1_local_filter(self, stocks: List[dict]) -> List[dict]:
        """가격·등락률 조건으로 100종목 → 10~15개 압축"""
        candidates = []
        for s in stocks:
            code = str(s.get("mk_code", ""))
            if not code:
                continue

            curr_prc = float(s.get("curr_prc", 0))
            flrt = float(s.get("flrt", 0))
            trd_amt = float(s.get("trd_amt", 0))
            high_prc = float(s.get("high_prc", 0))

            self.stock_info[code] = {
                "curr_prc": curr_prc,
                "trd_amt": trd_amt,
                "flrt": flrt,
                "high_prc": high_prc,
            }

            if not (2_000 <= curr_prc <= 25_000):
                continue

            upper_limit_price = curr_prc / (1 + flrt / 100) * (1 + self.UPPER_LIMIT_PCT / 100) \
                if flrt != -100 else curr_prc * 1.3
            if upper_limit_price > 0 and curr_prc > upper_limit_price * 0.95:
                continue

            candidates.append({
                "code": code,
                "curr_prc": curr_prc,
                "trd_amt": trd_amt,
                "flrt": flrt,
                "high_prc": high_prc,
            })

        candidates.sort(key=lambda x: x["trd_amt"], reverse=True)
        return candidates[:self.STAGE1_MAX]

    # ── Stage 2: 분봉 차트 기반 정밀 스크리닝 ──
    @staticmethod
    def _parse_chart_candle(row: dict) -> dict:
        """분봉 캔들 1건 파싱"""
        def _n(key: str) -> float:
            v = row.get(key, 0)
            if isinstance(v, str):
                v = v.replace(",", "").strip()
            try:
                return float(v)
            except (ValueError, TypeError):
                return 0.0

        return {
            "open": _n("open") or _n("strt_prc") or _n("open_prc"),
            "high": _n("high") or _n("high_prc"),
            "low": _n("low") or _n("low_prc"),
            "close": _n("close") or _n("curr_prc") or _n("cls_prc"),
            "volume": _n("volume") or _n("trd_qty") or _n("acml_vol"),
        }

    @staticmethod
    def _check_chart_conditions(candles: List[dict]) -> bool:
        """이동평균 정배열 + 장대양봉 존재 여부 판정"""
        if len(candles) < 3:
            return False

        closes = [c["close"] for c in candles if c["close"] > 0]
        if len(closes) < 3:
            return False

        ma1 = closes[-1]
        ma3 = sum(closes[-3:]) / 3
        if ma1 < ma3:
            return False

        for candle in candles[-2:]:
            o, c, vol = candle["open"], candle["close"], candle["volume"]
            if o <= 0 or c <= 0:
                continue
            body_pct = (c - o) / o * 100
            if body_pct >= 1.5 and vol > 0:
                return True

        return False

    def stage2_chart_filter(
        self, candidates: List[dict], api: "KiwoomAPI"
    ) -> List[str]:
        """후보군에 대해 분봉 API 조회 → 조건 충족 종목 코드 반환"""
        passed = []
        for cand in candidates:
            code = cand["code"]
            try:
                raw_candles = api.fetch_minute_chart(code, ncnt=1)
                candles = [self._parse_chart_candle(r) for r in raw_candles[:10]]
                if self._check_chart_conditions(candles):
                    passed.append(code)
            except Exception:
                pass
            time.sleep(0.05)
        return passed

    def add_score(self, codes: List[str]):
        """조건 통과 종목에 점수 1점 가산"""
        for code in codes:
            self.scores[code] += 1

    def top_candidate(self, blacklist: Set[str]) -> Optional[Tuple[str, dict]]:
        """블랙리스트 제외 후 누적 점수 최고 종목 반환"""
        candidates = {
            code: score
            for code, score in self.scores.items()
            if code not in blacklist and score > 0
        }
        if not candidates:
            return None
        best_code = max(candidates, key=candidates.get)
        return best_code, self.stock_info.get(best_code, {})


# ──────────────────────────────────────────────────────────────
# 3. 포트폴리오 / 포지션 관리
# ──────────────────────────────────────────────────────────────
class Portfolio:
    """보유 종목 + 자산 관리"""

    def __init__(self, initial_cash: int):
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.holdings: Dict[str, dict] = {}  # code → {qty, avg_price}

    def buy(self, code: str, qty: int, price: float):
        cost = qty * price
        self.cash -= cost
        if code in self.holdings:
            h = self.holdings[code]
            total_qty = h["qty"] + qty
            h["avg_price"] = (h["avg_price"] * h["qty"] + cost) / total_qty
            h["qty"] = total_qty
        else:
            self.holdings[code] = {"qty": qty, "avg_price": price}

    def sell(self, code: str, price: float) -> Tuple[int, float]:
        """전량 매도 → (매도 수량, 수익률%) 반환"""
        h = self.holdings.pop(code, None)
        if h is None:
            return 0, 0.0
        revenue = h["qty"] * price
        self.cash += revenue
        pnl_pct = (price - h["avg_price"]) / h["avg_price"] * 100
        return h["qty"], pnl_pct

    def total_eval(self, current_prices: Dict[str, float]) -> float:
        val = self.cash
        for code, h in self.holdings.items():
            val += h["qty"] * current_prices.get(code, h["avg_price"])
        return val

    def total_return_pct(self, current_prices: Dict[str, float]) -> float:
        total = self.total_eval(current_prices)
        if self.initial_cash == 0:
            return 0.0
        return (total - self.initial_cash) / self.initial_cash * 100

    def holdings_with_pnl(self, current_prices: Dict[str, float]) -> List[dict]:
        result = []
        for code, h in self.holdings.items():
            cp = current_prices.get(code, h["avg_price"])
            pnl = (cp - h["avg_price"]) / h["avg_price"] * 100
            result.append({
                "code": code,
                "qty": h["qty"],
                "avg_price": h["avg_price"],
                "curr_price": cp,
                "pnl_pct": pnl,
            })
        return result


# ──────────────────────────────────────────────────────────────
# 4. 매매 엔진 (백그라운드 스레드)
# ──────────────────────────────────────────────────────────────
class TradingEngine(threading.Thread):
    """설정된 시간 / 주기에 맞춰 매매 사이클을 실행하는 백그라운드 스레드"""

    def __init__(
        self,
        api: KiwoomAPI,
        portfolio: Portfolio,
        screener: StockScreener,
        start_time: str,
        end_time: str,
        interval_min: int,
        log_queue: queue.Queue,
        state_lock: threading.Lock,
    ):
        super().__init__(daemon=True)
        self.api = api
        self.portfolio = portfolio
        self.screener = screener
        self.start_hm = start_time
        self.end_hm = end_time
        self.interval_sec = interval_min * 60
        self.log_q = log_queue
        self.state_lock = state_lock

        self.running = True
        self.blacklist: Set[str] = set()
        self.current_prices: Dict[str, float] = {}
        self.next_cycle_end: Optional[datetime] = None
        self.cycle_phase = "대기"

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_q.put(f"[{ts}] {msg}")

    def _now_hm(self) -> str:
        return datetime.now().strftime("%H:%M")

    def _sell_all(self):
        """보유 종목 전량 매도"""
        with self.state_lock:
            codes = list(self.portfolio.holdings.keys())
        for code in codes:
            try:
                result = self.api.place_order(code, self.portfolio.holdings[code]["qty"], "SELL")
                curr = self.current_prices.get(code, self.portfolio.holdings[code]["avg_price"])
                with self.state_lock:
                    qty, pnl = self.portfolio.sell(code, curr)
                self.blacklist.add(code)
                self._log(
                    f"[매도 완료] 종목코드: {code} / 수량: {qty}주 / "
                    f"체결가: {curr:,.0f}원 (수익률: {pnl:+.2f}%)"
                )
            except Exception as e:
                self._log(f"[매도 오류] {code}: {e}")

    def _buy(self, code: str, price: float):
        """투자금 한도 내 시장가 매수"""
        if price <= 0:
            return
        qty = int(config.INVESTMENT_AMOUNT // price)
        if qty <= 0:
            self._log(f"[매수 스킵] {code}: 단가 {price:,.0f}원 — 수량 부족")
            return
        try:
            self.api.place_order(code, qty, "BUY")
            with self.state_lock:
                self.portfolio.buy(code, qty, price)
            self._log(
                f"[매수 완료] 종목코드: {code} / 수량: {qty}주 / "
                f"체결가: {price:,.0f}원"
            )
        except Exception as e:
            self._log(f"[매수 오류] {code}: {e}")

    @staticmethod
    def _parse_stock_row(row: dict) -> Optional[dict]:
        """API 응답 1건을 내부 표준 포맷으로 변환"""
        code = str(
            row.get("mk_code", "")
            or row.get("stk_code", "")
            or row.get("shcode", "")
        ).strip()
        if not code:
            return None

        def _num(key: str) -> float:
            v = row.get(key, 0)
            if isinstance(v, str):
                v = v.replace(",", "").strip()
            try:
                return float(v)
            except (ValueError, TypeError):
                return 0.0

        return {
            "mk_code": code,
            "curr_prc": _num("curr_prc") or _num("price") or _num("close"),
            "trd_amt": _num("trd_amt") or _num("value"),
            "flrt": _num("flrt") or _num("change_rate") or _num("drate"),
            "high_prc": _num("high_prc") or _num("high"),
        }

    def _fetch_and_score(self):
        """1분마다 호출 — 2단계 필터링 파이프라인"""
        t0 = time.time()
        try:
            # API 1회: 거래대금 상위 100종목
            raw_stocks = self.api.fetch_volume_rank()
            stocks = []
            for row in raw_stocks:
                parsed = self._parse_stock_row(row)
                if parsed:
                    stocks.append(parsed)

            with self.state_lock:
                for s in stocks:
                    self.current_prices[s["mk_code"]] = s["curr_prc"]

            # Stage 1: 로컬 필터 (API 0회)
            stage1 = self.screener.stage1_local_filter(stocks)
            s1_codes = [c["code"] for c in stage1]

            # Stage 2: 분봉 차트 정밀 검증 (API N회, N <= 15)
            passed = self.screener.stage2_chart_filter(stage1, self.api)
            self.screener.add_score(passed)

            elapsed = time.time() - t0
            self._log(
                f"[Screening] {len(stocks)} fetched -> "
                f"S1: {len(s1_codes)} -> S2: {len(passed)} passed "
                f"({elapsed:.1f}s)"
            )
        except Exception as e:
            self._log(f"[Fetch Error] {e}")

    def run(self):
        self._log("[시스템] 매매 엔진 시작 — 토큰 발급 중...")
        try:
            self.api.get_access_token()
            self._log("[시스템] 토큰 발급 완료")
        except Exception as e:
            self._log(f"[시스템 오류] 토큰 발급 실패: {e}")
            return

        # 시작 시간까지 대기
        while self.running:
            if self._now_hm() >= self.start_hm:
                break
            self.cycle_phase = "시작 대기"
            time.sleep(1)

        self._log(f"[시스템] 매매 시작 (종료 예정: {self.end_hm})")

        # ── 메인 매매 루프 ──
        while self.running:
            if self._now_hm() >= self.end_hm:
                break

            cycle_start = datetime.now()
            cycle_end = cycle_start + timedelta(seconds=self.interval_sec)
            self.next_cycle_end = cycle_end

            self.screener.reset()
            self.blacklist.clear()
            self.cycle_phase = "스크리닝"

            # 주기 내 1분 단위 스크리닝 (종료 15초 전까지)
            screening_deadline = cycle_end - timedelta(seconds=15)
            last_fetch = datetime.min
            while self.running and datetime.now() < screening_deadline:
                if self._now_hm() >= self.end_hm:
                    break
                if (datetime.now() - last_fetch).total_seconds() >= 60:
                    self._fetch_and_score()
                    last_fetch = datetime.now()
                time.sleep(1)

            if not self.running or self._now_hm() >= self.end_hm:
                break

            # 주기 종료 15초 전: 선매도
            self.cycle_phase = "선매도"
            self._sell_all()

            # 종목 확정 (14초 전 ~ 1초 전)
            self.cycle_phase = "종목 확정"
            candidate = self.screener.top_candidate(self.blacklist)

            # 정확한 주기 정각까지 대기
            while self.running and datetime.now() < cycle_end:
                time.sleep(0.2)

            # 정각: 매수
            if candidate:
                best_code, info = candidate
                price = self.current_prices.get(best_code, info.get("curr_prc", 0))
                self._log(f"[종목 선정] {best_code} (점수: {self.screener.scores.get(best_code, 0)})")
                self._buy(best_code, price)
            else:
                self._log("[스킵] 이번 주기 매수 후보 없음")

        # 종료 처리
        self.cycle_phase = "종료"
        self._log("[SYSTEM] End time reached - trading loop finished")
        self.running = False


# ──────────────────────────────────────────────────────────────
# 5. GUI — 설정창
# ──────────────────────────────────────────────────────────────
class SettingsWindow:
    """프로그램 시작 시 표시되는 설정 입력창"""

    def __init__(self):
        self.result: Optional[dict] = None

        self.root = tk.Tk()
        self.root.title("KOSDAQ Auto Trader - Settings")
        self.root.resizable(False, False)
        self.root.configure(bg="#1e1e2e")
        self._center(380, 320)

        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Title.TLabel", font=("Malgun Gothic", 16, "bold"),
                         foreground="#cdd6f4", background="#1e1e2e")
        style.configure("Sub.TLabel", font=("Malgun Gothic", 10),
                         foreground="#a6adc8", background="#1e1e2e")
        style.configure("TLabel", font=("Malgun Gothic", 11),
                         foreground="#cdd6f4", background="#1e1e2e")
        style.configure("TEntry", font=("Malgun Gothic", 12))
        style.configure("Accent.TButton", font=("Malgun Gothic", 12, "bold"),
                         foreground="#1e1e2e", background="#a6e3a1", padding=8)
        style.map("Accent.TButton",
                  background=[("active", "#94e2d5")])
        style.configure("TFrame", background="#1e1e2e")

        frame = ttk.Frame(self.root, padding=24)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="코스닥 단타 자동매매", style="Title.TLabel").pack(pady=(0, 2))
        ttk.Label(frame, text="키움 REST API · tkinter 대시보드", style="Sub.TLabel").pack(pady=(0, 16))

        row = ttk.Frame(frame)
        row.pack(fill="x", pady=4)
        ttk.Label(row, text="시작 시간 (HH:MM)").pack(side="left")
        self.entry_start = ttk.Entry(row, width=8)
        self.entry_start.insert(0, "09:00")
        self.entry_start.pack(side="right")

        row2 = ttk.Frame(frame)
        row2.pack(fill="x", pady=4)
        ttk.Label(row2, text="종료 시간 (HH:MM)").pack(side="left")
        self.entry_end = ttk.Entry(row2, width=8)
        self.entry_end.insert(0, "15:20")
        self.entry_end.pack(side="right")

        row3 = ttk.Frame(frame)
        row3.pack(fill="x", pady=4)
        ttk.Label(row3, text="매매 주기 (분)").pack(side="left")
        self.entry_interval = ttk.Entry(row3, width=8)
        self.entry_interval.insert(0, "20")
        self.entry_interval.pack(side="right")

        ttk.Button(frame, text="매매 시작", style="Accent.TButton",
                   command=self._on_start).pack(pady=(20, 0), fill="x")

    def _center(self, w: int, h: int):
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")

    def _validate(self) -> bool:
        try:
            s = self.entry_start.get().strip()
            e = self.entry_end.get().strip()
            i = self.entry_interval.get().strip()
            datetime.strptime(s, "%H:%M")
            datetime.strptime(e, "%H:%M")
            iv = int(i)
            if iv <= 0:
                raise ValueError
            if s >= e:
                messagebox.showerror("입력 오류", "시작 시간이 종료 시간보다 빨라야 합니다.")
                return False
            return True
        except ValueError:
            messagebox.showerror("입력 오류", "올바른 형식으로 입력해 주세요.\n시간: HH:MM / 주기: 양의 정수")
            return False

    def _on_start(self):
        if not self._validate():
            return
        self.result = {
            "start_time": self.entry_start.get().strip(),
            "end_time": self.entry_end.get().strip(),
            "interval_min": int(self.entry_interval.get().strip()),
        }
        self.root.destroy()

    def show(self) -> Optional[dict]:
        self.root.mainloop()
        return self.result


# ──────────────────────────────────────────────────────────────
# 6. GUI — 실시간 대시보드
# ──────────────────────────────────────────────────────────────
class DashboardWindow:
    """매매 중 실시간 상태 모니터링 대시보드"""

    REFRESH_MS = 1_000

    def __init__(
        self,
        engine: TradingEngine,
        portfolio: Portfolio,
        log_queue: queue.Queue,
        state_lock: threading.Lock,
    ):
        self.engine = engine
        self.portfolio = portfolio
        self.log_q = log_queue
        self.state_lock = state_lock

        self.root = tk.Tk()
        self.root.title("KOSDAQ Auto Trading Dashboard v1.0")
        self.root.configure(bg="#1e1e2e")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._center(720, 680)

        self._build_ui()

    def _center(self, w: int, h: int):
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")

    def _build_ui(self):
        BG = "#1e1e2e"
        FG = "#cdd6f4"
        ACCENT = "#a6e3a1"
        DIM = "#585b70"
        FONT = ("Malgun Gothic", 10)
        FONT_BOLD = ("Malgun Gothic", 10, "bold")
        MONO = ("Malgun Gothic", 10)

        # ── 상단 요약 ──
        top = tk.Frame(self.root, bg=BG, padx=16, pady=12)
        top.pack(fill="x")

        top_header = tk.Frame(top, bg=BG)
        top_header.pack(fill="x")

        self.lbl_phase = tk.Label(top_header, text="Status: Ready", fg=ACCENT, bg=BG, font=FONT_BOLD, anchor="w")
        self.lbl_phase.pack(side="left", fill="x", expand=True)

        self.btn_stop = tk.Button(
            top_header, text="STOP", command=self._stop_trading,
            bg="#d9534f", fg="white", activebackground="#c9302c", activeforeground="white",
            font=("Malgun Gothic", 10, "bold"), relief="flat", padx=16, pady=4, cursor="hand2",
        )
        self.btn_stop.pack(side="right")

        summary = tk.Frame(top, bg=BG)
        summary.pack(fill="x", pady=(6, 0))

        self.lbl_total = tk.Label(summary, text="Total: --", fg=FG, bg=BG, font=FONT, anchor="w")
        self.lbl_total.pack(side="left", padx=(0, 24))

        self.lbl_return = tk.Label(summary, text="P&L: --", fg=FG, bg=BG, font=FONT, anchor="w")
        self.lbl_return.pack(side="left", padx=(0, 24))

        timer_row = tk.Frame(top, bg=BG)
        timer_row.pack(fill="x", pady=(6, 0))

        self.lbl_next_time = tk.Label(
            timer_row, text="Next Trade: --:--:--", fg=FG, bg=BG, font=FONT, anchor="w",
        )
        self.lbl_next_time.pack(side="left", padx=(0, 24))

        self.lbl_countdown = tk.Label(
            timer_row, text="Remaining: --m --s", fg=DIM, bg=BG, font=FONT_BOLD, anchor="w",
        )
        self.lbl_countdown.pack(side="left", padx=(0, 24))

        # 구분선
        tk.Frame(self.root, bg=DIM, height=1).pack(fill="x", padx=16)

        # ── 보유 종목 테이블 ──
        table_frame = tk.Frame(self.root, bg=BG, padx=16, pady=8)
        table_frame.pack(fill="both", expand=True)

        tk.Label(table_frame, text="Holdings", fg=FG, bg=BG, font=FONT_BOLD, anchor="w").pack(fill="x")

        cols = ("code", "qty", "avg_price", "curr_price", "pnl_pct")
        self.tree = ttk.Treeview(table_frame, columns=cols, show="headings", height=6)
        for cid, heading, w in [
            ("code", "Code", 100),
            ("qty", "Qty", 60),
            ("avg_price", "Avg Price", 100),
            ("curr_price", "Cur Price", 100),
            ("pnl_pct", "P&L(%)", 100),
        ]:
            self.tree.heading(cid, text=heading)
            self.tree.column(cid, width=w, anchor="center")

        style = ttk.Style()
        style.configure("Treeview", background="#313244", foreground=FG,
                         fieldbackground="#313244", font=MONO, rowheight=24)
        style.configure("Treeview.Heading", background="#45475a", foreground=FG,
                         font=FONT_BOLD)

        self.tree.pack(fill="both", expand=True, pady=(4, 0))

        # 구분선
        tk.Frame(self.root, bg=DIM, height=1).pack(fill="x", padx=16)

        # ── 거래 로그 ──
        log_frame = tk.Frame(self.root, bg=BG, padx=16, pady=8)
        log_frame.pack(fill="both", expand=True)

        tk.Label(log_frame, text="Trade Log", fg=FG, bg=BG, font=FONT_BOLD, anchor="w").pack(fill="x")

        self.log_box = scrolledtext.ScrolledText(
            log_frame, height=10, wrap="word",
            bg="#181825", fg="#a6adc8", font=MONO,
            insertbackground=FG, relief="flat", borderwidth=0,
            state="disabled", exportselection=True,
            selectbackground="#45475a", selectforeground="#cdd6f4",
        )
        self.log_box.pack(fill="both", expand=True, pady=(4, 0))

        self.log_box.bind("<Key>", self._block_typing)
        self.log_box.bind("<Control-c>", self._copy_selection)
        self.log_box.bind("<Control-C>", self._copy_selection)

        self._log_context_menu = tk.Menu(self.root, tearoff=0,
                                         bg="#313244", fg="#cdd6f4",
                                         activebackground="#45475a",
                                         activeforeground="#cdd6f4",
                                         font=MONO)
        self._log_context_menu.add_command(label="Copy", command=self._copy_selection)
        self.log_box.bind("<Button-3>", self._show_log_context_menu)

    # ── 주기적 갱신 ──
    def _update(self):
        if not self.engine.running and self.engine.cycle_phase == "종료":
            self._drain_logs()
            self.lbl_phase.config(text="Status: Finished")
            self.btn_stop.config(state="disabled", bg="#585b70")
            return

        with self.state_lock:
            prices = dict(self.engine.current_prices)
            holdings = self.portfolio.holdings_with_pnl(prices)
            total_eval = self.portfolio.total_eval(prices)
            total_ret = self.portfolio.total_return_pct(prices)

        phase_map = {
            "대기": "Idle", "시작 대기": "Waiting",
            "스크리닝": "Screening", "선매도": "Pre-Sell",
            "종목 확정": "Selecting", "종료 매도": "Final Sell",
            "종료": "Finished",
        }
        phase_en = phase_map.get(self.engine.cycle_phase, self.engine.cycle_phase)
        self.lbl_phase.config(text=f"Status: {phase_en}")
        self.lbl_total.config(text=f"Total: {total_eval:,.0f} KRW")
        ret_color = "#a6e3a1" if total_ret >= 0 else "#f38ba8"
        self.lbl_return.config(text=f"P&L: {total_ret:+.2f}%", fg=ret_color)

        if self.engine.next_cycle_end:
            target_str = self.engine.next_cycle_end.strftime("%H:%M:%S")
            self.lbl_next_time.config(text=f"Next Trade: {target_str}")

            remain = (self.engine.next_cycle_end - datetime.now()).total_seconds()
            remain = max(remain, 0)
            rm, rs = divmod(int(remain), 60)
            self.lbl_countdown.config(text=f"Remaining: {rm}m {rs:02d}s")

            if remain < 60:
                self.lbl_countdown.config(fg="#f0ad4e")
            else:
                self.lbl_countdown.config(fg="#cdd6f4")
        else:
            self.lbl_next_time.config(text="Next Trade: --:--:--")
            self.lbl_countdown.config(text="Remaining: --m --s", fg="#585b70")

        # 보유 종목 테이블
        self.tree.delete(*self.tree.get_children())
        for h in holdings:
            pnl_str = f"{h['pnl_pct']:+.2f}"
            self.tree.insert("", "end", values=(
                h["code"], h["qty"],
                f"{h['avg_price']:,.0f}",
                f"{h['curr_price']:,.0f}",
                pnl_str,
            ))

        self._drain_logs()
        self.root.after(self.REFRESH_MS, self._update)

    def _block_typing(self, event):
        """읽기 전용 유지 — 방향키·Ctrl 조합 등 탐색 키만 허용"""
        allow = {
            "Left", "Right", "Up", "Down", "Home", "End",
            "Prior", "Next",  # Page Up / Page Down
        }
        if event.keysym in allow:
            return None
        if event.state & 0x4 and event.keysym.lower() in ("c", "a"):
            return None
        return "break"

    def _copy_selection(self, event=None):
        """선택된 텍스트를 클립보드에 복사"""
        try:
            selected = self.log_box.get("sel.first", "sel.last")
            self.root.clipboard_clear()
            self.root.clipboard_append(selected)
        except tk.TclError:
            pass
        return "break"

    def _show_log_context_menu(self, event):
        """우클릭 컨텍스트 메뉴 표시"""
        self._log_context_menu.post(event.x_root, event.y_root)

    def _drain_logs(self):
        """큐에 쌓인 로그 메시지를 GUI 텍스트 박스에 반영 (선택 영역 보존)"""
        has_new = False
        sel_range = None
        try:
            sel_range = (self.log_box.index("sel.first"), self.log_box.index("sel.last"))
        except tk.TclError:
            pass

        self.log_box.config(state="normal")
        while True:
            try:
                msg = self.log_q.get_nowait()
            except queue.Empty:
                break
            self.log_box.insert("end", msg + "\n")
            has_new = True

        if sel_range:
            self.log_box.tag_add("sel", sel_range[0], sel_range[1])

        if has_new:
            self.log_box.see("end")
        self.log_box.config(state="disabled")

    def _stop_trading(self):
        """STOP 버튼 콜백 — 안전한 종료 시퀀스"""
        self.btn_stop.config(state="disabled", bg="#585b70", text="STOPPING...")

        def _shutdown():
            self.engine.running = False
            self.engine._log("[SYSTEM] Trading stopped by user")
            self.engine.join(timeout=5)
            self.engine.cycle_phase = "종료"
            self._drain_logs()
            self.root.destroy()
            sys.exit(0)

        threading.Thread(target=_shutdown, daemon=True).start()

    def _on_close(self):
        """윈도우 X 버튼 — _stop_trading 과 동일한 안전 종료"""
        self._stop_trading()

    def run(self):
        self.root.after(500, self._update)
        self.root.mainloop()


# ──────────────────────────────────────────────────────────────
# 7. 엔트리포인트
# ──────────────────────────────────────────────────────────────
def main():
    # 설정창
    settings = SettingsWindow().show()
    if settings is None:
        return

    # 공유 객체 초기화
    api = KiwoomAPI()
    portfolio = Portfolio(initial_cash=config.INVESTMENT_AMOUNT)
    screener = StockScreener()
    log_queue: queue.Queue[str] = queue.Queue()
    state_lock = threading.Lock()

    # 매매 엔진 (백그라운드 스레드)
    engine = TradingEngine(
        api=api,
        portfolio=portfolio,
        screener=screener,
        start_time=settings["start_time"],
        end_time=settings["end_time"],
        interval_min=settings["interval_min"],
        log_queue=log_queue,
        state_lock=state_lock,
    )
    engine.start()

    # 대시보드 (메인 스레드)
    dashboard = DashboardWindow(engine, portfolio, log_queue, state_lock)
    dashboard.run()


if __name__ == "__main__":
    main()
