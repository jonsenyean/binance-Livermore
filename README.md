# 币安永续 · 组合纸面交易工具（Paper Trading）

**实盘行情 + 虚拟资金**：行情 100% 来自币安真实数据，成交和资金 100% 本地模拟，全程不碰真实订单与真实资金。
**盈亏算法按币安官方规则**：官方精度、分档维持保证金率、官方手续费与资金费公式。

> **公开发布提醒**：本项目适合公开的是源码、配置模板和测试代码。公开 GitHub 前请移除本地运行数据 `baozhengjin.ini`、`trade_log.csv`、`test_out.txt`、`huihua.md` 以及所有备份文件（如 `*.bak*`）。当前配置中的 API Key 和 Secret 为空，但任何 API 密钥都不得提交到 GitHub。项目只做纸面交易，不构成投资建议。

## 项目定位

这是一个基于 Python 和 `asyncio` 的币安 USDT 永续合约纸面交易工具：从币安获取真实公共行情，在本地模拟账户、撮合、手续费、资金费、保证金、强平价和策略盈亏。默认启用利弗莫尔关键点滚仓策略，不会向币安提交真实订单。账户 API 仅用于可选的只读信息展示。

## 组合模式

| 项目 | 配置 |
|---|---|
| 总资金 | 1000 USDT（`TOTAL_BALANCE`），**始终固定**（有 `baozhengjin.ini` 记录时用记录值） |
| 交易对 | 启动时询问数量（回车用默认 10，`NUM_PAIRS`）：交易量前 30 名（`VOLUME_POOL`）中，排除上市不足 28 天（`POOL_MIN_DAYS`）的新币，按 30 天平均小时振幅排序取前 N，**强制包含 ETH**（`REQUIRE_SYMBOLS`） |
| 每对资金 | 独立资金池 = 总资金 ÷ 交易对数量（`PER_PAIR_BALANCE` 按此计算） |
| 每笔建仓 | 首仓固定保证金 10 USDT（`TREND_POSITION_MARGIN_USD`），开仓名义 = 保证金 × 当前杠杆（10x → 100 USDT），低于该对官方最小下单时自动提高到最小下单 |
| 高波动时段 | 统计 30 天 1h K 线每小时平均振幅，滑动窗口找出连续 4 小时（`VOLATILITY_WINDOW_HOURS`）平均波动最高的时段（UTC），自动交易仅在该时段内进行（`TRADE_ONLY_IN_WINDOW`） |
| 组合风控 | 持仓 ≤6 对、保证金占用 ≤60%、净方向敞口 ≤3000 USDT、当日(UTC)权益回撤 ≥5% 熔断——只限自动策略，手动命令不受限 |
| 止盈目标 / 止损上限 | 启动时各输入金额（回车=不启用）：当日盈利达标或亏损达额 → 全组合停止开仓，重启前不再进场，特大字横幅通报 |
| 止盈/止损 | 按**仓位保证金（本金）**计算：盈利 ≥500% 止盈、亏损 ≥50% 止损（`TREND_TAKE_PROFIT`/`TREND_STOP_LOSS`），另有移动止盈 |

## 功能特性

- **实时行情**：**两条独立 WS 连接**分别订阅 bookTicker（Public 类）与 markPrice@1s（Market 类），经本地代理，断线自动重连。币安 2026-03-06 起 U本位合约 WS 按数据类别拆分地址（旧地址 `stream.binancefuture.com` 已下线，markPrice 不再路由）；每条连接带行情看门狗——超过 `WS_STALE_SECONDS` 无更新即强制断开重连，行情冻结自动自愈。markPrice 同时累加成 1m K 线供趋势判断
- **启动抓取交易对**：启动时先询问要抓取的数量，随后从币安抓取对应数量的「高流通（交易量靠前）+ 高波动率」交易对，自动写入 `jiaoyi.ini`（保留文件注释）并用于交易；抓取失败时回退到 `jiaoyi.ini` 已有列表 → `FALLBACK_SYMBOLS`
- **本地模拟撮合**（币安官方）：买入单吃卖一、卖出单吃买一；官方精度取整（启动时拉 exchangeInfo）
- **趋势跟随 + 浮盈加仓策略（每个交易对独立）**：1m 快/慢 EMA 识别趋势 → 按 `TREND_POSITION_MARGIN_USD` 固定保证金建仓 → 持仓盈利且回调时用浮盈加仓 → 止盈/止损/移动止盈/趋势反转离场
- **利弗莫尔战法（默认，`STRATEGY="livermore_roll"`）**：
  关键点突破进场 → 试探建仓 → 有盈利创新高逐级金字塔加仓 → 关键点止损 / 让利润奔跑（详见下方「利弗莫尔战法」一节）
- **组合风控 Governor**：持仓数上限（`RISK_MAX_POSITIONS`）+ 保证金占用上限（`RISK_MAX_MARGIN_PCT`）+ **净方向敞口**上限（`RISK_MAX_NET_NOTIONAL_USD`，多头名义−空头名义）+ 当日权益回撤熔断（`RISK_DAILY_MAX_LOSS_PCT`，UTC 日切自动恢复）——加密币相关性极高，防「全组合押同一个方向一锅端」；只限自动策略新开仓/加仓，手动命令与现有仓位离场不受限
- **止盈目标 / 止损上限**：启动时各输入一个金额（回车=不启用）——当日盈利达标（特大字「目标达成」横幅）或当日亏损达止损金额（特大字「止损触发」横幅）后，**全部交易对停止开仓/加仓**（锁存，重启前不再进场，现有仓位离场照常）
- **战绩统计与交易日志**：胜率 / 利润因子 / 平均盈亏 / 期望值每轮聚合统计（`stats` 命令），每笔平仓追加写入 `trade_log.csv`（utf-8-sig，Excel 直接打开）
- **盈亏统计**：每对明细（未实现/已实现/净盈亏）+ 组合汇总
- **余额收益记录（`baozhengjin.ini`）**：关闭工具时把当前总保证金写入 `baozhengjin.ini`，下次启动自动读取并作为本次总资金——余额与收益跨会话延续，重启不归零

## 利弗莫尔战法（默认策略）

把利弗莫尔《股票大作手回忆录》的交易原则全部落地（**无浮盈回调加仓**）：

| 利弗莫尔原则 | 本工具实现 |
|---|---|
| **关键点突破进场** | 最近 `LIVERMORE_PIVOT_PERIOD`(45) 根 1m K 线的高低点为关键点；价格突破前高（+自适应缓冲，见「进场质量」行）开多、跌破前低开空——不预测、等关键点被突破才进场 |
| **均线趋势过滤** | `LIVERMORE_REQUIRE_TREND`(True)：要求突破与 1m EMA 均线同向，滤掉逆势假突破（会挡掉「横盘→突破」形态） |
| **多时间周期过滤** | `LIVERMORE_USE_HIGHER_TF`(True)：只在 15m 趋势同向时进场，15m 横盘(FLAT)不放行（`LIVERMORE_HTF_FLAT_ALLOW`），减少横盘期双向假突破 |
| **试探建仓** | 首仓用 `LIVERMORE_PROBE_MARGIN_USD`(40) 试探仓，「先试探，行情证明你了再加」 |
| **进场质量** | 突破缓冲取 max(固定 0.1%, 0.25×15m ATR/价格)（`LIVERMORE_BREAKOUT_ATR_MULT`）——波动大的币自动放宽缓冲滤假突破；资金费率过滤（`LIVERMORE_FUNDING_FILTER`）：费率 >+0.03%/8h 不追多、<−0.03% 不追空（拥挤方向不接盘） |
| **风险预算** | 单笔最大亏损封顶（`LIVERMORE_RISK_MAX_LOSS_USD`=25U）：试探仓名义 = min(固定名义, 25U÷止损距离%)——止损越远仓位越小，无论止损宽窄单笔亏损恒定，杜绝「止损宽=亏更多」 |
| **金字塔加仓（有盈利就按利弗莫尔来）** | 只在**有盈利且创新高**时逐级顺势加仓：比上次加仓价再突破 `LIVERMORE_ADD_BREAKOUT_PCT`(0.5%) 加一档——只追强势、不接回调、逐级抬升、绝不摊平（`TREND_MAX_ADDS` 次封顶） |
| **让利润奔跑** | 移动止盈回吐峰值利润 35% 离场（`LIVERMORE_TRAIL_GIVEBACK`），浮盈 +1.5% 启动含费保本（`LIVERMORE_BREAKEVEN_ACTIVATE_PCT`，保本位=入场价±0.12% 覆盖 0.1% taker 往返费，杜绝「保本出场仍倒贴手续费」），由移动止盈/保本而非固定止盈离场 |
| **反转离场挂 15m** | 跌破上一根已收盘 15m K 线低点(多)/涨破高点(空) 或 15m EMA 反向即离场（`LIVERMORE_HTF_EXIT_BREAK`），比 1m 均线更早 |
| **滞涨离场（假突破认错）** | 突破进场后 45 分钟（`LIVERMORE_STAGNATION_MINUTES`）既没触发过加仓、峰值浮盈又不足保证金 10%（`LIVERMORE_STAGNATION_MIN_PNL`）→ 判定突破失败，主动离场，不等止损被打 |
| **迅速止损** | 保证金止损 + ATR 自适应止损（`LIVERMORE_STOP_ATR_MULT`）+ `LIVERMORE_USE_PIVOT_STOP` 关键点止损：跌破关键点止损位离场，止损位随关键点上移、只上移不下移 |
| **耐心等待** | 只在关键点突破时进场；平仓后须突破**更高**关键点才再次进场（`last_break` 守卫），配合冷却期避免频繁进出；亏损离场冷却 120 分钟，连败 2 次 320 分钟、3 次起 12 小时封顶（越亏越冷静） |

切换回原趋势策略：`config.py` 里把 `STRATEGY = "trend_pyramid"`。

## 盈亏算法（币安官方）

| 项目 | 公式 | 实现 |
|---|---|---|
| 未实现盈亏 | 多 `(标记价-入场价)×数量`；空反向，用标记价 | ✅ |
| 已实现盈亏 | `(出场价-入场价)×数量 - 开平仓手续费` | ✅ |
| 手续费 | `名义价值 × 费率`，吃单 0.05% / 挂单 0.02% | ✅ |
| 资金费 | `标记价 × 数量 × 资金费率`，正费率多头付费 | ✅ |
| 初始保证金 | `名义价值 / 杠杆`，杠杆按档位最大杠杆封顶 | ✅ |
| 维持保证金 | 按名义价值查官方分档表（0.4%/0.5%/1%/2.5%/5%） | ✅ |
| 强平价 | 多 `(入场名义-已锁保证金)/(数量×(1-MMR))`；空反向（含加仓累计保证金，与币安一致） | ✅ |
| 下单精度 | 数量向下取整到 step_size、价格舍入到 tick_size（官方 exchangeInfo） | ✅ |
| 最小下单 | 各交易对官方 min_notional（BTC 50 / ETH 20 / 其他拉取） | ✅ |

## 快速开始

```bash
cd binance_paper_trading
pip install -r requirements.txt
python main.py
```

> 不需要任何 API Key —— 本地模拟只读公共行情，不下真单。
> 国内网络需要代理：`WS_PROXY = http://127.0.0.1:7897`（改到你的 Clash 端口）。
> 连不上/不更新行情时先跑 `python check_proxy.py` 诊断：代理是否正常、新分类地址（Public/Market）能否收到数据。

## 命令一览

| 命令 | 说明 |
|---|---|
| `status` / `st` | 组合盈亏汇总表 |
| `pairs` | 选中的交易对（交易量 / 30天波动率 / 最新价） |
| `window` | 高波动时段与当前是否在窗口 |
| `buy <对> <数量>` / `buyu <对> <USDT>` | 开多 |
| `short <对> <数量>` / `shortu <对> <USDT>` | 开空 |
| `close <对> [数量]` | 平仓 |
| `auto [on\|off]` | 自动策略开关（默认利弗莫尔关键点滚仓，可切 `trend_pyramid`） |
| `lev <倍数>` | 调整杠杆（新仓位生效） |
| `tp <小数>` / `sl <小数>` | 止盈/止损（=保证金比例，默认 5.0/0.5） |
| `price <价格>` | 离线模式手动喂价（全部交易对） |
| `hist [交易对]` | 交易历史 |
| `stats` / `tj` | 策略战绩（胜率/利润因子/期望值每轮）+ 组合风控敞口 |
| `help` / `quit` | 帮助 / 退出 |

## 离线体验

```
price 105000           # 手动喂价（全部交易对）
auto on                # 开启策略 → 观察趋势建仓与盈亏
```

> 注意：离线模式没有历史 K 线，策略需要先累积 `TREND_MIN_CANDLES` 根 1m K 线（约 21 分钟）
> 才会开仓；在线启动会自动预载历史 1m K 线，信号立即可用。

## 配置（config.py）

**行情接入**：`WS_PUBLIC_URL`（Public 类：bookTicker）、`WS_MARKET_URL`（Market 类：markPrice@1s）、
`WS_PROXY`、`WS_STALE_SECONDS`(30，行情看门狗：超过该秒数无更新强制重连)、`WS_RECONNECT_SECONDS`(3)

**组合**：`TOTAL_BALANCE`、`NUM_PAIRS`、`PER_PAIR_BALANCE`、
`VOLUME_POOL`、`POOL_MIN_DAYS`、`REQUIRE_SYMBOLS`、`FALLBACK_SYMBOLS`、
`VOLATILITY_LOOKBACK_DAYS`、`VOLATILITY_WINDOW_HOURS`、`TRADE_ONLY_IN_WINDOW`

**趋势策略**：
- 趋势识别：`TREND_FAST_MA`(8)、`TREND_SLOW_MA`(21)、`TREND_MIN_CANDLES`(21)、
  `TREND_DEADBAND_PCT`、`TREND_MAX_CANDLES`、`TREND_KLINES_SEED`(240)
- 建仓：`TREND_POSITION_MARGIN_USD`(10，首仓固定保证金)
- 浮盈加仓：`TREND_PYRAMID_RATIO`(1.0)、`TREND_MIN_ADD_USD`(20)、`TREND_MAX_ADDS`(3)、
  `TREND_PULLBACK_PCT`(0.5%)
- 离场：`TREND_TAKE_PROFIT`(5.0)、`TREND_STOP_LOSS`(0.3)、`TREND_TRAIL_ACTIVATE_PCT`(2%)、
  `TREND_TRAIL_STOP_PCT`(1%)、`TREND_TIME_LIMIT`、`TREND_COOLDOWN_SECONDS`(60s)
- 亏损冷却/连败降频（两种策略共用，以实际盈亏<0 为亏损离场）：`TREND_LOSS_COOLDOWN_SECONDS`(20分钟)、
  `TREND_LOSS_STREAK_BAN`(2)、`TREND_LOSS_STREAK_MULT`(2.667)、`TREND_LOSS_STREAK_MAX_COOLDOWN`(720分钟)——
  亏损离场冷却阶梯：连败 1 次 120 分钟（盈利离场仅 60 秒）、2 次 320 分钟、3 次起 720 分钟封顶；盈利离场清零连败

**组合风控（Governor，只限自动策略新开仓/加仓）**：
- `RISK_MAX_POSITIONS`(6)：同时持仓交易对数上限
- `RISK_MAX_MARGIN_PCT`(0.60)：已用保证金 ≤ 组合权益 60%
- `RISK_MAX_NET_NOTIONAL_USD`(3000)：净方向敞口（多头名义−空头名义）绝对值上限——加密币相关性极高，净敞口才是「押同一个方向多少钱」
- `RISK_DAILY_MAX_LOSS_PCT`(0.05)：当日（UTC）权益回撤 ≥5% 熔断，暂停新开仓至 UTC 次日（现有仓位照常管理）
- `TRADE_JOURNAL`(`trade_log.csv`)：每笔平仓追加记录（时间/交易对/盈亏/离场原因），utf-8-sig 供 Excel
- `PROFIT_TARGET_DEFAULT`(0)：启动时回车默认的止盈目标金额（USDT）；0=不启用
- `LOSS_LIMIT_DEFAULT`(0)：启动时回车默认的止损金额（USDT）；0=不启用

**策略选择**：`STRATEGY`（`livermore_roll` 默认 / `trend_pyramid`）

**利弗莫尔战法**（`STRATEGY="livermore_roll"`）：
- 关键点：`LIVERMORE_PIVOT_PERIOD`(45)、`LIVERMORE_BREAKOUT_PCT`(0.1%)、`LIVERMORE_REQUIRE_TREND`(True)
- 试探仓：`LIVERMORE_PROBE_MARGIN_USD`(15)
- 金字塔加仓：`LIVERMORE_ADD_BREAKOUT_PCT`(0.5%)（有盈利+创新高才加，`TREND_MAX_ADDS` 次封顶）
- 多时间周期（15m）：`LIVERMORE_USE_HIGHER_TF`(True)、`LIVERMORE_HIGHER_TF_FAST`(5)/`_SLOW`(12)、`LIVERMORE_HTF_DEADBAND_PCT`(0.2%)、`LIVERMORE_HTF_FLAT_ALLOW`(False)
- 离场：`LIVERMORE_USE_PIVOT_STOP`(True)、`LIVERMORE_HTF_EXIT_BREAK`(True)、`LIVERMORE_TRAIL_ACTIVATE_PCT`(1%)、`LIVERMORE_TRAIL_GIVEBACK`(35%)、`LIVERMORE_BREAKEVEN_ACTIVATE_PCT`(1.5%)、`LIVERMORE_STOP_ATR_MULT`(1.5)
- 进场质量：`LIVERMORE_BREAKOUT_ATR_MULT`(0.25，突破缓冲=max(0.1%, 0.25×15m ATR/价))、`LIVERMORE_FUNDING_FILTER`(0.03%，资金费拥挤方向不进场)
- 风险预算：`LIVERMORE_RISK_MAX_LOSS_USD`(25，单笔最大亏损封顶→反推试探仓名义)
- 滞涨离场：`LIVERMORE_STAGNATION_MINUTES`(45)、`LIVERMORE_STAGNATION_MIN_PNL`(0.10)
- 含费保本：`LIVERMORE_BREAKEVEN_BUFFER_PCT`(0.12%，保本位覆盖往返手续费)

**账户/官方**：`LEVERAGE`、`TAKER_FEE`/`MAKER_FEE`、`SLIPPAGE_PCT`、`PRECISION`、`MMR_TIERS`

**余额记录**：`BALANCE_INI`（`baozhengjin.ini`）——关闭时写入当前总保证金、启动时读取为本次总资金；首次运行无记录时用 `TOTAL_BALANCE`（1000）

## 选对结果说明

交易对按「交易量前 30 → 排除新币 → 30 天波动率排序」自动筛选。注意：币安当前交易量前十本身就包含不少低价高波动币（如 ACE/CYS/APR 等，今日成交额数亿 USDT），选出的可能是微盘币而非传统大盘币——这是真实市场数据的反映。若想固定某些主流币，把它们加入 `REQUIRE_SYMBOLS` 即可。

## 启动器

双击 **`start_tool.bat`**：启动工具 / 离线自测 / 安装依赖 / 清理缓存。

## 自测

```bash
python test_logic.py   # 25 组离线测试：官方账务精度(含加仓后强平价) + 趋势加仓闭环 + 利弗莫尔战法闭环 + 组合风控/进场质量/滞涨/含费保本/风险预算/平仓盈亏入账/止盈目标/止损上限
```

## 文件说明

### 核心程序

| 文件 | 作用 |
|---|---|
| `main.py` | 主入口和命令行交互层。负责启动流程、读取启动参数、初始化账户和交易对、运行异步行情/策略循环、处理 `buy`、`short`、`close`、`auto`、`status`、`stats` 等命令，并在退出时保存余额。 |
| `config.py` | 全局配置中心。集中管理 WebSocket 地址、代理、交易对、资金、杠杆、手续费、精度、维持保证金档位、趋势策略、利弗莫尔策略、组合风控、交易日志和余额文件路径。 |
| `binance_ws.py` | 币安 U 本位合约 WebSocket 客户端。分别连接 Public 和 Market 两类地址，订阅 `bookTicker`、`markPrice@1s`，通过代理连接并在断线或行情静默时自动重连。 |
| `selector.py` | 在线选对和行情初始化。获取 24 小时成交量、交易所精度、历史 K 线，筛选交易对，计算波动率和高波动时段。 |
| `portfolio.py` | 组合账户管理。维护多个交易对的独立资金池、持仓、组合盈亏、净方向敞口、保证金占用和自动策略风控闸门。 |
| `paper_account.py` | 单个交易对的虚拟账户和模拟撮合。实现开多、开空、平仓、手续费、资金费、未实现/已实现盈亏、保证金、维持保证金、强平价、数量和价格精度处理。 |
| `strategy.py` | 自动交易策略。包含默认的 `livermore_roll` 利弗莫尔关键点突破滚仓策略，以及可切换的 `trend_pyramid` 趋势跟随浮盈加仓策略。 |
| `api_client.py` | 可选的币安只读账户信息客户端。用于展示余额、现货持仓和合约持仓；不负责下单，也不实现真实交易。启用时只应使用币安后台的只读 API Key。 |
| `check_proxy.py` | 网络诊断工具。分别测试 Public/Market WebSocket 是否能收到数据，帮助定位代理、网络、防火墙和币安地址问题。 |
| `_test_watchdog.py` | 行情看门狗相关的辅助测试脚本，用于验证行情静默检测和重连逻辑。 |
| `test_logic.py` | 离线回归测试。覆盖账户精度、盈亏、加仓、强平价、利弗莫尔策略、组合风控、目标止盈和止损上限等逻辑。 |

### 配置和数据文件

| 文件 | 作用 | 是否建议公开 |
|---|---|---|
| `requirements.txt` | Python 运行依赖，目前主要是 `aiohttp`。 | 可以公开 |
| `jiaoyi.ini` | 保存交易对列表。程序在线选对后会更新此文件，也可手工填写交易对。 | 建议公开模板，不公开个人运行结果 |
| `baozhengjin.ini` | 关闭程序时保存的模拟余额和时间，下次启动时作为初始资金读取。 | 不建议公开，应加入 `.gitignore` |
| `trade_log.csv` | 每笔模拟平仓的时间、交易对、盈亏和离场原因，使用 UTF-8 with BOM 方便 Excel 打开。 | 不建议公开，可能包含个人运行轨迹 |
| `test_out.txt` | 测试输出记录。 | 不建议公开，除非确认没有本地路径和个人信息 |
| `huihua.md` | 本地开发会话记录和项目工作备忘。 | 不建议公开，可能包含与源码无关的个人/环境信息 |
| `*.bak*` | 配置或源码备份文件。 | 不建议公开，容易包含旧密钥、旧代理或旧逻辑 |

### Windows 辅助文件

| 文件 | 作用 |
|---|---|
| `start_tool.bat` | Windows 菜单式启动器，可运行主程序、离线测试、安装依赖和清理 `__pycache__`。 |
| `check_color.bat` | Windows 控制台颜色/编码检查辅助脚本。 |
| `__pycache__/` | Python 字节码缓存目录，运行时自动生成，不应提交。 |

## 公开到 GitHub 前检查

1. 确认 `config.py` 中 `BINANCE_API_KEY` 和 `BINANCE_API_SECRET` 为空；不要提交真实密钥。
2. 删除或移出 `baozhengjin.ini`、`trade_log.csv`、`test_out.txt`、`huihua.md`、`*.bak*` 和 `__pycache__/`。
3. 检查 Git 暂存区：`git status --short`。
4. 搜索敏感内容：`git grep -n -i -E "api.?key|api.?secret|password|token|private.?key"`。
5. 不要提交包含真实代理用户名、代理密码、服务器地址或个人交易数据的配置。
6. 公开的是模拟交易软件，不是实盘交易机器人；程序不会提交真实订单，但行情和可选只读账户查询会访问币安接口。

## 安全与风险说明

- 默认只使用币安公共行情进行本地模拟，不需要 API Key。
- `BINANCE_API_KEY` 和 `BINANCE_API_SECRET` 仅用于可选的只读账户信息展示，代码不包含下单逻辑；仍建议使用最小权限、禁用提现权限的 API Key。
- `WS_PROXY` 是本地代理配置，公开仓库前应删除带认证信息的代理 URL。
- 交易策略、历史回测和模拟盈亏不代表未来收益。加密货币和杠杆合约具有高风险，本项目不构成投资、交易或财务建议。

## 文件结构

```
binance_paper_trading/
├── config.py           配置（组合/精度/档位/趋势策略参数）
├── binance_ws.py       行情客户端（aiohttp + 代理，Public+Market 双连接 + 行情看门狗）
├── check_proxy.py      连通性诊断（测新分类地址 Public/Market 是否收到数据）
├── selector.py         选对器（交易量+波动率+高波动时段+官方精度）
├── portfolio.py        组合账户（10 对 × 独立资金池/策略 + 组合风控闸门）
├── paper_account.py    虚拟账户（币安官方盈亏/精度/分档保证金）
├── strategy.py         自动策略：利弗莫尔战法（默认）+ 趋势跟随浮盈加仓（STRATEGY 切换）
├── main.py             主程序（CLI + 策略循环 + 汇总报表）
├── test_logic.py       离线自测
├── start_tool.bat      启动器（双击可用）
├── baozhengjin.ini     余额收益记录（关闭时自动生成/更新，启动时读取）
├── trade_log.csv       交易日志（每笔平仓自动追加：时间/交易对/盈亏/原因，utf-8-sig）
└── requirements.txt    aiohttp
```

## 已知限制

- 撮合只按最优一档/限价 + 滑点，未做完整盘口深度
- 持仓增长时保证金档位不重算（小仓位通常始终在第一档）
- 未模拟 VIP 阶梯费率与分摊资金费
- 高波动时段基于历史统计，未来收益不代表未来表现