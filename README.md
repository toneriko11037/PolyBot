# Polymarket Constellation 多资产策略

同时跟踪 Polymarket 上 **BTC / ETH / SOL / XRP / DOGE / BNB** 六个 5 分钟
Up/Down 盘：多数资产同向而动、却有**一个落后者**没跟上时，押这个落后者补涨/补跌，
持有到结算。核心是「共识 + 隐含概率差选落后者 + 盘口接近五五开」的三重过滤。

- 价格源：Polymarket 免费 RTDS WebSocket（Chainlink），**无需 API key、不花钱**。
- 盘口价：CLOB 公开 `/midpoint`（价格带 / 隐含概率差判断）。
- 下单：官方 `py-clob-client-v2`（CLOB V2）市价 `FOK` 买单。
- 默认 **DRY_RUN**：只打印信号和将要下的单，不花真钱。

> 免责：本策略为对 KumiBot 官网**公开信息**的仿制，未公开的细节（去重、盘口口径、多空取舍等）为
> 合理推断。所有阈值都可通过 `.env` 调参。**不是投资建议。**
> 旧版单资产 BTC 策略（`run.bat`/`bot.py`/`strategy.py`）已移除，仓库现只保留本引擎。

---

## 目录

1. [快速开始 / 运行方式](#1-快速开始--运行方式)
2. [每轮决策流程](#2-每轮决策流程)
3. [参数详解（重点：consensus / laggard_gap）](#3-参数详解)
4. [配置说明（.env 全量）](#4-配置说明)
5. [当前生效配置（$250 账户）](#5-当前生效配置)
6. [钱包与授权（重要）](#6-钱包与授权重要)
7. [日志与结算](#7-日志与结算)
8. [风控与资金](#8-风控与资金)
9. [回测](#9-回测)
10. [与官网的差异 / 局限 / 风险](#10-与官网的差异--局限--风险)
11. [文件说明](#11-文件说明)

---

## 1. 快速开始 / 运行方式

1. 双击 `run_constellation.bat`（首次会自动建虚拟环境、装依赖，并把 `.env.example`
   复制成 `.env` 后停下）。
2. 打开生成的 `.env`，填写 **`PRIVATE_KEY`**（实盘必须；DRY_RUN 可先不填），必要时填
   `FUNDER_ADDRESS`。其余项已有默认值。
3. 先在 **DRY_RUN** 下观察 1–2 轮，确认「共识 / 落后者 / 下单」日志正常。
4. 确认无误后，把 `.env` 里的 `DRY_RUN` 改成 `false`，重新 `run_constellation.bat` 即实盘。

```bat
:: 或手动运行
".venv\Scripts\python.exe" constellation.py
```

- `run_constellation.bat` 会自动建虚拟环境、装依赖，然后启动 `constellation.py`。
- `constellation.py` 读 `.env` 里的 `DRY_RUN`：`true` → 只打印信号和将要下的单，
  **不花真钱**；`false` → 实盘真下单。
- **首次启动的那一轮会跳过**：引擎需要本窗口开盘价，而开盘前价格流还没覆盖到，
  所以等到下一个 5 分钟窗口才可能开仓。

---

## 2. 每轮决策流程

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
   （代码默认 $0.38–0.68；当前 `.env` 设为 **$0.42–0.62**），买到接近五五开、还有上行空间的一侧。
4. **BTC 反转过滤**
   BTC 近 1 小时涨/跌 ≥ `BTC_REVERSION_PCT`（2%）时，**放松逆向一侧**的共识要求
   （`REVERSION_RELAX`，默认 1），博弈超涨/超跌回归。
5. **入场**
   按 `DCA_START_USD` 买入（当前 = 一次性 $3.2，不再补仓，见第 5 节）。
   入场时点受 `SCAN_AFTER_SECONDS`（开盘后起始）与 `DCA_STOP_BEFORE_CLOSE`（收盘前截止）共同约束。
6. **结算**
   持有到窗口结束，从 Gamma 已关闭市场读取结果，用真实盘口支出估算已实现 P&L。

---

## 3. 参数详解

### `MIN_CONSENSUS`（默认 4）—— 管「要不要开仓」

决定 6 个币里至少几个同向才动手。

- **调高**（5/6）：要求更多一致，信号更少、更精。
- **默认 4**：官网 BALANCED 预设，中等。
- **调低**（3）：信号更多，胜率略降。

### `AVG_MOVE_PCT`（默认 0.05）—— 官网 UP/DOWN threshold 的近似

官网 Constellation 的涨跌门槛对比的是**跟风资产的平均涨幅**，而不是逐个资产。
本实现保留逐资产 `MOVE_PCT` 作为「跟风」定义，再额外要求跟风队伍的平均涨幅
≥ `AVG_MOVE_PCT`。设为 0 即关闭该额外过滤（等价旧行为）。

全量回测（2026-04-15～10-01，DCA 关、每笔 $5、step 10，已修正现货未来函数）显示
0.05 略优于 0：胜率 56.6%→57.3%、ROI 4.28%→5.06%、回撤 $238→$213，成交少约 10%，
PnL 几乎持平（+$3160 → +$3371）。0.10 仍会砍掉约七成成交、绝对收益减半，不推荐。

### `LAGGARD_GAP`（默认 0.15）—— 管「押哪个币」（官网口径）

用**隐含概率差**衡量落后者「落后多少」才值得博：落后者在共识方向上的盘口价，
要比「跟风队伍」对应侧盘口价的**均值**低至少 `LAGGARD_GAP`。

- 官网 BALANCED 预设 = 0.15，例如队伍 Up 均价 0.85、落后者 Up 只要 ≤ 0.70 才算数。
- 调大：只挑明显掉队的（更严格、信号更少）。
- 调小：稍微落后就算（更宽松）。
- 完全同步（没有资产掉队）时不开仓，这是策略的正常状态。

一句话：**consensus 决定开不开仓，laggard_gap 决定谁算落后者。**

### 其余参数

| 参数 | 默认 | 含义 |
|---|---|---|
| `MOVE_PCT` | 0.03 | 判定「跟涨/跟跌」的最小涨跌幅（%） |
| `AVG_MOVE_PCT` | 0/0.05 | 跟风队伍平均涨幅门槛（官网 UP/DOWN threshold），0=关闭 |
| `LAGGARD_GAP` | 0.15 | 落后者需比队伍均价低的隐含概率差 |
| `MIN_CONSENSUS` | 4 | 开仓所需同向资产数 |
| `BTC_REVERSION_PCT` | 2.0 | BTC 1h 涨跌达此值触发反转放松（%） |
| `REVERSION_RELAX` | 1 | 放松的共识数量 |
| `ENTRY_MIN` / `ENTRY_MAX` | 0.38 / 0.68 | 入场盘口价带（当前 `.env` 用 0.42 / 0.62） |
| `SCAN_AFTER_SECONDS` | 90 | 本窗口开始后多久启动扫描 |
| `DCA_STOP_BEFORE_CLOSE` | 110 | 收盘前多少秒停止开仓/加仓（官网为 90） |

---

## 4. 配置说明

全部参数通过 `.env` 覆盖，模板见 `.env.example`。常用项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `PRIVATE_KEY` | *(空)* | 钱包私钥（`0x...`）。实盘必填，绝不外泄。 |
| `FUNDER_ADDRESS` | 空 | 资金地址。留空 = 私钥地址（EOA 直连）。代理钱包必填。 |
| `SIGNATURE_TYPE` | `auto` | `auto`/`0`/`1`/`2`/`3`，见第 6 节。 |
| `DRY_RUN` | `true` | `true` 模拟，`false` 实盘。 |
| `PRICE_SOURCE` | `chainlink_twap` | `chainlink_twap` / `chainlink` / `binance`。 |
| `CONSTELLATION_*` | 见模板 | 标的分组、共识数、落后者 gap、价带、DCA、风控等。 |

---

## 5. 当前生效配置（$250 账户）

在 `.env` 中已设置：

```ini
# 信号：照官网 BALANCED 预设
CONSTELLATION_MIN_CONSENSUS=4
CONSTELLATION_MOVE_PCT=0.03
CONSTELLATION_MIN_AVG_MOVE_PCT=0.05
CONSTELLATION_LAGGARD_GAP=0.15
CONSTELLATION_ENTRY_MIN=0.42
CONSTELLATION_ENTRY_MAX=0.62
CONSTELLATION_BTC_REVERSION_PCT=2.0

# 仓位：每笔一次性 $3.2，不再补仓
CONSTELLATION_DCA_START_USD=3.2
CONSTELLATION_DCA_ADD2_USD=0
CONSTELLATION_DCA_ADD3_USD=0
CONSTELLATION_DCA_MAX_USD=3.2

# 风控：最多 3 持仓，最大敞口 $15（占 $250 的 6%）
CONSTELLATION_MAX_POSITIONS=3
CONSTELLATION_MAX_EXPOSURE_USD=15
```

> 以上与仓库中 `.env` 一致；`.env.example` 是"照抄官网 BALANCED（0.38–0.68、$5）"的模板，
> 两者不同时以 `.env` 为准。

**为什么关闭 DCA？** 官网默认是三步入场（首笔 $5 → 加仓 $3 → 加仓 $2.50，单笔上限
$10.50）。改成一次性买入后：
- 单笔固定，最大敞口可控，资金占用清晰；
- 全量回测（2026-04-15～10-01，已修正未来函数）显示**摊低成本的 DCA 明显拖累收益**：
  一次性每笔 $5 → ROI **+4.28%**、回撤 $238；完整三步 DCA（$5/$3/$2.5，上限 $10.50）
  → ROI **−1.67%**、回撤 $2288。平均成本虽被摊低，但资金被压在持续下跌的输家上。

---

## 6. 钱包与授权（重要）

### 1) 找到你的资金地址（FUNDER_ADDRESS）

打开 `polymarket.com` → 右上角头像 → **Deposit / 存款**，页面显示的地址就是你的
**funder**。填入 `.env` 的 `FUNDER_ADDRESS`。

### 2) 确定 SIGNATURE_TYPE

| 值 | 适用场景 |
|---|---|
| `0` | EOA：私钥钱包直连，资金就在你自己的地址上（MetaMask 直接入金） |
| `1` | 邮箱 / Google(Magic) 登录生成的**代理钱包** |
| `2` | 浏览器钱包(MetaMask) 的**旧版代理钱包**（Gnosis Safe） |
| `3` | **2026-05-04 之后**新建账号 / 存款钱包（EIP-1271） |

- 不确定就填 `auto`：funder 为空或等于私钥地址 → `0`；是 Gnosis Safe → `2`；否则 → `1`。
- 若下单报 `maker address not allowed, please use the deposit wallet flow`，改填 `3`。

### 3) 代币授权（仅 EOA / MetaMask 直连需要）

邮箱/Magic 与代理钱包**无需手动授权**。EOA 直连需一次性授权 USDC 与 Conditional
Tokens（授权给 Polymarket 的交易所合约），否则无法成交。可参考官方说明：
<https://github.com/Polymarket/py-clob-client-v2> 的 “Token Allowances” 章节。

---

## 7. 日志与结算

- 运行日志：`logs/constellation.log`
- 下单/加仓/结算：`logs/constellation_orders.log`（同时打印到控制台）

> `DRY_RUN=true` 时，所有日志与 CSV 都写到 `logs/dry/`，与实盘数据完全隔离
> （可同时跑一个 live 和一个 dry 实例而互不干扰）。

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

### 7.1 trades.csv（每笔交易一行）

除基础盈亏列外，还记录**决策时特征**与**结算终值**，便于逐笔复盘：

| 列 | 含义 |
| --- | --- |
| `laggard_gap` | 队伍均价 − 落后者盘口价（真正的选股信号强度） |
| `leader_avg_price` | 跟风队伍在共识方向的平均盘口价 |
| `opposite_price` | 落后者对手侧盘口价（判断价差/是否买贵） |
| `entry_elapsed` | 首笔距窗口开盘的秒数（早/晚入场） |
| `btc_hour_pct` | 入场时 BTC 过去 1 小时涨跌（regime） |
| `up_required` / `down_required` | 当时生效的共识门槛 |
| `reversion_applied` | 是否触发 BTC 反转放松（1=是） |
| `final_value` / `final_delta_pct` | 收盘附近行情值及其相对 target 的百分比 |

### 7.2 trades_fills.csv（每笔成交一行）

`fill_time, round_start, asset, side, level, price, amount, shares, cum_spent`，
每笔成交立即落盘，用于拆解 DCA 是否在「追跌放大亏损」（`level=0` 为首笔）。

### 7.3 candidates.csv（每个窗口一行，无论是否开仓）

解决「成交太少、样本不足」：每个窗口都记一条候选快照（含被拒掉的），
窗口结束后补上结算结果。用于离线研究过滤阈值（比如 `laggard_gap` / 价带调参）。

| 列 | 含义 |
| --- | --- |
| `decision` | 该窗口的处置：`no_consensus` / `no_laggard` / `laggard_gap`（gap 不够）/ `price_band` / `risk` / `min_order` / `entered` |
| `consensus` / `up_count` / `down_count` / `up_required` / `down_required` | 共识方向与当时门槛 |
| `candidate_asset` / `candidate_side` | 该窗口被选中的落后者（不看 gap 门槛的最低盘口价者） |
| `candidate_gap` / `leader_avg_price` / `candidate_price` / `opposite_price` | 隐含概率差与两侧盘口 |
| `in_band` / `entered` | 是否落在入场价带 / 是否真的开仓 |
| `target` / `up_won` / `final_value` / `final_delta_pct` / `settled_by` | 候选资产的收盘结果（`up_won` 为该侧是否获胜） |

> 候选在同一窗口内保留「gap 最大」的快照；开仓则覆盖为 `entered`。
> 表头与代码不一致时（升级字段），旧文件会自动改名为 `*.legacy-<ts>.csv` 保留，另起新文件。

---

## 8. 风控与资金

- 同时最多 `MAX_POSITIONS`（3）个持仓。
- 总敞口 ≤ `MAX_EXPOSURE_USD`（$15）。
- 单笔 ≤ `DCA_MAX_USD`（当前 $3.2）。
- 可选交易时段 `CONSTELLATION_TRADING_HOURS_UTC`（UTC 小时，留空 = 全天）。
- 每个窗口每个资产只会建一次仓（按 `窗口+资产` 去重）。

> $250 账户、3 持仓 × $3.2：最大同时占用 $9.6（约 4%），即使全输也不会伤及本金结构。

---

## 9. 回测

历史数据 + Binance 现货 1m，离线复现实盘判定：T+90s 共识扫描 → 隐含概率差选落后者 →
价带过滤（默认 $0.38–0.68，可 `--params` 覆盖）→ 入场（可含 DCA）→ 持有到结算。
逻辑与实盘同源（`constellation_strategy.py`）。

> **数据精度警告（2026-10-03 修订）**：盘口价历史约 **1 分钟采样（每窗口 5–6 个点）**，
> 引擎做线性插值。因此回测只能用于**粗粒度、大样本**的方向判断，**不能**用来调秒级入场时点
> 或过细的阈值。另，早期版本的 `Series.close_before` 存在最多 **60s 的现货未来函数**（已于
> `backtest/dataset.py` 修正），修正后全区间 ROI 大约下降一半——下方所有数字均为**修正后**结果。

```bat
:: 1) 枚举 6 个资产的历史窗口
python -m backtest.run fetch-markets

:: 2) 下载各资产 Binance 1m 现货（data.binance.vision 日归档，历史更长）
python -m backtest.run fetch-spot --method daily

:: 3) 下载各资产 5m 盘口价
python -m backtest.run fetch-prices --workers 12

:: 4) 跑 Constellation 回测（含 1.56% taker 费 + $0.01 滑点）
python -m backtest.run constellation --assets btc,eth,sol,xrp,doge,bnb ^
    --start 2026-04-15 --end 2026-10-01 --fee-rate 0.0156 --slippage 0.01 --step 10
```

- 各资产 series id：BTC 10684 / ETH 10683 / XRP 10685 / SOL 10686 / DOGE 11325 / BNB 11326。
- DOGE、BNB 的 5m 盘 **2026-04 起**才有，其余约 2025-12 起。
- 数据落盘到 `backtest_data/`（已加入 `.gitignore`），下载均可断点续传。

### 9.1 回测结果

数据 2026-04-15 ~ 2026-10-01，约 48,952 轮，含 **1.56% taker 费 + $0.01 滑点**，
**已修正现货未来函数**。「隐含概率差」选落后者 + BALANCED 预设（consensus=4, laggard gap 0.15），
DCA 关、每笔 $5、`--step 10`：

| 配置 | 成交 | 胜率 | PnL | ROI | 回撤 |
|---|---|---|---|---|---|
| `AVG_MOVE_PCT=0`（仅逐资产门槛） | 14763 | 56.6% | +$3160 | 4.28% | $238 |
| **`AVG_MOVE_PCT=0.05`（官网平均门槛，采用）** | 13334 | **57.3%** | **+$3371** | **5.06%** | **$213** |
| `AVG_MOVE_PCT=0.10` | 3497 | 59.3% | +$1419 | 8.12% | $108 |

入场价带（`AVG_MOVE_PCT=0.05`、其余同上）：

| `ENTRY_MIN`–`ENTRY_MAX` | 成交 | 胜率 | PnL | ROI | 回撤 |
|---|---|---|---|---|---|
| 0.38–0.68（代码默认） | 13334 | 57.3% | +$3371 | 5.06% | $213 |
| 0.42–0.68 | 12320 | 59.1% | +$3564 | 5.79% | $190 |
| 0.38–0.62 | 11836 | 54.8% | +$2453 | 4.14% | $230 |
| **0.42–0.62（当前 `.env`）** | 10819 | 56.6% | +$2633 | 4.87% | $208 |

结论：
- 平均涨幅门槛 0 与 0.05 接近（PnL 差约 7%，在噪声内）；0.05 的胜率/ROI/回撤略优，故采用 0.05。
  0.10 过滤过狠，胜率/ROI 高但绝对收益减半，不采用。
- 价带各档差异不大（ROI 4.1%–5.8%），当前 0.42–0.62 与最优档差距在噪声内，无需据小样本改动。
- 修正未来函数后，策略仍**全区间为正**，但不再是此前宣称的两位数 ROI。

> 历史对照（旧版「现货最平」选落后者，0.49–0.55，step 5）：consensus=5 带 DCA 成交 3205 /
> 胜率 60.9% / +$1826；consensus=4 约 8686 / 58.8%。**该对照产生于未来函数修正之前，数值偏乐观，
> 仅供追溯，勿与上表并列比较。**

复跑回测：

```bat
python -m backtest.run constellation --assets btc,eth,sol,xrp,doge,bnb ^
    --start 2026-04-15 --end 2026-10-01 --fee-rate 0.0156 --slippage 0.01 --step 10 ^
    --params "avg_move_pct=0|0.05|0.10,dca_add2=0,dca_add3=0,dca_max=5"
```

### 9.2 DCA 对照

同一区间、band 默认 0.38–0.68，比较「一次性」与「三步入场」：

| 入场方式 | 成交 | 胜率 | PnL | ROI | 回撤 |
|---|---|---|---|---|---|
| 一次性 $5（`add2=add3=0, max=5`） | 14763 | 56.6% | +$3160 | 4.28% | $238 |
| 部分加仓（`add2=3, add3=0, max=10.5`） | 14763 | 56.6% | +$70 | 0.08% | $1003 |
| 完整三步（`add2=3, add3=2.5, max=10.5`） | 14763 | 56.6% | −$1560 | −1.67% | $2288 |

结论：**越"摊低成本"越差**——加仓把资金压在持续下跌的输家上，伤害远大于摊低成本的好处。
故默认关闭 DCA（每笔一次性）。

### 9.3 数据精度 / 已知偏差

- **盘口价**：历史约每 60s 一个采样点（每窗口 5–6 点），线性插值；无法还原实盘 3s 级盘口。
- **现货**：Binance 1m 收盘价，已修正「用到未收盘 K 线」的未来函数（最多 60s）。
- **结算源差异**：现货用 Binance 近似官方 Chainlink，存在基差。
- **未建模**：FOK 失败 / 无流动性 / 下单延迟 / 部分成交 / 手续费曲线细节。
- **推论**：回测数字应视为**上界**，实盘预期更低。**只用它做粗粒度、大样本的结论**
  （例如"要不要开 DCA"），不要用它挑秒级入场时点或极小阈值差异。

---

## 10. 与官网的差异 / 局限 / 风险

**照官网原值**：0.03% 涨跌阈、laggard gap 0.15、$0.38–0.68 价带、T+90s 扫描、
2% BTC 反转、共识 4/6、DCA 阶梯 $5/$3/$2.50/$10.50、收盘前 110s 停加、3 持仓/$31.50。

**推断部分**（官网未公开）：「队伍价位」用同向资产盘口价的**均值**、落后者并列时取
盘口价最低者、多空同时达标时的取舍、盘口取哪种价（我们用 CLOB 中间价）、去重口径。

**官网 UP/DOWN threshold 的近似**：官网说该门槛对比的是「跟风资产的平均涨幅」。我们用
逐资产 `MOVE_PCT` 定义「跟风」，再额外要求队伍平均涨幅 ≥ `AVG_MOVE_PCT`（默认 0.05）。
与官网确切算法（可能先按平均定方向、再以平均设门槛）仍有细微差别，但已回测验证影响可忽略。

**回测局限**（详见 9.3；实盘预期应**低于**回测）：
- 盘口价约 1 分钟采样、线性插值，无法还原实盘秒级盘口；
- 现货用 Binance 近似 Chainlink 结算源，存在基差；
- 已修正现货未来函数（此前令 ROI 虚高约一倍）；
- 未建模 FOK 失败 / 无流动性 / 下单延迟。

**风险提示**：
- Polymarket 5 分钟加密市场**开启了 taker 手续费**（按价格曲线，最高约 1.56%）。
- 策略不保证盈利；短周期盘口易受延迟、滑点、手续费侵蚀。
- 请先用 `DRY_RUN` 充分验证，并从**最小金额**开始。
- 妥善保管私钥；`.env` 已被 `.gitignore` 忽略，切勿提交或分享。

---

## 11. 文件说明

| 文件 | 作用 |
|---|---|
| `constellation.py` | 引擎入口（窗口管理 / 扫描 / 入场 / 结算） |
| `constellation_strategy.py` | 共识 / 落后者 / DCA 的纯逻辑（可单测） |
| `constellation_config.py` | 读取 `CONSTELLATION_*` 参数并校验 |
| `multi_feed.py` | 6 资产价格流（每标的一条 RTDS，含 1h 涨跌幅） |
| `clob_price.py` | CLOB `/midpoint` 盘口价（价格带判断用） |
| `gamma_market.py` | 按 slug 定位并解析 Up/Down 市场、结算结果 |
| `price_feed.py` | 单资产 RTDS WebSocket 价格流（含历史回填） |
| `trader.py` | CLOB 鉴权、钱包类型探测、市价买入 |
| `config.py` | 读取并校验 `.env`（钱包 / 价格源 / 日志等共用项） |
| `run_constellation.bat` | Windows 一键运行 |
| `backtest/` | Constellation 回测：数据抓取 / 引擎 / 报告 |
