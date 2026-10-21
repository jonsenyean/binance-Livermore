# -*- coding: utf-8 -*-
"""自动策略模块。

本文件包含两个可切换的策略（config.STRATEGY）：

1. TrendPyramidStrategy —— 趋势跟随 + 浮盈加仓（原策略）：
   EMA 快慢线识别趋势 → 固定保证金建仓 → 新高后回调用浮盈加仓 → 止盈/止损/移动止盈/趋势反转离场

2. LivermoreRollStrategy —— 利弗莫尔战法（默认策略）：
   关键点（前高/前低）突破进场 → 试探建仓 → 有盈利且创新高时逐级金字塔加仓（只追强势、不接回调）
   → 关键点止损 + 宽松移动止盈「让利润奔跑」
"""
from __future__ import annotations

import math
import time

import config


def ema(values: list[float], period: int) -> float | None:
    """指数移动平均（从序列起点递推，结果与逐点增量式一致）。"""
    if not values or period <= 0:
        return None
    k = 2.0 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def append_journal(symbol: str, pnl: float, reason: str):
    """把一轮平仓追加进 CSV 交易日志（config.TRADE_JOURNAL），供跨会话复盘胜率/盈亏比。

    utf-8-sig 带 BOM：Excel 直接双击打开中文不乱码；写失败静默，绝不影响交易。
    """
    path = getattr(config, "TRADE_JOURNAL", "")
    if not path:
        return
    try:
        import csv
        import os
        new_file = not os.path.exists(path)
        with open(path, "a", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(["时间", "交易对", "盈亏USDT", "离场原因"])
            w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), symbol, f"{pnl:.4f}", reason])
    except Exception:
        pass


class TrendPyramidStrategy:
    name = "trend_pyramid"

    def __init__(self):
        # ---- 趋势识别 ----
        self.base_margin_usd = config.TREND_POSITION_MARGIN_USD  # 首仓固定保证金
        self.base_usd = self.base_margin_usd * config.LEVERAGE    # 估算名义，仅供参考
        self.fast_ma = config.TREND_FAST_MA
        self.slow_ma = config.TREND_SLOW_MA
        self.min_candles = config.TREND_MIN_CANDLES
        self.deadband = config.TREND_DEADBAND_PCT
        self.max_candles = config.TREND_MAX_CANDLES

        # ---- 浮盈加仓 ----
        self.add_ratio = config.TREND_PYRAMID_RATIO
        self.min_add_usd = config.TREND_MIN_ADD_USD
        self.max_adds = config.TREND_MAX_ADDS
        self.pullback_pct = config.TREND_PULLBACK_PCT

        # ---- 离场 ----
        self.tp = config.TREND_TAKE_PROFIT
        self.sl = config.TREND_STOP_LOSS
        self.time_limit = config.TREND_TIME_LIMIT
        self.trail_activate = config.TREND_TRAIL_ACTIVATE_PCT
        self.trail_stop = config.TREND_TRAIL_STOP_PCT
        self.cooldown = config.TREND_COOLDOWN_SECONDS
        self.loss_cooldown = config.TREND_LOSS_COOLDOWN_SECONDS
        self.loss_streak_ban = config.TREND_LOSS_STREAK_BAN
        self.loss_streak_mult = config.TREND_LOSS_STREAK_MULT
        self.loss_streak_max_cooldown = config.TREND_LOSS_STREAK_MAX_COOLDOWN

        # ---- 运行时状态 ----
        self.candles: list[dict] = []   # 已收盘 1m K 线 {ts, open, high, low, close}
        self.building: dict | None = None
        self.state = "IDLE"             # IDLE / HOLDING
        self.add_count = 0              # 已加仓次数
        self.arm_for_add = False        # 是否已创出新高（可触发加仓）
        self.extreme = 0.0              # 加仓基准：多=近期最高价，空=近期最低价
        self.trail_peak = 0.0           # 移动止盈峰值（多=最高，空=最低）
        self.trail_active = False
        self.cooldown_until = 0.0
        self.loss_streak = 0            # 连续亏损离场次数（盈利离场清零）
        self.stats = self._new_stats()  # 战绩统计（胜率/盈亏比/连败，按平仓轮计）

    @staticmethod
    def _new_stats() -> dict:
        """新建战绩统计：gross_loss 为负值累加，gross_win + gross_loss = 净盈亏。"""
        return {"cycles": 0, "wins": 0, "losses": 0, "gross_win": 0.0,
                "gross_loss": 0.0, "cur_streak": 0, "max_loss_streak": 0}

    def reset(self):
        """切换交易对/重新初始化时重置策略内部状态。"""
        self.candles.clear()
        self.building = None
        self.state = "IDLE"
        self.add_count = 0
        self.arm_for_add = False
        self.extreme = 0.0
        self.trail_peak = 0.0
        self.trail_active = False
        self.cooldown_until = 0.0
        self.loss_streak = 0
        self.stats = self._new_stats()

    # ------------------------------------------------------------------
    # K 线累积与趋势识别
    # ------------------------------------------------------------------
    def seed_candles(self, rows: list[dict]):
        """注入历史 1m K 线（[{ts, open?, high, low, close}]），让均线信号立即可用。"""
        seen = set()
        merged = []
        for r in sorted(rows, key=lambda x: x["ts"]):
            ts = int(r["ts"])
            if ts in seen:
                continue
            seen.add(ts)
            close = float(r["close"])
            merged.append({
                "ts": ts,
                "open": float(r.get("open", close)),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": close,
            })
        self.candles = merged[-self.max_candles:]
        self.building = None

    def _update_candle(self, price: float, ts: float):
        """把一次行情价格累加进 1 分钟 K 线；跨分钟时收盘旧 K 线。"""
        minute = int(ts // 60)
        if self.building is None:
            self.building = {"ts": minute * 60, "open": price, "high": price,
                             "low": price, "close": price}
            return
        if minute != int(self.building["ts"] // 60):
            self.candles.append(self.building)
            if len(self.candles) > self.max_candles:
                self.candles.pop(0)
            self.building = {"ts": minute * 60, "open": price, "high": price,
                             "low": price, "close": price}
            return
        self.building["high"] = max(self.building["high"], price)
        self.building["low"] = min(self.building["low"], price)
        self.building["close"] = price

    def trend(self) -> str:
        """趋势: "UP" / "DOWN" / "FLAT"。均线未成形或差值过小返回 "FLAT"。"""
        closes = [c["close"] for c in self.candles]
        if len(closes) < self.min_candles:
            return "FLAT"
        fast = ema(closes, self.fast_ma)
        slow = ema(closes, self.slow_ma)
        if fast is None or slow is None or slow == 0:
            return "FLAT"
        diff = (fast - slow) / slow
        if diff > self.deadband:
            return "UP"
        if diff < -self.deadband:
            return "DOWN"
        return "FLAT"

    # ------------------------------------------------------------------
    # 建仓 / 加仓
    # ------------------------------------------------------------------
    def _size_qty(self, app, notional: float) -> float:
        """按名义价值换算数量，向下取整到 step；保证成交名义 ≥ 该对最小下单。"""
        ref = app.last_price or app.mark
        if ref is None or ref <= 0:
            return 0.0
        step = app.account.step_size
        qty = math.floor(notional / ref / step + 1e-9) * step
        if qty * ref < app.account.min_notional:
            qty = math.ceil(app.account.min_notional / ref / step) * step
        return qty

    def _start_position(self, app, side: str):
        """趋势明确后首次建仓。开仓名义 = 固定保证金 × 当前杠杆（保证金固定，杠杆可变）。"""
        ref = app.last_price or app.mark
        if ref is None or ref <= 0:
            return
        notional = self.base_margin_usd * app.account.leverage
        total = max(notional, app.account.min_notional)
        qty = self._size_qty(app, total)
        if qty <= 0:
            return
        ok, msg = app.account.open_position(side, qty, app.bid, app.ask)
        print(f"[趋势策略] ★ 识别到{'上涨' if side == 'LONG' else '下跌'}趋势，建仓 {side} "
              f"{qty:.4f}（名义≈{total:.2f} USDT，目标保证金 {self.base_margin_usd:.2f} USDT）-> {msg}")
        if ok:
            self._on_position_started(app, first=True)

    def _pyramid_add(self, app):
        """浮盈加仓：用当前浮盈扩大同向仓位。"""
        pos = app.account.position
        if pos is None or self.add_count >= self.max_adds:
            return
        pnl = pos.unrealized_pnl(app.mark)
        if pnl <= 0 or app.account.cash <= 0:
            return
        # 加仓名义 = 浮盈 × 比例；但至少覆盖交易所最小下单，保证「用盈利加仓」真实可成交
        floor = max(self.min_add_usd, app.account.min_notional)
        add_notional = max(floor, pnl * self.add_ratio)
        # 加仓所需保证金（名义/杠杆）不能超过可用余额
        add_notional = min(add_notional, app.account.cash * pos.leverage)
        qty = self._size_qty(app, add_notional)
        if qty <= 0:
            return
        ok, msg = app.account.open_position(pos.side, qty, app.bid, app.ask)
        print(f"[趋势策略] 浮盈 {pnl:+.2f} USDT，回调加仓 {pos.side} {qty:.4f}"
              f"（名义≈{add_notional:.2f} USDT）-> {msg}")
        if ok:
            self.add_count += 1
            self._on_position_started(app, first=False)

    def _on_position_started(self, app, first: bool):
        """初始化一轮交易的加仓/移动止盈基准（建仓或加仓后调用）。"""
        close = app.mark if app.mark is not None else app.last_price
        self.state = "HOLDING"
        self.extreme = close
        self.arm_for_add = False
        if first:
            self.add_count = 0
            self.trail_peak = close
            self.trail_active = False
        else:
            # 加仓发生在回调低点，移动止盈峰值不回退
            self.trail_peak = max(self.trail_peak, close)

    # ------------------------------------------------------------------
    # 主评估循环
    # ------------------------------------------------------------------
    def on_eval(self, app):
        # 行情未就绪（盘口/标记价未到位）时不评估，避免用空价格下单
        if app.bid is None or app.ask is None or app.mark is None:
            return
        price = app.mark
        self._update_candle(price, time.time())

        pos = app.account.position
        if pos is not None:
            if self.state == "IDLE":
                self._on_position_started(app, first=True)   # 采纳外部开仓
            self._update_extremes(app, pos)
            if self._check_exit(app, pos):
                return
            if self._pullback_triggered(app, pos):
                self._pyramid_add(app)
            return

        # 无持仓
        if self.state == "HOLDING":
            trades = app.account.trades
            self._finish_cycle(trades[-1].realized if trades else None,
                               reason="外部平仓", app=app)   # 外部平仓/强平按实际盈亏定性冷却
        if time.time() < self.cooldown_until:
            return
        trend = self.trend()
        if trend == "UP":
            self._start_position(app, "LONG")
        elif trend == "DOWN":
            self._start_position(app, "SHORT")

    def _update_extremes(self, app, pos):
        """跟踪加仓基准（新高/新低）与移动止盈峰值。"""
        close = app.mark if app.mark is not None else app.last_price
        if close is None:
            return
        if pos.side == "LONG":
            if close > self.extreme:
                self.extreme = close
                self.arm_for_add = True
            if close > self.trail_peak:
                self.trail_peak = close
            if not self.trail_active and close >= pos.entry_price * (1 + self.trail_activate):
                self.trail_active = True
        else:
            if close < self.extreme:
                self.extreme = close
                self.arm_for_add = True
            if close < self.trail_peak:
                self.trail_peak = close
            if not self.trail_active and close <= pos.entry_price * (1 - self.trail_activate):
                self.trail_active = True

    def _pullback_triggered(self, app, pos) -> bool:
        """加仓已解锁（创新高）后，回撤达到阈值即触发。"""
        close = app.mark if app.mark is not None else app.last_price
        if close is None or not self.arm_for_add:
            return False
        if pos.side == "LONG":
            return close <= self.extreme * (1 - self.pullback_pct)
        return close >= self.extreme * (1 + self.pullback_pct)

    def _check_exit(self, app, pos) -> bool:
        """止盈/止损/移动止盈/趋势反转/时限检查。命中则平仓并结束本轮。"""
        margin = pos.margin
        pnl = pos.unrealized_pnl(app.mark)
        pnl_pct = pnl / margin if margin else 0.0

        if pnl_pct <= -self.sl:
            hit = "止损"
        elif pnl_pct >= self.tp:
            hit = "止盈"
        else:
            hit = None
        if hit:
            ok, msg = app.account.close_position(None, app.bid, app.ask, reason=f"{hit}-趋势策略")
            print(f"[趋势策略] {hit}（盈亏 {pnl_pct * 100:+.1f}%）-> {msg}")
            self._finish_cycle(pnl, reason=hit, app=app)
            return True

        # 移动止盈
        close = app.mark if app.mark is not None else app.last_price
        if self.trail_active and close is not None:
            if pos.side == "LONG":
                retrace = (self.trail_peak - close) / self.trail_peak if self.trail_peak else 0.0
            else:
                retrace = (close - self.trail_peak) / self.trail_peak if self.trail_peak else 0.0
            if retrace >= self.trail_stop:
                ok, msg = app.account.close_position(None, app.bid, app.ask, reason="移动止盈-趋势策略")
                print(f"[趋势策略] 移动止盈（峰值回撤 {retrace * 100:.2f}%）-> {msg}")
                self._finish_cycle(pnl, reason="移动止盈", app=app)
                return True

        # 趋势反转
        trend = self.trend()
        if (pos.side == "LONG" and trend == "DOWN") or (pos.side == "SHORT" and trend == "UP"):
            ok, msg = app.account.close_position(None, app.bid, app.ask, reason="趋势反转-趋势策略")
            print(f"[趋势策略] 趋势反转（{trend}）平仓 -> {msg}")
            self._finish_cycle(pnl, reason="趋势反转", app=app)
            return True

        # 时限
        if time.time() - pos.opened_at > self.time_limit:
            ok, msg = app.account.close_position(None, app.bid, app.ask, reason="时间限制-趋势策略")
            print(f"[趋势策略] 持仓超时平仓 -> {msg}")
            self._finish_cycle(pnl, reason="时间限制", app=app)
            return True
        return False

    def _finish_cycle(self, pnl: float | None = None, reason: str = "", app=None):
        """一轮交易结束：清空加仓/移动止盈状态，进入冷却期；并更新战绩统计、写交易日志。

        A 亏损冷却：亏损离场（pnl<0）用 loss_cooldown 替代普通冷却，专治止损后同一把刀反复割；
        B 连败降频：连败达 loss_streak_ban 次后冷却逐次乘 loss_streak_mult（越亏越冷静），
          封顶 loss_streak_max_cooldown；非亏损离场清零连败计数。
        pnl 为 None（外部平仓且无记录）不计入战绩。
        """
        self.state = "IDLE"
        self.add_count = 0
        self.arm_for_add = False
        self.extreme = 0.0
        self.trail_peak = 0.0
        self.trail_active = False
        loss = pnl is not None and pnl < 0
        escalation = 1.0
        if loss:
            self.loss_streak += 1
            escalation = self.loss_streak_mult ** max(0, self.loss_streak - self.loss_streak_ban + 1)
            cooldown = self.loss_cooldown * escalation
        else:
            self.loss_streak = 0
            cooldown = self.cooldown
        cooldown = min(cooldown, self.loss_streak_max_cooldown)
        self.cooldown_until = time.time() + cooldown
        if cooldown > 0:
            tag = f"（连败{self.loss_streak}次，冷却×{escalation:g}）" if loss and escalation > 1 else ""
            print(f"[冷却] {cooldown / 60:.0f} 分钟后恢复该对进场{tag}")
        # ---- 战绩统计 + 交易日志（pnl 未知不计） ----
        if pnl is not None:
            st = self.stats
            st["cycles"] += 1
            if pnl > 0:
                st["wins"] += 1
                st["gross_win"] += pnl
                st["cur_streak"] = 0
            else:
                st["losses"] += 1
                st["gross_loss"] += pnl
                st["cur_streak"] += 1
                st["max_loss_streak"] = max(st["max_loss_streak"], st["cur_streak"])
            append_journal(getattr(app, "symbol", ""), pnl, reason)

    # ------------------------------------------------------------------
    # 状态显示
    # ------------------------------------------------------------------
    def status_lines(self, app) -> list[str]:
        trend = self.trend()
        lines = [
            f"趋势加仓: 趋势 {trend} | 状态 {self.state} | "
            f"K线 {len(self.candles)} 根 | 已加仓 {self.add_count}/{self.max_adds}",
        ]
        pos = app.account.position
        if pos:
            pnl = pos.unrealized_pnl(app.mark) if app.mark else 0.0
            pct = pnl / pos.margin * 100 if pos.margin else 0.0
            lines.append(f"当前盈亏 {pct:+.1f}% | 移动止盈 {'开' if self.trail_active else '关'} | "
                         f"回调触发 {self.pullback_pct * 100:.1f}% | 剩余时限 "
                         f"{max(0, int(self.time_limit - (time.time() - pos.opened_at)))}s")
        st = self.stats
        if st["cycles"]:
            wr = st["wins"] / st["cycles"] * 100
            lines.append(f"战绩: {st['cycles']}轮 胜率 {wr:.0f}% | "
                         f"累计 {st['gross_win'] + st['gross_loss']:+.2f} U | 最大连败 {st['max_loss_streak']}")
        return lines


class LivermoreRollStrategy(TrendPyramidStrategy):
    """利弗莫尔战法策略（默认，config.STRATEGY="livermore_roll"）。

    全部采用利弗莫尔战法（无浮盈回调加仓）：
      1. 关键点突破进场：最近 LIVERMORE_PIVOT_PERIOD 根 1m K 线的最高/最低为关键点，
         价格突破关键点上沿（+缓冲）开多、跌破下沿开空（纯突破，可选均线同向过滤）
      2. 试探建仓：首仓用 LIVERMORE_PROBE_MARGIN_USD 试探仓 —— 利弗莫尔「先试探，行情证明你了再加」
      3. 利弗莫尔金字塔加仓：只在「有盈利 + 创新高」（比上次加仓价再突破
         LIVERMORE_ADD_BREAKOUT_PCT）时逐级顺势加仓 —— 只追强势、不接回调、逐级抬升，绝不摊平
      4. 关键点止损：跌破关键点止损位离场，且止损位只上移不下移（随关键点保护盈利）
      5. 让利润奔跑：移动止盈启动/回撤阈值比趋势策略更宽松，由移动止盈而非固定止盈离场
    """
    name = "livermore_roll"

    def __init__(self):
        super().__init__()
        # ---- 关键点 ----
        self.pivot_period = config.LIVERMORE_PIVOT_PERIOD
        self.breakout_pct = config.LIVERMORE_BREAKOUT_PCT
        self.require_trend = config.LIVERMORE_REQUIRE_TREND
        # ---- 试探仓：首仓保证金 = 试探仓（比原首仓更小） ----
        self.base_margin_usd = config.LIVERMORE_PROBE_MARGIN_USD
        self.base_usd = self.base_margin_usd * config.LEVERAGE
        # ---- 利弗莫尔金字塔加仓（只在有盈利且创新高时逐级顺势加仓） ----
        self.add_breakout_pct = config.LIVERMORE_ADD_BREAKOUT_PCT
        # ---- 关键点止损 / 利润保护（让利润奔跑：回吐峰值利润 + 保本止损） ----
        self.use_pivot_stop = config.LIVERMORE_USE_PIVOT_STOP
        self.trail_activate = config.LIVERMORE_TRAIL_ACTIVATE_PCT
        self.trail_giveback = config.LIVERMORE_TRAIL_GIVEBACK
        self.breakeven_activate = config.LIVERMORE_BREAKEVEN_ACTIVATE_PCT
        self.stop_atr_mult = config.LIVERMORE_STOP_ATR_MULT
        # ---- 进场质量（自适应缓冲 + 资金费过滤） ----
        self.breakout_atr_mult = config.LIVERMORE_BREAKOUT_ATR_MULT
        self.funding_filter = config.LIVERMORE_FUNDING_FILTER
        # ---- 风险预算仓位 ----
        self.risk_max_loss = config.LIVERMORE_RISK_MAX_LOSS_USD
        # ---- 滞涨离场（突破失败快速认错） ----
        self.stagnation_minutes = config.LIVERMORE_STAGNATION_MINUTES
        self.stagnation_min_pnl = config.LIVERMORE_STAGNATION_MIN_PNL
        # ---- 保本止损含费用缓冲 ----
        self.breakeven_buffer = config.LIVERMORE_BREAKEVEN_BUFFER_PCT
        # ---- 多时间周期过滤（15m 趋势） ----
        self.use_higher_tf = config.LIVERMORE_USE_HIGHER_TF
        self.higher_tf_minutes = config.LIVERMORE_HIGHER_TF_MINUTES
        self.higher_tf_fast = config.LIVERMORE_HIGHER_TF_FAST
        self.higher_tf_slow = config.LIVERMORE_HIGHER_TF_SLOW
        self.htf_deadband = config.LIVERMORE_HTF_DEADBAND_PCT
        self.htf_flat_allow = config.LIVERMORE_HTF_FLAT_ALLOW
        self.htf_exit_break = config.LIVERMORE_HTF_EXIT_BREAK
        self._htf_open = 0.0        # 当前高周期 K 线的开盘价
        self._htf_high = 0.0        # 当前高周期 K 线的最高价
        self._htf_low = 0.0         # 当前高周期 K 线的最低价
        self._htf_candles: list[dict] = []  # 已完成的高周期 K 线列表
        self._htf_unit = -1           # 当前高周期 K 线所处的时间桶编号（-1=尚未初始化）
        # ---- 运行时状态 ----
        self.pivot_stop = 0.0        # 关键点止损位（只上移不下移）
        self.add_ref = 0.0           # 创新高加仓参考价（上次建仓/加仓价）
        self.last_break_hi = 0.0     # 上次做多突破的关键点上沿（防止同一价位反复进场）
        self.last_break_lo = 0.0     # 上次做空突破的关键点下沿
        self.peak_pnl = 0.0          # 峰值浮盈（USDT），用于「回吐峰值利润」移动止盈
        self.breakeven_armed = False # 是否已启动保本止损

    def reset(self):
        super().reset()
        self.pivot_stop = 0.0
        self.add_ref = 0.0
        self.last_break_hi = 0.0
        self.last_break_lo = 0.0
        self.peak_pnl = 0.0
        self.breakeven_armed = False
        # 高周期 K 线
        self._htf_open = 0.0
        self._htf_high = 0.0
        self._htf_low = 0.0
        self._htf_candles.clear()
        self._htf_unit = -1

    # ------------------------------------------------------------------
    # 关键点
    # ------------------------------------------------------------------
    def pivots(self) -> tuple[float, float]:
        """最近 N 根 K 线的关键点：返回 (pivot_high, pivot_low)。"""
        window = self.candles[-self.pivot_period:]
        if not window:
            return 0.0, 0.0
        hi = max(c["high"] for c in window)
        lo = min(c["low"] for c in window)
        return hi, lo

    def _breakout_buffer(self) -> float:
        """突破缓冲：固定缓冲 与 15m ATR 自适应缓冲取大者。

        高波动币（15m ATR 1%+）的固定 0.1% 缓冲形同虚设，噪音即「突破」；
        按波动自适应放大缓冲，波动越大要求突破得越远才算数（0 倍数=只用固定缓冲）。
        """
        buf = self.breakout_pct
        if self.breakout_atr_mult > 0 and self.candles:
            atr = self._atr()
            ref = self.candles[-1]["close"]
            if atr > 0 and ref > 0:
                buf = max(buf, self.breakout_atr_mult * atr / ref)
        return buf

    # ------------------------------------------------------------------
    # 建仓（试探仓）
    # ------------------------------------------------------------------
    def _start_position(self, app, side: str):
        """突破关键点后试探建仓（利弗莫尔「先试探，行情证明你了再加」）。

        风险预算：启用 ATR 止损时，名义收敛到「被止损打掉的最大亏损 ≤ risk_max_loss」——
        高波动币自动缩小仓位、低波动币保持试探仓预算，让每笔试探的风险大体一致。
        """
        ref = app.last_price or app.mark
        if ref is None or ref <= 0:
            return
        notional = self.base_margin_usd * app.account.leverage
        if self.risk_max_loss > 0 and self.stop_atr_mult > 0:
            atr = self._atr()
            if atr > 0:
                loss_pct = self.stop_atr_mult * atr / ref   # 止损距离占价格比例
                if loss_pct > 0:
                    notional = min(notional, self.risk_max_loss / loss_pct)
        total = max(notional, app.account.min_notional)
        qty = self._size_qty(app, total)
        if qty <= 0:
            return
        ok, msg = app.account.open_position(side, qty, app.bid, app.ask)
        print(f"[利弗莫尔] ★ 突破关键点，试探建仓 {side} {qty:.4f}"
              f"（名义≈{total:.2f} USDT，试探保证金 {total / app.account.leverage:.2f} USDT）-> {msg}")
        if ok:
            self._on_position_started(app, first=True)

    def _try_breakout_entry(self, app):
        """关键点突破进场：多头突破关键点上沿（+自适应缓冲），空头跌破下沿（-缓冲）。

        进场质量三道闸：① 自适应缓冲（波动越大要求突破越远）② 资金费率过滤（拥挤方向不追）
        ③ 组合风控闸门（Portfolio 注入 can_enter_*，防同向堆仓）。
        """
        price = app.mark
        if price is None:
            return
        piv_h, piv_l = self.pivots()
        if piv_h <= 0 or piv_l <= 0:
            return
        buf = self._breakout_buffer()
        break_hi = piv_h * (1 + buf)
        break_lo = piv_l * (1 - buf)
        trend = self.trend()
        long_ok = price > break_hi and (not self.require_trend or trend == "UP")
        short_ok = price < break_lo and (not self.require_trend or trend == "DOWN")
        # 资金费率过滤：费率过高 = 多头拥挤（追多费用高、易被收割），过负 = 空头拥挤
        if self.funding_filter > 0:
            fr = getattr(app, "funding_rate", None)
            if fr is not None:
                if fr > self.funding_filter:
                    long_ok = False
                if fr < -self.funding_filter:
                    short_ok = False
        # 高周期趋势过滤（15m）：只在同向时才进场
        if self.use_higher_tf:
            htf_trend = self.higher_trend()
            if self.htf_flat_allow:
                # 放行模式：高周期是 FLAT 时不过滤（仅非同向时过滤）
                if htf_trend != "FLAT":
                    long_ok = long_ok and htf_trend == "UP"
                    short_ok = short_ok and htf_trend == "DOWN"
            else:
                # 严格模式：15m 无趋势(横盘)时不放行，减少横盘期双向假突破打脸
                long_ok = long_ok and htf_trend == "UP"
                short_ok = short_ok and htf_trend == "DOWN"
        # 组合风控闸门（手动/离线无该属性时默认全放行）；被拦时不记录 last_break，价位不浪费
        if not getattr(app, "can_enter", True):
            return
        if long_ok and not getattr(app, "can_enter_long", True):
            long_ok = False
        if short_ok and not getattr(app, "can_enter_short", True):
            short_ok = False
        if long_ok:
            # 须突破比上次更高的关键点才再次进场，避免同一价位反复进出
            if break_hi <= self.last_break_hi:
                return
            self.last_break_hi = break_hi
            self._start_position(app, "LONG")
        elif short_ok:
            if self.last_break_lo and break_lo >= self.last_break_lo:
                return
            self.last_break_lo = break_lo
            self._start_position(app, "SHORT")

    # ------------------------------------------------------------------
    # 加仓（两种模式）
    # ------------------------------------------------------------------
    def _pyramid_add(self, app):
        """利弗莫尔金字塔加仓：有盈利 + 创新高时，逐级顺势扩大同向仓位（绝不接回调、绝不摊平）。"""
        pos = app.account.position
        if pos is None or self.add_count >= self.max_adds:
            return
        if not getattr(app, "can_add", True):   # 组合风控：保证金/净敞口吃满后停止加仓
            return
        pnl = pos.unrealized_pnl(app.mark)
        if pnl <= 0 or app.account.cash <= 0:
            return
        add_notional = pnl * self.add_ratio
        floor = max(self.min_add_usd, app.account.min_notional)
        if add_notional < floor:
            return  # 浮盈不足以支撑最小加仓，继续等利润奔跑
        add_notional = min(add_notional, app.account.cash * pos.leverage)
        qty = self._size_qty(app, add_notional)
        if qty <= 0:
            return
        ok, msg = app.account.open_position(pos.side, qty, app.bid, app.ask)
        print(f"[利弗莫尔] 有盈利创新高，金字塔加仓 {pos.side} {qty:.4f}"
              f"（浮盈 {pnl:+.2f} USDT，名义≈{add_notional:.2f} USDT）-> {msg}")
        if ok:
            self.add_count += 1
            self._on_position_started(app, first=False)

    def _on_position_started(self, app, first: bool):
        """初始化一轮交易的加仓/移动止盈基准（沿用父类 + 记录加仓参考价）。"""
        super()._on_position_started(app, first)
        close = app.mark if app.mark is not None else app.last_price
        if close is not None:
            self.add_ref = close
        if first:
            self.peak_pnl = 0.0
            self.breakeven_armed = False

    # ------------------------------------------------------------------
    # 高周期 K 线种子
    # ------------------------------------------------------------------
    def seed_candles(self, rows: list[dict]):
        """利弗莫尔策略：继承父类种子 + 从 1m K 线聚合高周期 K 线。"""
        super().seed_candles(rows)
        self._seed_htf_from_1m()

    def _seed_htf_from_1m(self):
        """从已有的 1m K 线聚合出高周期已收盘 K 线，并初始化当前高周期 K 线状态。"""
        if not self.candles or not self.use_higher_tf:
            return
        period_sec = self.higher_tf_minutes * 60
        groups = {}
        for c in self.candles:
            key = c["ts"] // period_sec * period_sec
            if key not in groups:
                groups[key] = {"ts": key, "open": c["open"], "high": c["high"],
                               "low": c["low"], "close": c["close"]}
            else:
                g = groups[key]
                g["high"] = max(g["high"], c["high"])
                g["low"] = min(g["low"], c["low"])
                g["close"] = c["close"]
        keys = sorted(groups.keys())
        if not keys:
            return
        # 所有除最后一个外的完整高周期 K 线加入已收盘列表
        self._htf_candles = [groups[k] for k in keys[:-1]]
        if len(self._htf_candles) > self.max_candles:
            self._htf_candles = self._htf_candles[-self.max_candles:]
        # 最后一个高周期 K 线是当前正在进行中的，初始化状态
        last = groups[keys[-1]]
        self._htf_unit = last["ts"] // period_sec
        self._htf_open = last["open"]
        self._htf_high = last["high"]
        self._htf_low = last["low"]

    def _update_pivot_stop(self, pos):
        """更新关键点止损位：多头只上移（随关键点低点上移），空头只下移。"""
        piv_h, piv_l = self.pivots()
        if pos.side == "LONG":
            if piv_l > self.pivot_stop:
                self.pivot_stop = piv_l
        else:
            if self.pivot_stop == 0.0 or piv_h < self.pivot_stop:
                self.pivot_stop = piv_h

    def _add_triggered(self, app, pos) -> bool:
        """加仓触发（利弗莫尔金字塔）：只在有盈利且创新高（比上次加仓价再突破）时顺势加仓，绝不接回调。"""
        close = app.mark if app.mark is not None else app.last_price
        if close is None or not self.arm_for_add or self.add_ref <= 0:
            return False
        if pos.side == "LONG":
            return close >= self.add_ref * (1 + self.add_breakout_pct)
        return close <= self.add_ref * (1 - self.add_breakout_pct)

    def _update_extremes(self, app, pos):
        """沿用父类（新高/新低、移动止盈启动）+ 追踪峰值浮盈与保本止损。"""
        super()._update_extremes(app, pos)
        close = app.mark if app.mark is not None else app.last_price
        if close is None:
            return
        pnl = pos.unrealized_pnl(close)
        if pnl > self.peak_pnl:
            self.peak_pnl = pnl
        if not self.breakeven_armed:
            if pos.side == "LONG":
                self.breakeven_armed = close >= pos.entry_price * (1 + self.breakeven_activate)
            else:
                self.breakeven_armed = close <= pos.entry_price * (1 - self.breakeven_activate)

    # ------------------------------------------------------------------
    # 主评估循环
    # ------------------------------------------------------------------
    def on_eval(self, app):
        # 行情未就绪（盘口/标记价未到位）时不评估，避免用空价格下单
        if app.bid is None or app.ask is None or app.mark is None:
            return
        price = app.mark
        self._update_candle(price, time.time())
        self._update_htf_candle(price, time.time())   # 累积 15m K 线

        pos = app.account.position
        if pos is not None:
            if self.state == "IDLE":
                self._on_position_started(app, first=True)   # 采纳外部开仓
            self._update_extremes(app, pos)
            if self.use_pivot_stop:
                self._update_pivot_stop(pos)
            if self._check_exit(app, pos):
                return
            if self._add_triggered(app, pos):
                self._pyramid_add(app)
            return

        # 无持仓
        if self.state == "HOLDING":
            trades = app.account.trades
            self._finish_cycle(trades[-1].realized if trades else None,
                               reason="外部平仓", app=app)   # 外部平仓/强平按实际盈亏定性冷却
        if time.time() < self.cooldown_until:
            return
        self._try_breakout_entry(app)

    # ------------------------------------------------------------------
    # 离场
    # ------------------------------------------------------------------
    def _check_exit(self, app, pos) -> bool:
        """离场：ATR/保证金止损 → 止盈 → 保本(含费) → 关键点 → 滞涨 → 移动止盈(回吐峰值) → 15m 反转 → 时限。

        命中任一即平仓并结束本轮。反转离场挂到 15m 结构（比 1m 均线更早），
        移动止盈回吐峰值利润比例，浮盈达标后含费保本，硬止损 ATR 自适应，
        滞涨离场快速承认失败突破（利弗莫尔：行情不证明你对，就当它错了）。
        """
        margin = pos.margin
        pnl = pos.unrealized_pnl(app.mark)
        pnl_pct = pnl / margin if margin else 0.0
        close = app.mark if app.mark is not None else app.last_price
        entry = pos.entry_price

        # 1) 硬止损：保证金百分比兜底（绝对下限，30x 下≈价格 -1%，不能再放宽）+ ATR 自适应提前离场
        stop_reason = None
        if pnl_pct <= -self.sl:
            stop_reason = "止损"
        # ATR 止损只在比保证金止损更紧时更早触发（低波动下离场更果断，绝不放宽到强平价附近）
        if stop_reason is None and self.stop_atr_mult > 0:
            atr = self._atr()
            if atr > 0:
                stop_dist = atr * self.stop_atr_mult
                if pos.side == "LONG" and close is not None and close <= entry - stop_dist:
                    stop_reason = f"ATR止损({stop_dist:.4f})"
                elif pos.side == "SHORT" and close is not None and close >= entry + stop_dist:
                    stop_reason = f"ATR止损({stop_dist:.4f})"
        if stop_reason:
            self._do_close(app, pos, stop_reason)
            return True

        # 2) 止盈（固定止盈，一般由移动止盈触发，此处兜底）
        if pnl_pct >= self.tp:
            self._do_close(app, pos, "止盈")
            return True

        # 3) 保本止损：浮盈达标后回落到含费保本位（入场价±费用缓冲）即离场，
        #    消灭「浮盈变亏损」，且保本离场不再倒贴手续费
        if self.breakeven_armed and close is not None:
            be = entry * (1 + self.breakeven_buffer) if pos.side == "LONG" else entry * (1 - self.breakeven_buffer)
            if (pos.side == "LONG" and close <= be) or (pos.side == "SHORT" and close >= be):
                self._do_close(app, pos, "保本止损")
                return True

        # 4) 关键点止损（利弗莫尔）：跌破关键点止损位离场（只上移不下移，保护浮盈）
        if self.use_pivot_stop and self.pivot_stop and close is not None:
            hit_pivot = ((pos.side == "LONG" and close < self.pivot_stop)
                         or (pos.side == "SHORT" and close > self.pivot_stop))
            if hit_pivot:
                self._do_close(app, pos, "关键点止损")
                return True

        # 4.5) 滞涨离场：试探仓迟迟未获加仓资格（行情从未证明）且峰值浮盈不达标
        #      → 利弗莫尔：进场后行情不马上证明你对，就当它错了，承认突破失败先离场
        if (self.stagnation_minutes > 0 and self.add_count == 0
                and time.time() - pos.opened_at > self.stagnation_minutes * 60
                and self.peak_pnl < pos.margin * self.stagnation_min_pnl):
            self._do_close(app, pos, "滞涨离场")
            return True

        # 5) 移动止盈：回吐峰值利润的 giveback 比例即离场（让利润奔跑，不接固定回撤）
        if self.trail_active and self.peak_pnl > 0:
            if pnl <= self.peak_pnl * (1 - self.trail_giveback):
                self._do_close(app, pos, "移动止盈(回吐峰值利润)")
                return True

        # 6) 反转离场：优先 15m 结构（比 1m 均线更早），未启用高周期时才用 1m 均线
        if self.use_higher_tf:
            if self._htf_exit_signal(app, pos):
                self._do_close(app, pos, "15m反转")
                return True
        else:
            trend = self.trend()
            if (pos.side == "LONG" and trend == "DOWN") or (pos.side == "SHORT" and trend == "UP"):
                self._do_close(app, pos, "趋势反转")
                return True

        # 7) 时限
        if time.time() - pos.opened_at > self.time_limit:
            self._do_close(app, pos, "时间限制")
            return True
        return False

    def _do_close(self, app, pos, reason: str):
        """统一平仓出口：按盘口平仓、打印、结束本轮。"""
        pnl = pos.unrealized_pnl(app.mark)   # 平仓前取（平仓后 qty/margin 清零，再取恒为 0）
        margin = pos.margin
        ok, msg = app.account.close_position(None, app.bid, app.ask, reason=f"{reason}-利弗莫尔")
        if ok:
            trades = app.account.trades
            if trades:
                pnl = trades[-1].realized    # 实际已实现盈亏（含手续费）优先
        pnl_pct = pnl / margin * 100 if margin else 0.0
        print(f"[利弗莫尔] {reason}（盈亏 {pnl_pct:+.1f}%）-> {msg}")
        self._finish_cycle(pnl, reason=reason, app=app)

    def _htf_exit_signal(self, app, pos) -> bool:
        """15m 反转离场信号：结构破坏（跌破/涨破上一根已收盘 15m 关键位）+ 均线反转。"""
        if not self.use_higher_tf:
            return False
        close = app.mark if app.mark is not None else app.last_price
        if close is None:
            return False
        if self.htf_exit_break and self._htf_candles:
            last = self._htf_candles[-1]
            if pos.side == "LONG" and close < last["low"]:
                return True
            if pos.side == "SHORT" and close > last["high"]:
                return True
        htf = self.higher_trend()
        if (pos.side == "LONG" and htf == "DOWN") or (pos.side == "SHORT" and htf == "UP"):
            return True
        return False

    def _atr(self) -> float:
        """平均真实波幅，基于高周期(15m)已收盘 K 线；不足时回退到 1m K 线。"""
        src = self._htf_candles if (self.use_higher_tf and len(self._htf_candles) >= 2) else self.candles
        if len(src) < 2:
            return 0.0
        window = src[-min(14, len(src)):]
        trs = []
        for i in range(1, len(window)):
            h = window[i]["high"]
            l = window[i]["low"]
            pc = window[i - 1]["close"]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        return sum(trs) / len(trs) if trs else 0.0

    def _finish_cycle(self, pnl: float | None = None, reason: str = "", app=None):
        """一轮交易结束：清空基准与关键点止损，进入冷却（保留 last_break 防止同一价位反复进场）。"""
        super()._finish_cycle(pnl, reason, app)
        self.pivot_stop = 0.0
        self.peak_pnl = 0.0
        self.breakeven_armed = False

    # ------------------------------------------------------------------
    # 多时间周期（15m）
    # ------------------------------------------------------------------
    def _update_htf_candle(self, price: float, ts: float):
        """把 1m _update_candle 的每次行情累积成高周期 K 线（如 15m）。

        由 on_eval 在 _update_candle 之后调用。高周期 K 线跨边界时收盘旧线。
        """
        if not self.use_higher_tf:
            return
        minute = int(ts // 60)
        htf_unit = minute // self.higher_tf_minutes

        if self._htf_unit < 0 or htf_unit != self._htf_unit:
            # 跨高周期边界 → 收盘旧高周期 K 线
            if self._htf_unit >= 0:
                self._htf_candles.append({
                    "open": self._htf_open,
                    "high": self._htf_high,
                    "low": self._htf_low,
                    "close": price,
                })
                if len(self._htf_candles) > self.max_candles:
                    self._htf_candles.pop(0)
            self._htf_unit = htf_unit
            self._htf_open = price
            self._htf_high = price
            self._htf_low = price
        else:
            self._htf_high = max(self._htf_high, price)
            self._htf_low = min(self._htf_low, price)

    def higher_trend(self) -> str:
        """高周期趋势: "UP" / "DOWN" / "FLAT"，基于高周期 K 线收盘价 EMA。"""
        if not self.use_higher_tf:
            return "FLAT"
        closes = [c["close"] for c in self._htf_candles]
        if len(closes) < self.higher_tf_slow:
            return "FLAT"
        fast = ema(closes, self.higher_tf_fast)
        slow = ema(closes, self.higher_tf_slow)
        if fast is None or slow is None or slow == 0:
            return "FLAT"
        diff = (fast - slow) / slow
        if diff > self.htf_deadband:
            return "UP"
        if diff < -self.htf_deadband:
            return "DOWN"
        return "FLAT"

    # ------------------------------------------------------------------
    # 状态显示
    # ------------------------------------------------------------------
    def status_lines(self, app) -> list[str]:
        piv_h, piv_l = self.pivots()
        trend = self.trend()
        lines = [
            f"利弗莫尔: 趋势 {trend} | 状态 {self.state} | 金字塔加仓(创新高)",
            f"关键点 H {piv_h:.2f} / L {piv_l:.2f} | 已加仓 {self.add_count}/{self.max_adds}",
        ]
        pos = app.account.position
        if pos:
            pnl = pos.unrealized_pnl(app.mark) if app.mark else 0.0
            pct = pnl / pos.margin * 100 if pos.margin else 0.0
            stop_desc = f"关键点止损 {self.pivot_stop:.2f}" if self.pivot_stop else "关键点止损 未启用"
            lines.append(f"当前盈亏 {pct:+.1f}% | {stop_desc} | 移动止盈 {'开' if self.trail_active else '关'} | "
                         f"剩余时限 {max(0, int(self.time_limit - (time.time() - pos.opened_at)))}s")
        else:
            # 无持仓时显示建仓所需的关键点突破位（含自适应缓冲），便于观察为何未进场
            if piv_h and piv_l:
                buf = self._breakout_buffer()
                bh = piv_h * (1 + buf)
                bl = piv_l * (1 - buf)
                lines.append(f"建仓需: 突破 {bh:.2f} 开多 / 跌破 {bl:.2f} 开空"
                             + ("" if self.require_trend else "（纯突破，无均线过滤）"))
        st = self.stats
        if st["cycles"]:
            wr = st["wins"] / st["cycles"] * 100
            lines.append(f"战绩: {st['cycles']}轮 胜率 {wr:.0f}% | "
                         f"累计 {st['gross_win'] + st['gross_loss']:+.2f} U | 最大连败 {st['max_loss_streak']}")
        return lines
