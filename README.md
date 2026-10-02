# Polymarket Constellation 多资产策略

同时跟踪 Polymarket 上 **BTC / ETH / SOL / XRP / DOGE / BNB** 六个 5 分钟
Up/Down 盘：多数资产同向而动、却有**一个落后者**没跟上时，押这个落后者补涨/补跌，
持有到结算。核心是「共识 + 隐含概率差选落后者 + 盘口接近五五开」的三重过滤。

- 价格源：Polymarket 免费 RTDS WebSocket（Chainlink），**无需 API key、不花钱**。
- 盘口价：CLOB 公开 `/midpoint`（价格带 / 隐含概率差判断）。
- 下单：官方 `py-clob-client-v2`（CLOB V2）市价 `FOK` 买单。
- 默认 **DRY_RUN**：只打印信号和将要下的单，不花真钱。
- 详细规则、参数与回测结论见 **`CONSTELLATION.md`**。

---

## 快速开始

1. 双击 `run_constellation.bat`（首次会自动建虚拟环境、装依赖，并把 `.env.example`
   复制成 `.env` 后停下）。
2. 打开生成的 `.env`，填写 **`PRIVATE_KEY`**（实盘必须；DRY_RUN 可先不填），必要时填
   `FUNDER_ADDRESS`。其余项已有默认值。
3. 先在 **DRY_RUN** 下观察 1–2 轮，确认「共识 / 落后者 / $5 下单」日志正常。
4. 确认无误后，把 `.env` 里的 `DRY_RUN` 改成 `false`，重新 `run_constellation.bat` 即实盘。

```bat
:: 或手动运行
".venv\Scripts\python.exe" constellation.py
```

> 首次启动的那一轮会跳过：引擎需要本窗口开盘价，而开盘前价格流还没覆盖到，
> 所以等到下一个 5 分钟窗口才可能开仓。

---

## 配置说明

全部参数通过 `.env` 覆盖，模板见 `.env.example`。常用项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `PRIVATE_KEY` | *(空)* | 钱包私钥（`0x...`）。实盘必填，绝不外泄。 |
| `FUNDER_ADDRESS` | 空 | 资金地址。留空 = 私钥地址（EOA 直连）。代理钱包必填。 |
| `SIGNATURE_TYPE` | `auto` | `auto`/`0`/`1`/`2`/`3`，见下文。 |
| `DRY_RUN` | `true` | `true` 模拟，`false` 实盘。 |
| `PRICE_SOURCE` | `chainlink_twap` | `chainlink_twap` / `chainlink` / `binance`。 |
| `CONSTELLATION_*` | 见模板 | 标的分组、共识数、落后者 gap、价带、DCA、风控等。 |

---

## 钱包与授权（重要）

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

## 回测模式（`backtest/`）

历史数据 + Binance 现货 1m，离线复现实盘判定：T+90s 共识扫描 → 隐含概率差选落后者 →
$0.38–0.68 价带 → 三步入场 → 持有到结算。逻辑与实盘同源（`constellation_strategy.py`）。

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

已知局限（实盘预期应**低于**回测）：盘口价约 1 分钟采样、现货用 Binance 近似 Chainlink
结算源、未建模 FOK 失败 / 无流动性 / 下单延迟。

---

## 文件说明

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

---

## 风险提示

- Polymarket 5 分钟加密市场**开启了 taker 手续费**（按价格曲线，最高约 1.56%）。
- 策略不保证盈利；短周期盘口易受延迟、滑点、手续费侵蚀。
- 请先用 `DRY_RUN` 充分验证，并从**最小金额**开始。
- 妥善保管私钥；`.env` 已被 `.gitignore` 忽略，切勿提交或分享。
