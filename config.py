# -*- coding: utf-8 -*-
"""全局配置。所有可调参数集中在这里，改完直接生效。"""
from __future__ import annotations

import os

# ==================== 行情接入 ====================
# 币安 USDT 永续合约 WebSocket 地址（{streams} 会被替换为订阅流）
# 注：币安 2026-03-06 升级，U本位合约 WS 按数据类别拆分为三个地址：
#   Public（高频行情 bookTicker 等）→ wss://fstream.binance.com/public
#   Market（常规行情 markPrice 等） → wss://fstream.binance.com/market
#   Private（用户数据）             → wss://fstream.binance.com/private
# 旧地址 stream.binancefuture.com 已于 2026-04-23 下线：只对 Public 类（bookTicker）残留部分转发，
# Market 类（markPrice）不再路由 —— 这正是此前「bookTicker 通、markPrice 死」行情冻结的根因。
# 两类分属两个地址，bookTicker 走 Public、markPrice 走 Market，需要两条 WS 连接（见 binance_ws.py）
WS_PUBLIC_URL = "wss://fstream.binance.com/public/stream?streams={streams}"     # 高频行情：bookTicker
WS_MARKET_URL = "wss://fstream.binance.com/market/stream?streams={streams}"     # 常规行情：markPrice@1s

# 本地代理地址（Clash 等）。国内网络连币安必填；连不上时留空字符串 "" 直连
WS_PROXY = "http://127.0.0.1:7897"

# ==================== 币安账户展示（只读） ====================
# 启动时（banner 之后）用私密 REST API 拉取真实账户信息并展示：用户名 / USDT 余额 /
# 现货代币持仓 / 合约仓位 / 现货仓位。
# 仅用于显示，绝不下单 —— 请在币安后台创建【只读】（Enable Reading）权限的 API Key。
# 留空则跳过展示，不影响其他功能。
BINANCE_API_KEY = ""
BINANCE_API_SECRET = ""
# 用户名：币安私密 API 不返回 UID/昵称，只能手动填一个展示名（如你的币安昵称/UID）
BINANCE_ACCOUNT_NAME = "我的币安账户"

# 行情看门狗：防止「连接还活着、但行情不推了」导致行情/做单全部冻结
# 代理链路可能静默中断 markPrice 流，此时 WS 不报错也不推数据，唯一办法是主动断开重连
WS_STALE_SECONDS = 30        # markPrice 超过该秒数无更新即判定行情过期，强制断开连接触发重连
WS_RECONNECT_SECONDS = 3     # 断线/重连之间的等待间隔（秒）

# 监控的交易对（离线/选对失败时的回退；在线会自动筛选 TOP 交易对）
SYMBOL = "ETHUSDT"

# ==================== 组合模式 ====================
TOTAL_BALANCE = 1000.0        # 总模拟资金 (USDT)
NUM_PAIRS = 10                # 交易对数量
PER_PAIR_BALANCE = 100.0      # 每个交易对的独立资金池 (USDT)
# 首仓固定保证金见 TREND_POSITION_MARGIN_USD（趋势策略建仓用）

# 自动选对（在线启动时从币安拉取）
VOLUME_POOL = 30              # 流动池：交易量前 N 名
POOL_MIN_DAYS = 28            # 只保留上市≥该天数（排除刚上线的新币）
REQUIRE_SYMBOLS = ["ETHUSDT"] # 必须包含的交易对（ETH 必须在列）
FALLBACK_SYMBOLS = [          # 离线/选对失败时的回退交易对
    "ETHUSDT", "BTCUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT",
    "1000PEPEUSDT", "SUIUSDT", "WIFUSDT", "ADAUSDT", "AVAXUSDT",
]

# 高波动时段（自动统计历史 K 线得出，供参考与择时交易）
VOLATILITY_LOOKBACK_DAYS = 30 # 统计历史天数
VOLATILITY_WINDOW_HOURS = 4   # 高波动连续时段长度（小时）
TRADE_ONLY_IN_WINDOW = False   # 自动策略只在统计出的高波动时段内交易

# ==================== 费用与滑点（币安官方标准） ====================
TAKER_FEE = 0.0005               # 吃单费率 0.05%（官方标准费率，普通用户）
MAKER_FEE = 0.0002               # 挂单费率 0.02%（官方标准费率）
SLIPPAGE_PCT = 0.0               # 吃单滑点（小数，0.001 = 0.1%）

# ==================== 精度与最小下单（币安官方） ====================
# 每交易对: step_size(数量最小增量) / tick_size(价格最小增量) / min_notional(最小下单名义价值 USDT)
PRECISION = {
    "BTCUSDT": {"step_size": 0.001, "tick_size": 0.1, "min_notional": 50},
    "ETHUSDT": {"step_size": 0.001, "tick_size": 0.01, "min_notional": 20},
}
DEFAULT_PRECISION = {"step_size": 0.001, "tick_size": 0.01, "min_notional": 20}

# ==================== 维持保证金率档位（币安官方 BTCUSDT USDT-M 永续） ====================
# 按持仓名义价值(USDT)分档： (档位上限, 维持保证金率, 该档最大杠杆)
# 注：币安会不定期调整档位，请以官方 leverageBracket 接口或官网为准
MMR_TIERS = [
    (50_000,      0.004, 125),   # 0  ~ 5万     USDT: MMR 0.40%, 最大杠杆 125x
    (250_000,     0.005, 100),   # 5  ~ 25万    USDT: MMR 0.50%, 最大杠杆 100x
    (1_000_000,   0.01,  50),    # 25 ~ 100万   USDT: MMR 1.00%, 最大杠杆 50x
    (10_000_000,  0.025, 20),    # 100~1000万   USDT: MMR 2.50%, 最大杠杆 20x
    (20_000_000,  0.05,  10),    # 1000~2000万  USDT: MMR 5.00%, 最大杠杆 10x
]

# ==================== 自动策略 ====================
AUTO_ENABLED = True             # 启动时是否自动开启策略
STRATEGY_EVAL_SECONDS = 1.0      # 策略评估频率（秒）
LEVERAGE = 10                    # 目标杠杆倍数（实际按档位最大杠杆封顶）

# 策略选择：默认利弗莫尔关键点滚仓；切换 "trend_pyramid" 回到原趋势跟随+浮盈加仓
STRATEGY = "livermore_roll"      # "livermore_roll"（利弗莫尔关键点突破 + 滚仓）/ "trend_pyramid"（原趋势加仓）

# ---- 趋势识别：基于 1 分钟 K 线收盘价的快/慢 EMA ----
TREND_FAST_MA = 8                # 快线 EMA 周期（根 1m K 线）
TREND_SLOW_MA = 21               # 慢线 EMA 周期（根 1m K 线）
TREND_MIN_CANDLES = 21           # 至少累计该数量 K 线后才开始交易（均线未稳定前不开仓）
TREND_DEADBAND_PCT = 0.001       # 快慢线差小于该比例视为无趋势(FLAT)，避免信号抖动反复开仓
TREND_MAX_CANDLES = 1000         # 本地保留的最大 K 线数量（超出丢弃最早）
TREND_KLINES_SEED = 240          # 启动时从币安预载多少根历史 1m K 线（让均线立即可用，0=不预载）

# ---- 建仓 ----
# 首仓固定保证金（USDT）。开仓名义 = 保证金 × 当前杠杆（杠杆可用 lev 命令调整，保证金保持固定）
TREND_POSITION_MARGIN_USD = 10   # 例：10x 杠杆 → 首仓名义 = 10 × 10 = 100 USDT，保证金始终 10 USDT

# ---- 浮盈加仓：持仓盈利后，回调时用盈利加仓 ----
TREND_PYRAMID_RATIO = 1.0        # 每次加仓名义 = 当前浮盈 × 该比例（1.0 = 按全部浮盈加仓）
TREND_MIN_ADD_USD = 20           # 加仓名义价值下限（低于该值时按此下限加；也须 ≥ 交易所最小下单）
TREND_MAX_ADDS = 3               # 最多加仓次数（达到后不再加）
TREND_PULLBACK_PCT = 0.005       # 回调阈值：加仓后创新高再回撤该比例触发加仓（0.005 = 0.5%）

# ---- 离场 ----
TREND_TAKE_PROFIT = 5.0          # 止盈：盈利达保证金的 500%（@10x ≈ 价格 +50%）；主要由移动止盈离场
TREND_STOP_LOSS = 0.3            # 止损：亏损达保证金的 30%（@10x ≈ 价格 -3%）
TREND_TRAIL_ACTIVATE_PCT = 0.02  # 浮盈达入场价的 2% 后启动移动止盈
TREND_TRAIL_STOP_PCT = 0.01      # 移动止盈：从持仓峰值回撤 1% 平仓
TREND_TIME_LIMIT = 60 * 60 * 8   # 持仓时限（秒）= 8 小时
TREND_COOLDOWN_SECONDS = 60      # 平仓后冷却（秒）：盈利/中性离场用，期间不开新仓，避免频繁进出

# ---- 亏损离场冷却 + 连败降频（越亏越冷静，两种策略共用；以实际盈亏<0 为「亏损离场」） ----
# 实际阶梯：连败1次 120 分钟 → 2次 320 分钟 → 3次及以后 720 分钟封顶
TREND_LOSS_COOLDOWN_SECONDS = 60 * 120     # 首次亏损离场冷却秒数：120 分钟（连败1次档）
TREND_LOSS_STREAK_BAN = 2                  # 连续亏损离场达到该次数后，冷却开始逐次加乘
TREND_LOSS_STREAK_MULT = 2.667             # 此后每多连败一次，冷却再乘该系数（120×2.667=320，再乘即超 720 上限）
TREND_LOSS_STREAK_MAX_COOLDOWN = 60 * 720  # 连败冷却上限（秒）：720 分钟 = 12 小时封顶，连败3次即触顶

# ==================== 组合风控（所有策略共用；加密币相关性极高，防「全部同方向一锅端」） ====================
RISK_MAX_POSITIONS = 6            # 同时持仓的交易对数量上限：10 对同向 = 同一笔赌注放大 10 倍，多仓位不加风险只加相关性
RISK_MAX_MARGIN_PCT = 0.60        # 已用保证金 ÷ 总权益 的上限，超过后暂停新开仓与加仓（始终留现金缓冲）
RISK_MAX_NET_NOTIONAL_USD = 3000  # 净方向敞口上限（|多头名义-空头名义|，USDT）：同向名义吃满后不再加同向仓
RISK_DAILY_MAX_LOSS_PCT = 0.05    # 当日熔断：权益自 UTC 日初回撤达该比例，暂停新开仓至 UTC 次日（现有仓位照常管理）
TRADE_JOURNAL = "trade_log.csv"   # 交易日志 CSV：每轮平仓追加 时间/交易对/盈亏/离场原因，供跨会话复盘；空串=关闭
PROFIT_TARGET_DEFAULT = 0.0       # 启动时回车默认的止盈目标金额（USDT）：当日盈利达标后全组合停止开仓（重启前不再进场）；0=不启用
LOSS_LIMIT_DEFAULT = 0.0          # 启动时回车默认的止损金额（USDT）：当日亏损达额后全组合停止开仓（重启前不再进场）；0=不启用

# ==================== 利弗莫尔战法（STRATEGY="livermore_roll" 时生效） ====================
# 核心思想（利弗莫尔战法，无浮盈回调加仓）：
#   关键点突破进场 → 试探建仓 → 有盈利且创新高时逐级金字塔加仓（只追强势、不接回调、绝不摊平）
#   → 让利润奔跑（宽松移动止盈）→ 关键点止损迅速离场
# 加仓/止损止盈/冷却等共用：TREND_PYRAMID_RATIO、TREND_MIN_ADD_USD、TREND_MAX_ADDS、
# TREND_TAKE_PROFIT、TREND_STOP_LOSS、TREND_COOLDOWN_SECONDS

# ---- 关键点 (pivotal point)：最近 N 根 1m K 线的最高价/最低价 ----
LIVERMORE_PIVOT_PERIOD = 45          # 关键点窗口（根 K 线，从 30 提高到 45，减少假突破）
LIVERMORE_BREAKOUT_PCT = 0.001       # 突破缓冲：价格须超过关键点该比例才算突破（滤掉临界假突破）
LIVERMORE_REQUIRE_TREND = True       # 突破进场时是否要求均线趋势同向（已开启，减少假突破）；
                                     #  改为 True 可滤掉逆势假突破，但会挡掉「横盘→突破」常见形态）

# ---- 试探建仓（利弗莫尔「先试探，行情证明你了再加」） ----
LIVERMORE_PROBE_MARGIN_USD = 40      # 试探仓保证金（USDT），10x 下名义 400U，约 +5% 涨幅浮盈 20U 即达首次加仓门槛（min_notional 20U）

# ---- 利弗莫尔金字塔加仓（只在有盈利且创新高时逐级顺势加仓） ----
LIVERMORE_ADD_BREAKOUT_PCT = 0.005   # 比上次加仓价再创新高该比例才加仓（0.005 = 0.5%），逐级抬升，减少噪音加仓

# ---- 多时间周期过滤（15m 趋势判断） ----
LIVERMORE_USE_HIGHER_TF = True       # 是否启用高周期趋势过滤（启用后只在 15m 趋势同向时才进场）
LIVERMORE_HIGHER_TF_MINUTES = 15     # 高周期分钟数（15 = 15 分钟 K 线）
LIVERMORE_HIGHER_TF_FAST = 5         # 高周期快线 EMA（5根15m=75min，种子240根1m立即可用）
LIVERMORE_HIGHER_TF_SLOW = 12        # 高周期慢线 EMA（12根15m=3h，种子240根1m=16根15m够用）
LIVERMORE_HTF_DEADBAND_PCT = 0.002   # 15m 趋势死区（比 1m 的 0.001 更宽，滤掉高周期噪音抖动）
LIVERMORE_HTF_FLAT_ALLOW = False     # 15m 为 FLAT(横盘)时是否放行进场（False=横盘不进，减少横盘期双向假突破打脸）

# ---- 离场（本次优化核心：反转离场挂 15m + 利润保护，解决「赚得少、反转全亏」） ----
LIVERMORE_USE_PIVOT_STOP = True      # 关键点止损：跌破关键点离场，且止损位只上移不下移
LIVERMORE_HTF_EXIT_BREAK = True      # 反转离场：跌破上一根已收盘 15m K 线低点(多)/涨破高点(空)即离场（比均线交叉更早）
LIVERMORE_TRAIL_ACTIVATE_PCT = 0.01  # 移动止盈启动阈值（浮盈占入场价 1%，比原 3% 更易启动，让利润保护尽早生效）
LIVERMORE_TRAIL_GIVEBACK = 0.35      # 移动止盈：从峰值利润回吐该比例即平仓（0.35=回吐35%，比固定 2% 回撤更贴合山寨波动）
LIVERMORE_BREAKEVEN_ACTIVATE_PCT = 0.015  # 浮盈达入场价 1.5% 后启动保本止损（回落至入场价即离场，消灭「浮盈变亏损」）
LIVERMORE_STOP_ATR_MULT = 1.5        # 硬止损宽度 = 15m ATR × 该倍数（低波动时更早离场；0=关闭，只用保证金百分比止损）
                                     # 注：10x 杠杆下保证金 -30% 已≈价格 -3%，止损离强平价(约-9.6%)有充足缓冲；要更宽再继续降杠杆

# ---- 进场质量（少做、做精：自适应缓冲 + 资金费过滤） ----
LIVERMORE_BREAKOUT_ATR_MULT = 0.25   # 突破缓冲 = max(固定缓冲, 15m ATR×该倍数÷价格)：波动越大缓冲越宽，滤掉噪音突破（0=只用固定缓冲）
LIVERMORE_FUNDING_FILTER = 0.0003    # 资金费率过滤（每 8h）：费率 > 该值不追多（多头拥挤、费用高），< -该值不追空；0=关闭

# ---- 风险预算仓位（按波动率收敛试仓名义，控制单笔最大亏损） ----
LIVERMORE_RISK_MAX_LOSS_USD = 25.0   # 试探仓被 ATR 止损打掉的最大亏损预算（USDT，约总资金 2.5%）；名义 = min(试探名义, 预算÷止损距离%)，0=关闭

# ---- 滞涨离场（利弗莫尔：进场后行情不马上证明你对，就当它错了） ----
LIVERMORE_STAGNATION_MINUTES = 45    # 试探仓持仓超过该分钟数且从未加仓（行情未证明）→ 检查是否滞涨（0=关闭）
LIVERMORE_STAGNATION_MIN_PNL = 0.10  # 上述时限内峰值浮盈未达保证金的该比例（0.10=+10%@10x≈价格+1%）→ 承认突破失败，离场

# ---- 保本止损含费用缓冲 ----
LIVERMORE_BREAKEVEN_BUFFER_PCT = 0.0012  # 保本位 = 入场价 ± 该比例（覆盖开平吃单手续费约 0.1%，保本离场不再倒贴手续费）

# ==================== 显示 ====================
STATUS_INTERVAL = 10             # 每 N 秒自动打印一次账户状态

# ==================== 交易对配置文件 ====================
TRADING_INI = "jiaoyi.ini"       # 交易对配置文件（相对本项目根目录），启动时按数量抓取后写入


def save_trading_pairs(symbols: list[str]) -> None:
    """把交易对列表写入 jiaoyi.ini 的 [trading] pairs 行（保留注释，覆盖 pairs）。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), TRADING_INI)
    out = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            out = f.read().splitlines()
    in_trading, found = False, False
    result = []
    for line in out:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_trading = (stripped == "[trading]")
            result.append(line)
            continue
        if in_trading and "=" in line and stripped.split("=", 1)[0].strip().lower() == "pairs":
            result.append("pairs = " + ", ".join(symbols))
            found = True
            continue
        result.append(line)
    if not found:
        if result:
            result.append("")
        result.append("[trading]")
        result.append("pairs = " + ", ".join(symbols))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(result) + "\n")


def load_trading_pairs() -> list[str]:
    """从 jiaoyi.ini 读取自定义交易对列表（大写去重）。

    支持：`pairs = A, B`、每行一个、全角逗号；注释行（#/;）与 [节名] 忽略。
    文件缺失、为空或无有效交易对时返回空列表（调用方走原在线选对逻辑）。
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), TRADING_INI)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read()
    except (UnicodeDecodeError, OSError):
        try:  # Windows 记事本可能保存为 GBK
            with open(path, encoding="gbk") as f:
                lines = f.read()
        except (UnicodeDecodeError, OSError):
            return []
    pairs: list[str] = []
    try:
        for raw in lines.splitlines():
            line = raw.strip()
            if not line or line.startswith(("#", ";", "[")):
                continue
            if "=" in line:
                _, val = line.split("=", 1)
            else:
                val = line
            for tok in val.replace(",", " ").replace("，", " ").split():
                tok = tok.strip().upper()
                if tok and tok not in pairs:
                    pairs.append(tok)
    except Exception:
        return []
    return pairs


# ==================== 余额收益记录（baozhengjin.ini） ====================
# 关闭工具时把当前总保证金写入该文件，下次启动时读取并作为本次总资金，实现余额/收益跨会话延续
BALANCE_INI = "baozhengjin.ini"    # 余额收益记录文件（相对本项目根目录）


def load_saved_balance() -> float | None:
    """从 baozhengjin.ini 读取上次关闭时的总保证金（余额）。无记录/无效返回 None。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), BALANCE_INI)
    if not os.path.exists(path):
        return None
    content = None
    for enc in ("utf-8", "gbk"):    # Windows 记事本可能存为 GBK
        try:
            with open(path, encoding=enc) as f:
                content = f.read()
            break
        except (UnicodeDecodeError, OSError):
            continue
    if content is None:
        return None
    try:
        for line in content.splitlines():
            line = line.strip()
            if line.lower().startswith("total_balance"):
                val = line.split("=", 1)[1].strip().replace(",", "").replace(" ", "")
                amount = float(val)
                return amount if amount > 0 else None
    except (ValueError, IndexError):
        return None
    return None


def save_balance(amount: float) -> None:
    """把当前总保证金写入 baozhengjin.ini（覆盖旧值）。"""
    from datetime import datetime
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), BALANCE_INI)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(path, "w", encoding="utf-8") as f:
        f.write("# 余额收益记录：关闭工具时写入当前总保证金，下次启动时读取作为初始总资金\n")
        f.write("[balance]\n")
        f.write(f"total_balance = {amount:.2f}\n")
        f.write(f"saved_at = {now}\n")