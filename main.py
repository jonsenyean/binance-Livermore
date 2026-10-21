# -*- coding: utf-8 -*-
"""币安永续合约 · 组合纸面交易工具主程序。

组合模式：
  - 总资金 TOTAL_BALANCE，均分给 NUM_PAIRS 个交易对（每对 PER_PAIR_BALANCE 独立资金池）
  - 启动时从币安拉取行情，自动筛选「交易量靠前 + 波动率高（含 ETH）」的交易对
  - 统计历史 1h K 线，找出高波动时段（TRADE_ONLY_IN_WINDOW 时自动策略仅在此时段交易）
  - 每个交易对独立运行自动策略（config.STRATEGY 选择）：
      利弗莫尔关键点滚仓（默认）：关键点突破进场 → 试探仓 → 金字塔滚仓（回调/顺势）→ 关键点止损
      趋势跟随 + 浮盈加仓（可切回）：1m EMA 快慢线趋势 → 固定保证金建仓 → 有浮盈且回调时加仓
  - 止盈止损按仓位保证金(本金)：盈利≥TREND_TAKE_PROFIT 止盈、亏损≥TREND_STOP_LOSS 止损
  - 汇总统计每对与组合盈亏
"""
from __future__ import annotations

import asyncio
import atexit
import queue
import sys
import threading
import time
import unicodedata
from datetime import datetime, timezone


# ── Windows 控制台：启动时禁用 QuickEdit 模式 ──
# 防止鼠标点击误入文本选择状态，导致 print()/input() 被系统锁住（控制台整个卡死）。
# 退出时自动恢复，不影响其他程序。
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _console_handle = ctypes.windll.kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
    _original_mode = wintypes.DWORD()
    if _console_handle and _console_handle != -1:
        ctypes.windll.kernel32.GetConsoleMode(_console_handle, ctypes.byref(_original_mode))
        # ENABLE_QUICK_EDIT_MODE = 0x0040, ENABLE_EXTENDED_FLAGS = 0x0080
        ctypes.windll.kernel32.SetConsoleMode(
            _console_handle, _original_mode.value & ~0x0040 | 0x0080)
        atexit.register(lambda: ctypes.windll.kernel32.SetConsoleMode(
            _console_handle, _original_mode.value))

    # 启用 ANSI 颜色（VT 处理）：cmd 默认不解析 \033[...m，需给输出句柄开
    # ENABLE_VIRTUAL_TERMINAL_PROCESSING（0x0004）。不加则颜色码会变成乱码显示。
    _out_handle = ctypes.windll.kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
    if _out_handle and _out_handle != -1:
        _out_mode = wintypes.DWORD()
        ctypes.windll.kernel32.GetConsoleMode(_out_handle, ctypes.byref(_out_mode))
        ctypes.windll.kernel32.SetConsoleMode(_out_handle,
                                              _out_mode.value | 0x0004)

import config
from binance_ws import BinanceFuturesWS
from portfolio import Portfolio
from selector import (avg_hourly_amplitude, describe_window, fetch_klines_many,
                      fetch_precision, get_24h_tickers, high_volatility_window,
                      select_pairs, volatility_by_hour)


def fmt(x) -> str:
    return f"{x:,.2f}" if x is not None else "-"


def sfmt(x) -> str:
    return f"{x:+,.2f}"


def disp(s, width: int) -> str:
    """按终端显示宽度左对齐补齐：CJK/全角字符占 2 列，保证中文表头与数据列对齐。"""
    w = sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in str(s))
    return str(s) + " " * max(0, width - w)


class App:
    def __init__(self):
        self.portfolio: Portfolio | None = None
        self.selected: list[dict] = []
        self.pair_source = "fallback"   # custom=jiaoyi.ini / online=在线选对 / fallback=回退
        self.cmd_q: queue.Queue = queue.Queue()
        self.connected = False
        self.auto_enabled = config.AUTO_ENABLED
        self.leverage = config.LEVERAGE
        self.last_status = 0.0
        self._stop = False
        self.ws: BinanceFuturesWS | None = None
        self.ws_task: asyncio.Task | None = None
        self.strategy_task: asyncio.Task | None = None
        self.account_refresh_task: asyncio.Task | None = None  # 账户缓存刷新任务
        self._shared_cash = True  # 由 ask_shared_cash() 启动时设置
        self.profit_target = 0.0  # 由 ask_profit_target() 启动时设置（USDT，0=不启用）
        self.loss_limit = 0.0     # 由 ask_loss_limit() 启动时设置（USDT，0=不启用）
        self.last_update = 0.0        # 最近一次实时行情/手动喂价的时间（monotonic，0=尚无数据）
        self._stale_warned = False    # 行情过期告警只打印一次，恢复后重置
        # 币安账户置顶区（API 开启时缓存，避免每次刷新状态都异步拉取）
        self._account_cache = None    # fetch_account_info 的返回 dict；未配置/未拉到为 None
        self._account_refresh_at = 0.0  # 下次刷新账户缓存的时间（monotonic）

    # ------------------------------------------------------------------
    # 行情回调
    # ------------------------------------------------------------------
    def on_market_update(self, kind: str, data: dict):
        # 任何 book/mark 消息都算行情心跳：用于判断行情是否过期（断线/静默冻结时停止更新）
        self.last_update = time.monotonic()
        self.portfolio.update_market(kind, data)

    def _on_connect(self):
        self.connected = True

    def _on_disconnect(self):
        self.connected = False

    # ------------------------------------------------------------------
    # 命令处理
    # ------------------------------------------------------------------
    def process_command(self, line: str):
        parts = line.strip().split()
        if not parts:
            return
        cmd = parts[0].lower()
        try:
            if cmd in ("help", "?"):
                self.print_help()
            elif cmd in ("status", "st"):
                self.print_status()
            elif cmd in ("pairs", "p"):
                self.print_pairs()
            elif cmd == "window":
                self.print_window()
            elif cmd in ("buy", "long"):             # 开多（按数量）：buy ETHUSDT 0.01
                self.cmd_open("LONG", parts)
            elif cmd in ("short", "sell"):           # 开空（按数量）：short ETHUSDT 0.01
                self.cmd_open("SHORT", parts)
            elif cmd == "buyu":                      # 开多（按名义U）：buyu ETHUSDT 20
                self.cmd_open_notional("LONG", parts)
            elif cmd == "shortu":                    # 开空（按名义U）：shortu ETHUSDT 20
                self.cmd_open_notional("SHORT", parts)
            elif cmd in ("close", "cp"):
                self.cmd_close(parts)
            elif cmd == "auto":
                self.cmd_auto(parts)
            elif cmd == "lev":
                self.leverage = int(float(parts[1]))
                for ctx in self.portfolio.ctx.values():
                    ctx.account.leverage = self.leverage
                print(f"杠杆已设为 {self.leverage}x（新仓位生效）")
            elif cmd == "tp":
                for ctx in self.portfolio.ctx.values():
                    ctx.strategy.tp = float(parts[1])
                print(f"止盈已设为保证金的 {float(parts[1]) * 100:.0f}%")
            elif cmd == "sl":
                for ctx in self.portfolio.ctx.values():
                    ctx.strategy.sl = float(parts[1])
                print(f"止损已设为保证金的 {float(parts[1]) * 100:.0f}%")
            elif cmd == "price":
                p = float(parts[1])
                for ctx in self.portfolio.ctx.values():
                    ctx.bid = ctx.ask = ctx.mark = ctx.last_price = p
                # 手动喂价视为行情新鲜：离线模式继续用策略也不会被「行情过期」暂停挡掉
                self.last_update = time.monotonic()
                print(f"手动设置全部交易对价格 {p:,.2f}")
            elif cmd == "hist":
                self.print_history(parts)
            elif cmd in ("stats", "tj"):
                self.print_stats()
            elif cmd in ("quit", "exit", "q"):
                self._stop = True
                raise SystemExit
            else:
                print("未知命令，输入 help 查看帮助")
        except (IndexError, ValueError):
            print("参数错误，输入 help 查看用法")

    def _resolve_symbol(self, parts) -> str | None:
        """命令第 1 个参数为交易对时返回它，否则返回第一个有行情的交易对。"""
        if len(parts) > 1 and parts[1].upper() in self.portfolio.ctx:
            return parts[1].upper()
        return self.portfolio.pairs[0]

    def cmd_open(self, side: str, parts: list):
        if len(parts) < 3:
            print(f"用法: {parts[0]} <交易对> <数量>")
            return
        sym = parts[1].upper()
        if sym not in self.portfolio.ctx:
            print(f"未知交易对: {sym}（当前组合: {', '.join(self.portfolio.pairs)}）")
            return
        ctx = self.portfolio.ctx[sym]
        if ctx.bid is None or ctx.ask is None:
            print(f"{sym} 行情未就绪（离线请先 price <价格>）")
            return
        qty = float(parts[2])
        ok, msg = ctx.account.open_position(side, qty, ctx.bid, ctx.ask)
        print(f"[手动 {sym}] {msg}")

    def cmd_open_notional(self, side: str, parts: list):
        if len(parts) < 3:
            print(f"用法: {parts[0]} <交易对> <USDT金额>")
            return
        sym = parts[1].upper()
        if sym not in self.portfolio.ctx:
            print(f"未知交易对: {sym}")
            return
        ctx = self.portfolio.ctx[sym]
        ref = ctx.ask if side == "LONG" else ctx.bid
        if ref is None:
            print(f"{sym} 行情未就绪")
            return
        qty = float(parts[2]) / ref
        ok, msg = ctx.account.open_position(side, qty, ctx.bid, ctx.ask)
        print(f"[手动 {sym}] {msg}")

    def cmd_close(self, parts: list):
        if len(parts) < 2:
            print(f"用法: close <交易对> [数量]")
            return
        sym = parts[1].upper()
        if sym not in self.portfolio.ctx:
            print(f"未知交易对: {sym}")
            return
        ctx = self.portfolio.ctx[sym]
        if ctx.bid is None or ctx.ask is None:
            print(f"{sym} 行情未就绪")
            return
        qty = float(parts[2]) if len(parts) > 2 else None
        ok, msg = ctx.account.close_position(qty, ctx.bid, ctx.ask)
        print(f"[手动 {sym}] {msg}")

    def cmd_auto(self, parts: list):
        if len(parts) > 1:
            self.auto_enabled = parts[1].lower() in ("on", "true", "1")
        else:
            self.auto_enabled = not self.auto_enabled
        print(f"自动策略：{'开' if self.auto_enabled else '关'}")

    # ------------------------------------------------------------------
    # 策略评估循环
    # ------------------------------------------------------------------
    @staticmethod
    def _utc_hour() -> int:
        return datetime.now(timezone.utc).hour

    def in_window_now(self) -> bool:
        if not config.TRADE_ONLY_IN_WINDOW or not self.portfolio.window:
            return True
        return self.portfolio.is_in_window(self._utc_hour())

    def _market_fresh(self) -> bool:
        """行情是否新鲜：最近一次实时行情/手动喂价在 WS_STALE_SECONDS 以内。

        断线、或「连接活但行情不推」（看门狗强制重连前）都会让该值为 False；
        离线模式用 price 命令手动喂价会刷新 last_update，因此仍可正常跑策略。
        """
        return time.monotonic() - self.last_update <= config.WS_STALE_SECONDS

    async def strategy_loop(self):
        while not self._stop:
            await asyncio.sleep(config.STRATEGY_EVAL_SECONDS)
            if not self.auto_enabled:
                continue
            if not self.in_window_now():
                continue
            if not self._market_fresh():
                # 行情过期：暂停自动交易，避免用陈旧价格做单；恢复行情后自动继续
                if not self._stale_warned:
                    print("[策略] 行情过期（无实时更新），已暂停自动交易，避免用陈旧价格做单；"
                          "行情恢复后自动继续")
                    self._stale_warned = True
                continue
            self._stale_warned = False
            try:
                self.portfolio.on_eval()
            except Exception as e:
                print(f"[策略] 错误: {e}")

    async def account_refresh_loop(self):
        """后台刷新置顶区的账户缓存（每 ACCOUNT_REFRESH_SECONDS 拉一次真实账户）。

        启动后立即拉一次（不等首次间隔），之后每 interval 秒刷新；
        未配置 API Key 时空跑；失败静默，不影响策略。
        """
        interval = getattr(config, "ACCOUNT_REFRESH_SECONDS", 30)
        while not self._stop:
            if config.BINANCE_API_KEY and config.BINANCE_API_SECRET:
                try:
                    from api_client import fetch_account_info
                    self._account_cache = await fetch_account_info(
                        config.BINANCE_API_KEY, config.BINANCE_API_SECRET)
                except Exception:
                    pass  # 拉取失败保留旧缓存
            # 首次即拉取（未成功则 2 秒后重试），随后周期性刷新
            await asyncio.sleep(interval
                                if self._account_cache is not None
                                else 2)

    # ------------------------------------------------------------------
    # 显示
    # ------------------------------------------------------------------
    def print_help(self):
        print("""
命令一览：
  status / st           组合盈亏汇总
  pairs                 当前选中的交易对（交易量/波动率）
  window                高波动时段
  buy <对> <数量>       开多（如 buy ETHUSDT 0.01）
  buyu <对> <USDT>      按名义价值开多
  short <对> <数量>     开空
  shortu <对> <USDT>    按名义价值开空
  close <对> [数量]     平仓
  auto [on|off]         自动策略开关（当前: 利弗莫尔关键点突破+金字塔 / 趋势跟随+浮盈加仓）
  lev <倍数>            调整杠杆
  tp <小数>             止盈=保证金该比例（默认 5.0 = 500%）
  sl <小数>             止损=保证金该比例（默认 0.5 = 50%）
  price <价格>          离线模式手动喂价（全部交易对）
  hist [交易对]         交易历史
  stats                 策略战绩（胜率/盈亏比/期望）与组合风控闸门
  help                  帮助
  quit                  退出
""")

    def print_pairs(self):
        if not self.selected:
            src = {"custom": "jiaoyi.ini 指定", "count": "启动抓取",
                   "online": "在线选对"}.get(self.pair_source, "回退")
            print(f"使用{src}交易对：", ", ".join(self.portfolio.pairs))
            return
        print(f"{'交易对':<12}{'24h交易量(USDT)':<18}{'30d波动率':<10}{'最新价':<12}")
        for t in self.selected:
            print(f"{t['symbol']:<12}{t['quote_volume']:>15,.0f}  {t.get('volatility', 0) * 100:>6.2f}%  {t['last']:>12,.2f}")
        print(f"共 {len(self.selected)} 个（交易量前 {config.VOLUME_POOL} 名中按 30 天波动率筛选）")

    def print_window(self):
        if not self.portfolio.window:
            print("无高波动时段数据")
            return
        print("高波动时段：", describe_window(self.portfolio.window))
        now = self._utc_hour()
        status = "在窗口内 ✓" if self.in_window_now() else "窗口外"
        print(f"当前 UTC {now:02d}:00 -> {status}")

    def print_status(self):
        s = self.portfolio.summary()
        width = 129
        # —— 置顶区：清屏回顶，先画账户三行标题，再画状态表格 ——
        # 账户数据来自缓存 self._account_cache（strategy_loop 定时刷新）；
        # 未配置/未拉到则为 None，print_top_banner 也打印占位标题。
        from api_client import print_top_banner
        print("\033[H\033[2J", end="")            # 光标回顶 + 清屏
        print_top_banner(self._account_cache)      # 置顶三行（总资金/合约/现货）
        print("=" * width)
        data_state = "无数据" if self.last_update == 0 else ("新鲜" if self._market_fresh() else "过期")
        print(f" 组合账户 | {len(self.portfolio.pairs)} 交易对 | 总资金 {s['initial']:,.0f} USDT | "
              f"自动策略 {'开' if self.auto_enabled else '关'} | 连接 {'已连接' if self.connected else '未连接'} | "
              f"行情 {data_state}")
        if self.portfolio.window and config.TRADE_ONLY_IN_WINDOW:
            print(" " + describe_window(self.portfolio.window) +
                  f" | 当前{'在' if self.in_window_now() else '不在'}窗口")
        rs = self.portfolio.stats_summary()
        wr_txt = f" | 胜率 {rs['wins'] / rs['cycles'] * 100:.0f}%（{rs['cycles']}轮）" if rs["cycles"] else ""
        halt_txt = " | ⚠当日熔断" if rs.get("halted") else ""
        tgt_txt = ""
        if rs.get("profit_target"):
            tgt_txt = (" | ★止盈目标已达成" if rs.get("target_hit")
                       else f" | 今日盈利 {rs['profit_today']:+,.1f}/{rs['profit_target']:g} U")
        loss_txt = ""
        if rs.get("loss_limit"):
            loss_txt = (" | ▓止损上限已触发" if rs.get("loss_hit")
                        else f" | 今日亏损 {max(rs['loss_today'], 0.0):,.1f}/{rs['loss_limit']:g} U")
        print(f" 风控: 持仓 {rs['n_pos']}/{config.RISK_MAX_POSITIONS} | "
              f"保证金占用 {rs['margin_pct'] * 100:.0f}%（上限 {config.RISK_MAX_MARGIN_PCT * 100:.0f}%） | "
              f"净敞口 {rs['net_notional']:+,.0f}/{config.RISK_MAX_NET_NOTIONAL_USD:,.0f} U{wr_txt}{halt_txt}{tgt_txt}{loss_txt}")
        print("-" * width)
        header = (disp("对", 13) + disp("标记价", 12) + disp("持仓(数量@价)", 16)
                  + disp("倍数", 6) + disp("加仓", 6) + disp("趋势", 6)
                  + disp("关键点", 16) + disp("止损价", 12) + disp("强平价", 12)
                  + disp("未实现", 10) + disp("已实现", 10) + disp("盈亏", 10))
        print(header)
        for r in s["rows"]:
            line = (disp(r["symbol"], 13) + disp(fmt(r["mark"]), 12) + disp(r["position"], 16)
                    + disp(r["leverage"], 6) + disp(r["roll"], 6) + disp(r["trend"], 6)
                    + disp(r.get("pivot") or "-", 16) + disp(fmt(r["sl_price"]), 12)
                    + disp(fmt(r["liq_price"]), 12)
                    + disp(sfmt(r["unrealized"]), 10) + disp(sfmt(r["realized"]), 10)
                    + disp(sfmt(r["net"]), 10))
            print(line)
        print("-" * width)
        print(f" 合计  持仓价值 {fmt(s['total_position_value']):<14}总保证金 {fmt(s['total_equity']):<12}"
              f"已实现 {sfmt(s['total_realized']):<10}手续费 {fmt(s['total_fees']):<10}"
              f"资金费 {sfmt(s['total_funding']):<10}纯收益(已扣费) {sfmt(s['total_net'])}")
        print("=" * width)

    def print_history(self, parts: list):
        sym = parts[1].upper() if len(parts) > 1 else None
        ctxs = [self.portfolio.ctx[sym]] if sym else list(self.portfolio.ctx.values())
        for ctx in ctxs:
            trades = ctx.account.trades
            if not trades:
                print(f"[{ctx.symbol}] 暂无交易记录")
                continue
            print(f"[{ctx.symbol}]")
            print(f"{'时间':<17}{'动作':<7}{'方向':<6}{'数量':<10}{'价格':<12}"
                  f"{'已实现':<12}{'手续费':<10}{'原因'}")
            for t in trades[-20:]:
                ts = time.strftime("%m-%d %H:%M:%S", time.localtime(t.ts))
                print(f"{ts:<17}{t.action:<7}{t.side:<6}{t.qty:<10.4f}"
                      f"{t.price:<12.2f}{t.realized:<12.4f}{t.fee:<10.4f}{t.reason}")

    def print_stats(self):
        """策略战绩（胜率/盈亏比/期望）+ 组合风控闸门状态。"""
        s = self.portfolio.stats_summary()
        print("=" * 70)
        print(" 策略战绩（本次运行累计，按平仓轮计；跨会话明细见 trade_log.csv）")
        if s["cycles"] == 0:
            print("  尚无完整交易轮")
        else:
            wr = s["wins"] / s["cycles"] * 100
            net = s["gross_win"] + s["gross_loss"]
            pf = (s["gross_win"] / -s["gross_loss"]) if s["gross_loss"] < 0 else float("inf")
            avg_w = s["gross_win"] / s["wins"] if s["wins"] else 0.0
            avg_l = s["gross_loss"] / s["losses"] if s["losses"] else 0.0
            print(f"  完整 {s['cycles']} 轮 | 胜 {s['wins']} 负 {s['losses']} | "
                  f"胜率 {wr:.1f}% | 最大连败 {s['max_loss_streak']}")
            print(f"  总盈利 {s['gross_win']:+.2f} | 总亏损 {s['gross_loss']:+.2f} | "
                  f"净 {net:+.2f} U | 利润因子 {pf:.2f}")
            print(f"  平均盈利 {avg_w:+.2f} | 平均亏损 {avg_l:+.2f} | 期望 {net / s['cycles']:+.3f} U/轮")
        print(f" 风控: 持仓 {s['n_pos']}/{config.RISK_MAX_POSITIONS} | "
              f"保证金 {s['used_margin']:,.0f}/{s['equity']:,.0f} U"
              f"（{s['margin_pct'] * 100:.0f}%，上限 {config.RISK_MAX_MARGIN_PCT * 100:.0f}%） | "
              f"净方向敞口 {s['net_notional']:+,.0f} U（上限 {config.RISK_MAX_NET_NOTIONAL_USD:,.0f}）")
        if s.get("halted"):
            print("  ⚠ 当日熔断生效：权益回撤触线，暂停新开仓至 UTC 次日（现有仓位照常管理）")
        if s.get("profit_target"):
            if s.get("target_hit"):
                print("  ★ 止盈目标已达成：全部交易对停止开仓（重启前不再进场，现有仓位照常管理）")
            else:
                pct = min(s["profit_today"] / s["profit_target"] * 100, 100.0) if s["profit_target"] > 0 else 0.0
                print(f"  今日盈利 {s['profit_today']:+.2f} / 止盈目标 {s['profit_target']:g} USDT（达成 {pct:.0f}%）")
        if s.get("loss_limit"):
            if s.get("loss_hit"):
                print("  ▓ 止损上限已触发：全部交易对停止开仓（重启前不再进场，现有仓位照常管理）")
            else:
                loss_now = max(s["loss_today"], 0.0)
                print(f"  今日亏损 {loss_now:,.2f} / 止损上限 {s['loss_limit']:g} USDT（余量 {s['loss_limit'] - loss_now:,.2f}）")
        print("=" * 70)

    def print_banner(self, count: int):
        strat_label = {
            "livermore_roll": "利弗莫尔关键点突破 + 金字塔",
            "trend_pyramid": "趋势跟随 + 浮盈加仓",
        }.get(config.STRATEGY, config.STRATEGY)
        print("=" * 58)
        print("  币安 USDT 永续 · 利弗莫尔战法滚仓策略 · 组合纸面交易（Paper Trading）")
        mode = "共享现金池" if self._shared_cash else "每对独立现金池"
        print(f"  总资金 {config.TOTAL_BALANCE:,.0f} USDT | 抓取 {count} 交易对 | "
              f"{mode} | 首仓保证金 {config.TREND_POSITION_MARGIN_USD:.0f} USDT")
        if config.STRATEGY == "livermore_roll":
            print(f"  策略: {strat_label} | 试探仓 {config.LIVERMORE_PROBE_MARGIN_USD:.0f} USDT | "
                  f"加仓: 有盈利创新高金字塔 | 止损 保证金{config.TREND_STOP_LOSS * 100:.0f}%")
        else:
            print(f"  策略: {strat_label} | "
                  f"止盈 保证金{config.TREND_TAKE_PROFIT * 100:.0f}% / 止损 保证金{config.TREND_STOP_LOSS * 100:.0f}%")
        print(f"  组合风控: 持仓≤{config.RISK_MAX_POSITIONS}对 | 保证金≤{config.RISK_MAX_MARGIN_PCT * 100:.0f}% | "
              f"净敞口≤{config.RISK_MAX_NET_NOTIONAL_USD:,.0f}U | 日亏≥{config.RISK_DAILY_MAX_LOSS_PCT * 100:.0f}%熔断")
        if self.profit_target > 0:
            print(f"  止盈目标: 今日盈利 {self.profit_target:g} USDT → 达成后全组合停止开仓（重启前不再进场）")
        if self.loss_limit > 0:
            print(f"  止损上限: 今日亏损 {self.loss_limit:g} USDT → 触发后全组合停止开仓（重启前不再进场）")
        print("  输入 help 查看命令")
        print("=" * 58)

    # ------------------------------------------------------------------
    # 启动流程
    # ------------------------------------------------------------------
    def ask_shared_cash(self) -> bool:
        """询问资金池模式：共享（所有对共用总资金）/ 独立（每对各 PER_PAIR_BALANCE）。"""
        print("资金池模式选择：")
        print("  1) 共享资金池（所有交易对共用总资金，个别对现金不足时自动借用）")
        print("  2) 独立资金池（每对独享 PER_PAIR_BALANCE，各对资金互不干扰）")
        try:
            inp = input("请选择（回车=共享资金池，输入 2 切到独立）: ").strip()
        except EOFError:
            return True
        if inp == "2":
            print("→ 独立资金池模式（每对互不干扰）")
            return False
        print("→ 共享资金池模式（共用总资金）")
        return True

    def ask_pair_count(self) -> int:
        """启动时询问要抓取的高流通交易对数量。回车/非法输入回退默认 NUM_PAIRS。"""
        default = config.NUM_PAIRS
        try:
            inp = input(f"请输入要抓取的高流通交易对数量（回车=默认 {default}）: ").strip()
        except EOFError:
            return default
        if not inp:
            return default
        try:
            n = int(inp)
        except ValueError:
            print(f"输入无效，使用默认 {default}")
            return default
        if n <= 0:
            print(f"数量需大于 0，使用默认 {default}")
            return default
        return n

    def ask_profit_target(self) -> float:
        """启动时询问今日止盈目标金额（USDT）：当日盈利达标后全组合停止开仓，重启前不再进场。

        回车/非法输入用默认（config.PROFIT_TARGET_DEFAULT），0 或负数=不启用。
        """
        default = config.PROFIT_TARGET_DEFAULT
        try:
            inp = input(f"请输入今日止盈目标金额 USDT（回车=默认 {default:g}，0=不启用）: ").strip()
        except EOFError:
            return default
        try:
            target = float(inp) if inp else default
        except ValueError:
            print(f"输入无效，止盈目标不启用")
            return 0.0
        if target <= 0:
            return 0.0
        print(f"[止盈目标] 今日盈利达 {target:g} USDT 后，全部交易对停止开仓（重启前不再进场，现有仓位照常管理）")
        return target

    def ask_loss_limit(self) -> float:
        """启动时询问今日止损金额（USDT）：当日亏损达额后全组合停止开仓，重启前不再进场。

        回车/非法输入用默认（config.LOSS_LIMIT_DEFAULT），0 或负数=不启用。
        """
        default = config.LOSS_LIMIT_DEFAULT
        try:
            inp = input(f"请输入今日止损金额 USDT（回车=默认 {default:g}，0=不启用）: ").strip()
        except EOFError:
            return default
        try:
            limit = float(inp) if inp else default
        except ValueError:
            print(f"输入无效，止损上限不启用")
            return 0.0
        if limit <= 0:
            return 0.0
        print(f"[止损设置] 今日亏损达 {limit:g} USDT 后，全部交易对停止开仓（重启前不再进场，现有仓位照常管理）")
        return limit

    # ------------------------------------------------------------------
    # 余额收益记录（baozhengjin.ini）
    # ------------------------------------------------------------------
    def _apply_saved_balance(self, count: int):
        """启动时设置总资金：总资金始终固定（有 baozhengjin.ini 记录用它，否则用默认），
        交易对数量只决定每对分到多少 = 总资金 ÷ 交易对数量。"""
        saved = config.load_saved_balance()
        if saved and saved > 0:
            total = saved
            src = f"baozhengjin.ini 上期总保证金 {saved:,.2f}"
        else:
            total = config.TOTAL_BALANCE
            src = f"默认总资金 {config.TOTAL_BALANCE:,.0f}"
        config.TOTAL_BALANCE = total
        config.PER_PAIR_BALANCE = total / count
        print(f"[余额] 总资金固定为 {total:,.2f} USDT（{src}），按 {count} 个交易对均分，每对 {total / count:,.2f} USDT")

    def _save_balance_on_exit(self, verbose: bool = True):
        """把当前总保证金写入 baozhengjin.ini（退出时 verbose 打印；状态定时刷新时静默）。

        定时静默保存保证即使直接关闭窗口/异常退出，baozhengjin.ini 里也有最新余额。
        """
        try:
            if self.portfolio is None:
                return
            s = self.portfolio.summary()
            config.save_balance(s["total_equity"])
            if verbose:
                print(f"[余额] 已写入 baozhengjin.ini：当前总保证金 {s['total_equity']:,.2f} USDT")
        except Exception as e:
            if verbose:
                print(f"[余额] 保存失败: {e!r}")

    async def _fetch_high_flow_pairs(self, count: int) -> tuple[list[str], list[dict], dict]:
        """抓取 count 个高流通（交易量靠前）高波动率交易对，返回 (symbols, selected, kmap)。"""
        tickers = await get_24h_tickers()
        # 流动池：交易量前 N 名（数量多时扩大候选池）
        pool_size = max(config.VOLUME_POOL, count * 2)
        pool = sorted(tickers, key=lambda x: x["quote_volume"], reverse=True)[:pool_size]
        pool_syms = [t["symbol"] for t in pool]
        # 拉历史 K 线 → 30 天平均小时振幅 = 稳定波动率
        kmap = await fetch_klines_many(pool_syms, interval="1h",
                                       limit=min(config.VOLATILITY_LOOKBACK_DAYS * 24, 1500))
        volatility = {sym: avg_hourly_amplitude(rows) for sym, rows in kmap.items()}
        kline_counts = {sym: len(rows) for sym, rows in kmap.items()}
        selected = select_pairs(tickers, volatility, kline_counts, num=count, pool_size=pool_size)
        symbols = [t["symbol"] for t in selected]
        return symbols, selected, kmap

    async def async_setup(self, count: int) -> tuple[list[str], dict | None, dict]:
        """抓取 count 个高流通高波动交易对 → 写入 jiaoyi.txt → 交易。

        抓取失败时回退到 jiaoyi.ini → FALLBACK_SYMBOLS。
        """
        kmap: dict = {}
        symbols: list[str] = []
        precision: dict = {}
        try:
            symbols, selected, kmap = await self._fetch_high_flow_pairs(count)
            self.selected = selected
            self.pair_source = "count"
            config.save_trading_pairs(symbols)  # 写入 jiaoyi.ini
            print(f"[选对] 已抓取 {len(symbols)} 个高流通高波动交易对并写入 jiaoyi.ini: {', '.join(symbols)}")
        except Exception as e:
            print(f"[选对] 在线抓取失败({e})")
            custom = config.load_trading_pairs()
            if custom:
                symbols = custom
                self.selected = []
                self.pair_source = "custom"
                print(f"[选对] 使用 jiaoyi.ini 指定 {len(symbols)} 个交易对: {', '.join(symbols)}")
            else:
                symbols = config.FALLBACK_SYMBOLS[:config.NUM_PAIRS]
                self.selected = []
                self.pair_source = "fallback"
                print(f"[选对] 使用回退交易对: {', '.join(symbols)}")
        # 拉取交易对的官方精度（step/tick/最小下单）
        try:
            precision = await fetch_precision(symbols)
        except Exception:
            precision = {}
        # 高波动时段统计
        window = None
        try:
            avg = volatility_by_hour(kmap, symbols)
            window = high_volatility_window(avg)
            if window:
                print(f"[时段] 高波动时段: {describe_window(window)}")
        except Exception as e:
            print(f"[时段] 统计失败({e})，全天可交易")
        return symbols, window, precision

    def start_input_thread(self):
        def worker():
            while True:
                try:
                    line = input("cmd> ")
                except EOFError:
                    break
                self.cmd_q.put(line)
        threading.Thread(target=worker, daemon=True).start()

    async def run(self):
        # -1) 询问是否共享资金池
        self._shared_cash = self.ask_shared_cash()
        # 0) 启动时先询问抓取数量
        count = self.ask_pair_count()
        self._apply_saved_balance(count)     # 读取 baozhengjin.ini 余额，设为本次总资金
        self.profit_target = self.ask_profit_target()   # 今日止盈目标（0=不启用）
        self.loss_limit = self.ask_loss_limit()         # 今日止损金额（0=不启用）
        self.print_banner(count)
        if config.BINANCE_API_KEY and config.BINANCE_API_SECRET:
            # 启动时立即拉一次真实账户：存进缓存（置顶区用），再显示一次
            try:
                from api_client import fetch_account_info, print_top_banner
                self._account_cache = await fetch_account_info(
                    config.BINANCE_API_KEY, config.BINANCE_API_SECRET)
                print_top_banner(self._account_cache)
            except Exception as e:
                print(f"[账户] 展示失败：{type(e).__name__}: {e}")

        # 1) 抓取 count 个高流通高波动交易对
        symbols, window, precision = await self.async_setup(count)
        self.portfolio = Portfolio(symbols, window, precision,
                                   shared_cash=self._shared_cash)
        self.portfolio.profit_target = self.profit_target   # 止盈目标注入组合风控
        self.portfolio.loss_limit = self.loss_limit         # 止损金额注入组合风控
        self.print_pairs()

        # 2) 预载历史 1m K 线给趋势策略，让均线信号立即可用
        if config.TREND_KLINES_SEED > 0:
            try:
                kmap = await fetch_klines_many(symbols, interval="1m", limit=config.TREND_KLINES_SEED)
                for sym, rows in kmap.items():
                    if sym in self.portfolio.ctx:
                        self.portfolio.ctx[sym].strategy.seed_candles(rows)
                print(f"[趋势] 已预载 {len(kmap)} 个交易对的历史 1m K 线（{config.TREND_KLINES_SEED} 根/对）")
            except Exception as e:
                print(f"[趋势] 预载 K 线失败({e})，将仅用实时数据累积")

        # 3) 连接行情
        self.ws = BinanceFuturesWS(symbols, self.on_market_update, self._on_connect, self._on_disconnect)
        self.ws_task = asyncio.create_task(self.ws.connect())
        self.strategy_task = asyncio.create_task(self.strategy_loop())
        # 3.5) 币安账户缓存刷新（置顶区数字实时更新；未配置则空跑）
        self.account_refresh_task = asyncio.create_task(self.account_refresh_loop())
        try:
            while not self._stop:
                try:
                    line = self.cmd_q.get_nowait()
                except queue.Empty:
                    line = None
                if line is not None:
                    try:
                        self.process_command(line)
                    except SystemExit:
                        raise                    # quit 等命令仍需退出，抛给外层处理
                    except Exception as e:
                        print(f"[命令] 处理出错: {e!r}（已跳过，继续运行）")
                if time.time() - self.last_status > config.STATUS_INTERVAL:
                    self.last_status = time.time()
                    try:
                        self.print_status()
                    except Exception as e:
                        # 刷新出错不能终止主循环，否则控制台会停止刷新；打印原因便于定位
                        print(f"[状态] 刷新出错: {e!r}（不影响运行，若反复出现请反馈此行）")
                    self._save_balance_on_exit(verbose=False)   # 定时静默刷新余额记录，直接关窗口也有最新值
                await asyncio.sleep(0.2)
        except SystemExit:
            pass
        finally:
            self._stop = True
            if self.ws:
                self.ws.stop()
            for t in (self.ws_task, self.strategy_task, self.account_refresh_task):
                if t and not t.done():
                    t.cancel()
            # 给被取消的任务片刻时间真正结束并关闭 aiohttp 连接，避免 Windows 退出时 proactor 告警
            await asyncio.sleep(0.1)
            self._save_balance_on_exit()     # 关闭时把当前总保证金写入 baozhengjin.ini
            print("\n已退出。")


if __name__ == "__main__":
    try:
        asyncio.run(App().run())
    except KeyboardInterrupt:
        print("\n收到中断，退出。")