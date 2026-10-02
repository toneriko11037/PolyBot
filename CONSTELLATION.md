# Constellation 多资产策略（说明文档）

本文件专门说明仿 KumiBot 官网 **Constellation** 策略的独立实现，方便日后查看。
旧版单资产 BTC 策略（`run.bat`/`bot.py`/`strategy.py`）已移除，仓库现只保留本引擎。

> 免责：本策略为对官网**公开信息**的仿制，未公开的细节（去重、盘口口径、多空取舍等）为
> 合理推断。所有阈值都可通过 `.env` 调参。不是投资建议。

---

## 目录

1. [一句话概述](#1-一句话概述)
2. [运行方式](#2-运行方式)
3. [每轮决策流程](#3-每轮决策流程)
4. [参数详解（重点：consensus / laggard_gap）](#4-参数详解)
5. [当前生效配置（$250 账户）](#5-当前生效配置)
6. [日志与结算](#6-日志与结算)
7. [风控与资金](#7-风控与资金)
8. [回测结果](#8-回测结果)
9. [与官网的差异 / 局限](#9-与官网的差异--局限)
10. [文件索引](#10-文件索引)

---

## 1. 一句话概述

同时跟踪 Polymarket 上 **BTC / ETH / SOL / XRP / DOGE / BNB** 六个 5 分钟
Up/Down 盘。当多数资产朝同一方向动、却有**一个落后者**没跟上时，押这个落后者补涨/补跌，
持有到结算。核心是「共识 + 隐含概率差选落后者 + 盘口接近五五开」的三重过滤。

---

## 2. 运行方式

```bat
:: 双击即可
run_constellation.bat

:: 或手动
".venv\Scripts\python.exe" constellation.py
```

- `run_constellation.bat` 会自动建虚拟环境、装依赖，然后启动 `constellation.py`。
- 旧版单资产 `run.bat`/`bot.py`/`strategy.py` 已删除，仓库只保留本引擎。
- **首次启动的那一轮会跳过**：引擎需要知道本窗口开盘价，而开盘前价格流还没覆盖到，
  所以等到下一个 5 分钟窗口才可能开仓。

### 先空跑验证

`constellation.py` 读 `.env` 里的 `DRY_RUN`：

- `DRY_RUN=true` → 只打印信号和将要下的单，**不花真钱**。
- `DRY_RUN=false` → 实盘真下单。

建议先 `true` 跑 1–2 轮，确认「共识 / 落后者 / $5 下单」日志正常后再改 `false`。

---

## 3. 每轮决策流程

每个 5 分钟窗口（一轮）按下面顺序走：

1. **共识扫描（T+90s 起）**
   各资产相对**本窗口开盘价**算涨跌幅：
   - 涨 > `MOVE_PCT`（0.03%）→ 记「跟涨」
   - 跌 < -`MOVE_PCT` → 记「跟跌」
   - 同向数量 ≥ `MIN_CONSENSUS`（默认 4/6）才认为大盘有方向。
2. **挑落后者（隐含概率差）**
   取「跟风队伍」（与共识同向的资产）对应方向盘口价的**均值**作为队伍价位；
   在没跟上的资产里，找盘口价比队伍价位低至少 `LAGGARD_GAP`（默认 0.15）的那一个，
   押它补涨/补跌。**无人明显掉队（完全同步）时不开仓。**
3. **价格带过滤**
   该落后者对应方向的 Polymarket 盘口价必须落在 `[ENTRY_MIN, ENTRY_MAX]`
   （默认 **$0.38–$0.68**），买到接近五五开、还有上行空间的一侧。
4. **BTC 反转过滤**
   BTC 近 1 小时涨/跌 ≥ `BTC_REVERSION_PCT`（2%）时，**放松逆向一侧**的共识要求
   （`REVERSION_RELAX`，默认 1），博弈超涨/超跌回归。
5. **入场**
   按 `DCA_START_USD` 买入（当前 = 一次性 $5，不再补仓，见第 5 节）。
6. **结算**
   持有到窗口结束，从 Gamma 已关闭市场读取结果，用真实盘口支出估算已实现 P&L。

---

## 4. 参数详解

### `MIN_CONSENSUS`（默认 4）—— 管「要不要开仓」

决定 6 个币里至少几个同向才动手。

- **调高**（5/6）：要求更多一致，信号更少、更精。
- **默认 4**：官网 BALANCED 预设，中等。
- **调低**（3）：信号更多，胜率略降。

### `AVG_MOVE_PCT`（默认 0.05）—— 官网 UP/DOWN threshold 的近似

官网 Constellation 的涨跌门槛对比的是**跟风资产的平均涨幅**，而不是逐个资产。
本实现保留逐资产 `MOVE_PCT` 作为「跟风」定义，再额外要求跟风队伍的平均涨幅
≥ `AVG_MOVE_PCT`。设为 0 即关闭该额外过滤（等价旧行为）。

全量回测（2026-04-15～10-01，DCA 关，step 10）显示这是**平局**：
0.05 相比 0 胜率 +0.6pp、ROI +1pp、回撤更低，PnL 几乎相同、成交仅少 8%。
0.10 会砍掉七成成交、绝对收益腰斩，不推荐。

### `LAGGARD_GAP`（默认 0.15）—— 管「押哪个币」（官网口径）

用**隐含概率差**衡量落后者「落后多少」才值得博：落后者在共识方向上的盘口价，
要比「跟风队伍」对应侧盘口价的**均值**低至少 `LAGGARD_GAP`。

- 官网 BALANCED 预设 = 0.15，例如队伍 Up 均价 0.85、落后者 Up 只要 ≤ 0.70 才算数。
- 调大：只挑明显掉队的（更严格、信号更少）。
- 调小：稍微落后就算（更宽松）。
- 完全同步（没有资产掉队）时不开仓，这是策略的正常状态。

一句话：**consensus 决定开不开仓，laggard_gap 决定谁算落后者。**

### 其余参数（见 `.env` 注释）

| 参数 | 默认 | 含义 |
|---|---|---|
| `MOVE_PCT` | 0.03 | 判定「跟涨/跟跌」的最小涨跌幅（%） |
| `AVG_MOVE_PCT` | 0/0.05 | 跟风队伍平均涨幅门槛（官网 UP/DOWN threshold），0=关闭 |
| `LAGGARD_GAP` | 0.15 | 落后者需比队伍均价低的隐含概率差 |
| `MIN_CONSENSUS` | 4 | 开仓所需同向资产数 |
| `BTC_REVERSION_PCT` | 2.0 | BTC 1h 涨跌达此值触发反转放松（%） |
| `REVERSION_RELAX` | 1 | 放松的共识数量 |
| `ENTRY_MIN` / `ENTRY_MAX` | 0.38 / 0.68 | 入场盘口价带 |
| `SCAN_AFTER_SECONDS` | 90 | 本窗口开始后多久启动扫描 |

---

## 5. 当前生效配置（$250 账户）

在 `.env` 中已设置：

```ini
# 信号：照官网 BALANCED 预设
CONSTELLATION_MIN_CONSENSUS=4
CONSTELLATION_MOVE_PCT=0.03
CONSTELLATION_MIN_AVG_MOVE_PCT=0.05
CONSTELLATION_LAGGARD_GAP=0.15
CONSTELLATION_ENTRY_MIN=0.38
CONSTELLATION_ENTRY_MAX=0.68
CONSTELLATION_BTC_REVERSION_PCT=2.0

# 仓位：每笔一次性 $5，不再补仓
CONSTELLATION_DCA_START_USD=5
CONSTELLATION_DCA_ADD2_USD=0
CONSTELLATION_DCA_ADD3_USD=0
CONSTELLATION_DCA_MAX_USD=5

# 风控：最多 3 持仓，最大敞口 $15（占 $250 的 6%）
CONSTELLATION_MAX_POSITIONS=3
CONSTELLATION_MAX_EXPOSURE_USD=15
```

**为什么关闭 DCA？** 官网默认是三步入场（首笔 $5 → 加仓 $3 → 加仓 $2.50，单笔上限
$10.50）。对 $250 账户来说，一笔会放大到 $10.50、三笔满仓 $31.50。改成一次性 $5 后：
- 单笔固定 $5，最大敞口 $15，资金占用清晰；
- 回测显示**一次性投入 ROI 更高**（+14.29% vs +9.55%），DCA 摊低成本但会把资金压在输家上。

---

## 6. 日志与结算

- 运行日志：`logs/constellation.log`
- 下单/加仓/结算：`logs/constellation_orders.log`（同时打印到控制台）

结算日志示例：

```
[LIVE] 结算(gamma): BNB Up | WIN | 支出 $5.00 收回 $9.80 | P&L $+4.80
[LIVE] 结算(local): ETH Down | LOSE | 支出 $5.00 收回 $0.00 | P&L $-5.00
[LIVE] 结算修正: ETH Down | 本地近似 LOSE → 官方 LOSE
```

- 胜负优先从 Gamma 已关闭市场读取（`gamma_market.fetch_outcome`）。
- Gamma 结算有延迟：收盘 15s 后若仍无结果，先用本地行情近似（`_local_outcome`，取
  `end_ts` 后第一个 tick；`PRICE_SOURCE=chainlink_twap` 时即官方结算源）。近似记录
  `settled_by=local`，并释放风控额度；官方结果出来后若不一致，再补一条
  `settled_by=gamma` 的修正行。统计时按 `窗口+资产` 取 `gamma` 行优先去重。
- P&L = 结算收回 − 实际盘口支出（含滑点影响），**不估算、用真实成交价折算的份额**。

---

## 7. 风控与资金

- 同时最多 `MAX_POSITIONS`（3）个持仓。
- 总敞口 ≤ `MAX_EXPOSURE_USD`（$15）。
- 单笔 ≤ `DCA_MAX_USD`（$5）。
- 可选交易时段 `CONSTELLATION_TRADING_HOURS_UTC`（UTC 小时，留空 = 全天）。
- 每个窗口每个资产只会建一次仓（按 `窗口+资产` 去重）。

> $250 账户、3 持仓 × $5：最大同时占用 $15（6%），即使全输也不会伤及本金结构。

---

## 8. 回测结果

数据 2026-04-15 ~ 2026-10-01，约 49k 轮，含 **1.56% taker 费 + $0.01 滑点**。
**新版**「隐含概率差」选落后者 + BALANCED 预设（consensus=4, 0.38–0.68, laggard gap 0.15），
DCA 关、每笔 $5、`--step 10`：

| 配置 | 成交 | 胜率 | PnL | ROI | 回撤 |
|---|---|---|---|---|---|
| `AVG_MOVE_PCT=0`（仅逐资产门槛） | 13304 | 57.7% | +$6913 | 10.39% | $180 |
| **`AVG_MOVE_PCT=0.05`（官网平均门槛，采用）** | 12238 | **58.3%** | **+$6944** | **11.35%** | **$170** |
| `AVG_MOVE_PCT=0.10` | 3459 | 59.8% | +$2387 | 13.80% | $108 |

结论：
- 平均涨幅门槛 0 与 0.05 几乎是**平局**（PnL 差 0.4%，在噪声内）；0.05 的胜率/ROI/回撤略优，
  成交仅少 8%，故采用 0.05。0.10 过滤过狠，绝对收益腰斩，不采用。
- 策略全区间**稳定盈利**，新版成交量级远大于旧版「现货最平」选法。

> 历史对照（旧版「现货最平」选落后者，0.49–0.55，step 5）：consensus=5 带 DCA 成交 3205 /
> 胜率 60.9% / +$1826；consensus=4 约 8686 / 58.8%。

复跑回测：

```bat
python -m backtest.run constellation --assets btc,eth,sol,xrp,doge,bnb ^
    --start 2026-04-15 --end 2026-10-01 --fee-rate 0.0156 --slippage 0.01 --step 10 ^
    --params "avg_move_pct=0|0.05|0.10,dca_add2=0,dca_add3=0,dca_max=5"
```

---

## 9. 与官网的差异 / 局限

**照官网原值**：0.03% 涨跌阈、laggard gap 0.15、$0.38–0.68 价带、T+90s 扫描、
2% BTC 反转、共识 4/6、DCA 阶梯 $5/$3/$2.50/$10.50、收盘前 110s 停加、3 持仓/$31.50。

**推断部分**（官网未公开）：「队伍价位」用同向资产盘口价的**均值**、落后者并列时取
盘口价最低者、多空同时达标时的取舍、盘口取哪种价（我们用 CLOB 中间价）、去重口径。

**官网 UP/DOWN threshold 的近似**：官网说该门槛对比的是「跟风资产的平均涨幅」。我们用
逐资产 `MOVE_PCT` 定义「跟风」，再额外要求队伍平均涨幅 ≥ `AVG_MOVE_PCT`（默认 0.05）。
与官网确切算法（可能先按平均定方向、再以平均设门槛）仍有细微差别，但已回测验证影响可忽略。

**回测局限**（实盘预期应**低于**回测）：
- 盘口价约 1 分钟采样，用插值估计；
- 现货用 Binance 近似 Chainlink 结算源，存在基差；
- 未建模 FOK 失败 / 无流动性 / 下单延迟。

---

## 10. 文件索引

| 文件 | 作用 |
|---|---|
| `constellation.py` | 引擎入口（窗口管理 / 扫描 / 入场 / 结算） |
| `constellation_strategy.py` | 共识 / 落后者 / DCA 的纯逻辑（可单测） |
| `constellation_config.py` | 读取 `CONSTELLATION_*` 参数并校验 |
| `multi_feed.py` | 6 资产价格流（每标的一条 RTDS，含 1h 涨跌幅） |
| `clob_price.py` | CLOB `/midpoint` 盘口价（价格带用） |
| `run_constellation.bat` | 一键启动本引擎 |
| `backtest/` | 多资产回测（数据抓取 / 引擎 / 报告） |
