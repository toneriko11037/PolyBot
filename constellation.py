"""Constellation 多资产引擎（KumiBot 同款思路的独立实现）。

每 5 分钟窗口同时跟踪 BTC/ETH/SOL/XRP/DOGE/BNB 六个盘：

1. T+90s 起扫描各资产相对本窗口开盘价的涨跌幅，达到共识方向后，用「隐含概率差」
   （laggard gap）挑出掉队最明显的「落后者」，仅当其对应侧盘口价落在入场价带
   （默认 $0.38–$0.68）时入场。
2. 三步入场：先 $5，价格每下滑 $0.09 依次加 $3、$2.50，单笔上限 $10.50，
   收盘前 110 秒停止加仓。
3. BTC 过去 1 小时涨跌 ≥2% 时放松逆向一侧的共识要求（超涨/超跌回归）。
4. 风险上限：同时最多 3 个持仓、总敞口 $31.50；持仓一律持有到结算，
   结算结果从 Gamma 已关闭市场读取，用真实盘口支出估算已实现 P&L。
"""

from __future__ import annotations

import asyncio
import csv
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import clob_price
import config
import constellation_config as ccfg
import gamma_market
import trader
from constellation_strategy import (
    AssetSnapshot,
    ConstellationStrategy,
    dca_add,
)
from gamma_market import Market
from multi_feed import MultiPriceFeed

log = logging.getLogger("constellation")
order_log = logging.getLogger("corder")

SCAN_INTERVAL = 3.0
MARKET_REFRESH_INTERVAL = 5.0
SNAPSHOT_LOG_INTERVAL = 15.0
DCA_CHECK_INTERVAL = 2.0
SETTLE_CHECK_INTERVAL = 10.0
SETTLE_DELAY = 15.0
# Gamma 结算有延迟，留 15 分钟正常等待窗口；超过后转入待结算队列继续轮询，
# 不再占用风控额度。再超过 SETTLE_ABANDON_WAIT 才放弃记录。
SETTLE_MAX_WAIT = 900.0
SETTLE_ABANDON_WAIT = 3600.0
# 收盘后若 Gamma 未出结果，用本地行情近似：取 end_ts 之后的第一个 tick，
# 但若距收盘超过该容差（掉线/断流）则视为不可用，宁可不判。
LOCAL_SETTLE_TOLERANCE_MS = 15000
BTC_HOUR_SECONDS = 3600.0

TRADES_CSV = "trades.csv"
TRADE_FIELDS = [
    "settle_time",
    "round_start",
    "end_time",
    "asset",
    "side",
    "result",
    "entry_price",
    "avg_fill_price",
    "spent",
    "shares",
    "payout",
    "pnl",
    "fills",
    "laggard_delta_pct",
    "laggard_gap",
    "leader_avg_price",
    "opposite_price",
    "entry_elapsed",
    "btc_hour_pct",
    "up_count",
    "down_count",
    "up_required",
    "down_required",
    "reversion_applied",
    "target",
    "final_value",
    "final_delta_pct",
    "settled_by",
]

# 逐笔成交明细（每笔立即落盘，避免进程中断丢失）。
FILLS_CSV = "trades_fills.csv"
FILL_FIELDS = [
    "fill_time",
    "round_start",
    "asset",
    "side",
    "level",
    "price",
    "amount",
    "shares",
    "cum_spent",
]

# 候选日志：每个窗口一行（无论是否开仓），用于离线研究过滤阈值。
CANDIDATES_CSV = "candidates.csv"
CANDIDATE_FIELDS = [
    "round_start",
    "end_time",
    "btc_hour_pct",
    "up_count",
    "down_count",
    "up_required",
    "down_required",
    "reversion_applied",
    "consensus",
    "candidate_asset",
    "candidate_side",
    "candidate_delta_pct",
    "candidate_gap",
    "leader_avg_price",
    "candidate_price",
    "opposite_price",
    "in_band",
    "decision",
    "entered",
    "target",
    "up_won",
    "final_value",
    "final_delta_pct",
    "settled_by",
]


def _log_dir() -> Path:
    """模式感知的日志目录：DRY_RUN 时放到 `logs/dry/`，与实盘数据隔离。"""
    base = Path(config.BASE_DIR if hasattr(config, "BASE_DIR") else ".") / ccfg.LOG_DIR
    if ccfg.DRY_RUN:
        base = base / "dry"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _rotate_csv_if_schema_changed(path: Path, fields: list[str]) -> None:
    """表头与当前字段不一致时，把旧文件改名保留，另起新文件（避免列错位）。"""
    if not path.exists():
        return
    try:
        with path.open(encoding="utf-8-sig") as fh:
            header = fh.readline().strip()
    except OSError:
        return
    if header == ",".join(fields):
        return
    legacy = path.with_name(f"{path.stem}.legacy-{int(time.time())}{path.suffix}")
    path.replace(legacy)
    log.warning("CSV 表头已变更，旧文件保留为 %s", legacy.name)


def setup_logging() -> None:
    log_dir = _log_dir()
    level = getattr(logging, ccfg.LOG_LEVEL, logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)-11s | %(message)s",
        datefmt="%H:%M:%S",
    )
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_dir / "constellation.log", encoding="utf-8"),
    ]
    logging.basicConfig(level=level, handlers=handlers)
    for handler in handlers:
        handler.setFormatter(fmt)

    order_log.setLevel(level)
    order_log.propagate = False
    order_file = logging.FileHandler(log_dir / "constellation_orders.log", encoding="utf-8")
    order_file.setFormatter(fmt)
    order_console = logging.StreamHandler(sys.stdout)
    order_console.setFormatter(fmt)
    order_log.addHandler(order_file)
    order_log.addHandler(order_console)


def _fmt_ts(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


@dataclass
class Position:
    round_start: int
    end_ts: float
    asset: str
    side: str
    token: str
    condition_id: str
    tick_size: str
    neg_risk: bool
    target: float
    delta_pct: float
    entry_price: float
    laggard_gap: float = 0.0
    leader_avg_price: float = 0.0
    opposite_price: float = 0.0
    entry_elapsed: float = 0.0
    btc_hour_pct: float | None = None
    up_required: int = 0
    down_required: int = 0
    reversion_applied: bool = False
    final_value: float | None = None
    final_delta_pct: float | None = None
    spent: float = 0.0
    shares: float = 0.0
    levels: set[int] = field(default_factory=set)
    fills: int = 0
    last_attempt: float = 0.0
    closed: bool = False
    pending: bool = False
    settled: bool = False
    settled_by: str = ""
    won: bool | None = None
    pnl: float = 0.0
    entry_ts: float = 0.0
    up_count: int = 0
    down_count: int = 0


@dataclass
class Candidate:
    """某个窗口的候选快照（无论最终是否开仓）。"""

    round_start: int
    end_ts: float
    btc_hour_pct: float | None
    up_count: int
    down_count: int
    up_required: int
    down_required: int
    reversion_applied: bool
    consensus: str
    asset: str
    side: str
    delta_pct: float | None
    gap: float | None
    leader_avg_price: float | None
    price: float | None
    opposite_price: float | None
    in_band: bool
    decision: str
    entered: bool
    target: float | None
    up_won: bool | None = None
    final_value: float | None = None
    final_delta_pct: float | None = None
    settled_by: str = ""


class ConstellationBot:
    def __init__(self, feed: MultiPriceFeed, client) -> None:
        self.feed = feed
        self.client = client
        self.strategy = ConstellationStrategy(ccfg.to_params())
        self.markets: dict[str, Market] = {}
        self.round_start: int | None = None
        self.end_ts: float = 0.0
        self.positions: dict[tuple[int, str], Position] = {}
        self.candidates: dict[tuple[int, str], Candidate] = {}
        self._attempts: dict[tuple[int, str], int] = {}
        self._last_scan = 0.0
        self._last_dca = 0.0
        self._last_settle = 0.0
        self._last_market_refresh = 0.0
        self._last_log: dict[str, float] = {}
        self._logged_targets: int | None = None

    # -- logging helper ------------------------------------------------------
    def _throttled(
        self, key: str, seconds: float, message: str, *args, level=logging.INFO
    ) -> None:
        now = time.time()
        if now - self._last_log.get(key, 0.0) >= seconds:
            self._last_log[key] = now
            log.log(level, message, *args)

    # -- per tick ------------------------------------------------------------
    async def tick(self) -> None:
        now = time.time()
        self._maybe_settle(now)
        duration = config.window_seconds()
        current = int(now // duration) * duration

        if self.round_start != current:
            if not self._roll_round(current):
                return
        if self.round_start is None:
            return

        await self._maybe_fill_markets(now)

        remaining = self.end_ts - now
        if remaining <= 0:
            self._maybe_settle(now)
            return

        if self._logged_targets != self.round_start and any(
            self._target(asset) not in (None, 0) for asset in self.markets
        ):
            self._logged_targets = self.round_start
            self._log_targets()

        elapsed = now - self.round_start
        self._maybe_settle(now)
        self._maybe_log_snapshots(now)

        if (
            elapsed >= ccfg.SCAN_AFTER_SECONDS
            and remaining > ccfg.DCA_STOP_BEFORE_CLOSE
            and ccfg.in_trading_hours(now)
        ):
            await self._maybe_enter(now, remaining)
        await self._run_dca(now, remaining)

    # -- round management ----------------------------------------------------
    def _roll_round(self, start: int) -> bool:
        markets: dict[str, Market] = {}
        for asset in ccfg.ASSETS:
            market = gamma_market.fetch_market(start, asset=asset)
            if market is not None and market.accepting:
                markets[asset] = market
        if not markets:
            self._throttled("market", 3.0, "等待新窗口市场上线: %s ..." % _fmt_ts(start))
            return False

        self.markets = markets
        self.round_start = start
        self.end_ts = max(m.end_ts for m in markets.values())
        for key in [k for k in self.positions if self.positions[k].closed]:
            del self.positions[key]
        for key in [k for k in self._attempts if k[0] < start]:
            del self._attempts[key]
        self._last_scan = 0.0
        self._last_market_refresh = 0.0
        log.info(
            "新窗口 %s-%s | 市场 %d/6: %s",
            _fmt_ts(start),
            _fmt_ts(self.end_ts),
            len(markets),
            ", ".join(a.upper() for a in markets),
        )
        return True

    async def _maybe_fill_markets(self, now: float) -> None:
        """本轮已提交但部分标的市场未就绪时，窗口内周期性补拉。"""
        if len(self.markets) >= len(ccfg.ASSETS):
            return
        if now - self._last_market_refresh < MARKET_REFRESH_INTERVAL:
            return
        self._last_market_refresh = now
        missing = [a for a in ccfg.ASSETS if a not in self.markets]
        fetched = await asyncio.gather(
            *(
                asyncio.to_thread(gamma_market.fetch_market, self.round_start, asset)
                for asset in missing
            )
        )
        added: list[str] = []
        for asset, market in zip(missing, fetched):
            if market is not None and market.accepting:
                self.markets[asset] = market
                self.end_ts = max(self.end_ts, market.end_ts)
                added.append(asset.upper())
        if added:
            self._logged_targets = None
            log.info(
                "补入市场: %s | 当前 %d/%d",
                ", ".join(added),
                len(self.markets),
                len(ccfg.ASSETS),
            )

    def _target(self, asset: str) -> float | None:
        if config.TARGET_MODE == "manual":
            return config.MANUAL_TARGET
        open_ms = self.round_start * 1000
        earliest = self.feed.earliest_ts(asset)
        if earliest is None or earliest > open_ms + ccfg.TARGET_TOLERANCE_MS:
            return None
        return self.feed.value_at_or_after(asset, open_ms)

    def _snapshots(self) -> list[AssetSnapshot]:
        snaps: list[AssetSnapshot] = []
        for asset in self.markets:
            target = self._target(asset)
            price = self.feed.latest(asset)
            if target in (None, 0) or price is None:
                continue
            snaps.append(AssetSnapshot(asset, (price - target) / target * 100.0))
        return snaps

    async def _priced_snapshots(
        self, snaps: list[AssetSnapshot], direction: str
    ) -> list[AssetSnapshot]:
        """为每个快照补齐共识方向的 CLOB 盘口价（算隐含概率差用）。"""

        async def with_price(s: AssetSnapshot) -> AssetSnapshot:
            market = self.markets.get(s.symbol)
            if market is None:
                return s
            px = await asyncio.to_thread(
                clob_price.midpoint, market.token_for(direction)
            )
            if direction == "Up":
                return AssetSnapshot(
                    s.symbol, s.delta_pct, up_price=px, down_price=s.down_price
                )
            return AssetSnapshot(
                s.symbol, s.delta_pct, up_price=s.up_price, down_price=px
            )

        return list(await asyncio.gather(*(with_price(s) for s in snaps)))

    def _log_targets(self) -> None:
        parts: list[str] = []
        for asset in ccfg.ASSETS:
            if asset not in self.markets:
                parts.append("%s=无市场" % asset.upper())
                continue
            target = self._target(asset)
            price = self.feed.latest(asset)
            if target in (None, 0):
                parts.append("%s=目标待定" % asset.upper())
            elif price is None:
                parts.append("%s=%.6g(现价待定)" % (asset.upper(), target))
            else:
                delta = (price - target) / target * 100.0
                parts.append("%s=%.6g(Δ%+.3f%%)" % (asset.upper(), target, delta))
        log.info("窗口目标价 %s | %s", _fmt_ts(self.round_start), " | ".join(parts))

    def _maybe_log_snapshots(self, now: float) -> None:
        if now - self._last_log.get("snapshots", 0.0) < SNAPSHOT_LOG_INTERVAL:
            return
        self._last_log["snapshots"] = now
        self._log_snapshots()

    def _log_snapshots(self) -> None:
        parts: list[str] = []
        for asset in ccfg.ASSETS:
            if asset not in self.markets:
                parts.append("%s=无市场" % asset.upper())
                continue
            target = self._target(asset)
            price = self.feed.latest(asset)
            if target in (None, 0):
                parts.append("%s=目标待定" % asset.upper())
            elif price is None:
                parts.append("%s=现价待定" % asset.upper())
            else:
                delta = (price - target) / target * 100.0
                parts.append("%s=%+.3f%%" % (asset.upper(), delta))
        btc_hour = (
            self.feed.change_pct_over("btc", BTC_HOUR_SECONDS)
            if "btc" in ccfg.ASSETS
            else None
        )
        direction, up_count, down_count = self.strategy.consensus(
            self._snapshots(), btc_hour
        )
        if direction is None:
            cons = "无共识 跟涨%d/跟跌%d" % (up_count, down_count)
        else:
            cons = "共识=%s 跟涨%d/跟跌%d" % (direction, up_count, down_count)
        btc_txt = f"{btc_hour:+.2f}%" if btc_hour is not None else "n/a"
        log.info("扫描 | %s | %s | BTC1h=%s", " | ".join(parts), cons, btc_txt)

    # -- entry ---------------------------------------------------------------
    async def _maybe_enter(self, now: float, remaining: float) -> None:
        if now - self._last_scan < SCAN_INTERVAL:
            return
        self._last_scan = now

        snaps = self._snapshots()
        btc_hour = self.feed.change_pct_over("btc", BTC_HOUR_SECONDS) if "btc" in ccfg.ASSETS else None
        direction, up_count, down_count = self.strategy.consensus(snaps, btc_hour)
        if direction is None:
            self._upsert_candidate(
                direction=None, up_count=up_count, down_count=down_count,
                btc_hour=btc_hour, decision="no_consensus",
            )
            return

        priced = await self._priced_snapshots(snaps, direction)
        pack_price = self.strategy.leader_pack_price(priced, direction)
        raw = self.strategy.cheapest_laggard(priced, direction)
        raw_price = raw.price_for(direction) if raw is not None else None
        gap = (pack_price - raw_price) if (pack_price is not None and raw_price is not None) else None
        laggard = self.strategy.pick_laggard(priced, direction)
        if laggard is None:
            self._upsert_candidate(
                direction=direction, up_count=up_count, down_count=down_count,
                btc_hour=btc_hour, laggard=raw, gap=gap, leader_avg=pack_price,
                target=self._target(raw.symbol) if raw is not None else None,
                decision="laggard_gap" if raw is not None else "no_laggard",
            )
            self._throttled("no_laggard", 15.0, "有共识(%s)但无落后者", direction)
            return
        key = (self.round_start, laggard.symbol)
        if key in self.positions:
            return
        if self._attempts.get(key, 0) >= ccfg.ORDER_RETRY_MAX_ATTEMPTS:
            return

        market = self.markets.get(laggard.symbol)
        if market is None:
            return
        price = laggard.price_for(direction)
        target = self._target(laggard.symbol) or 0.0
        if not self.strategy.in_band(price):
            self._upsert_candidate(
                direction=direction, up_count=up_count, down_count=down_count,
                btc_hour=btc_hour, laggard=laggard, gap=gap, leader_avg=pack_price,
                price=price, target=target, in_band=False, decision="price_band",
            )
            self._throttled(
                "band",
                15.0,
                "落后者 %s(%s) 盘口 %s 不在 $%.2f-%.2f 价带内",
                laggard.symbol.upper(),
                direction,
                f"{price:.3f}" if price is not None else "n/a",
                ccfg.ENTRY_MIN,
                ccfg.ENTRY_MAX,
            )
            return

        if not self._risk_ok(ccfg.DCA_START_USD):
            self._upsert_candidate(
                direction=direction, up_count=up_count, down_count=down_count,
                btc_hour=btc_hour, laggard=laggard, gap=gap, leader_avg=pack_price,
                price=price, target=target, in_band=True, decision="risk",
            )
            self._throttled(
                "risk",
                15.0,
                "风险上限: 持仓 %d/%d, 敞口 $%.2f/$%.2f",
                self._open_count(),
                ccfg.MAX_POSITIONS,
                self._open_exposure(),
                ccfg.MAX_EXPOSURE_USD,
                level=logging.WARNING,
            )
            return

        required = market.min_size * price
        if ccfg.DCA_START_USD < required:
            self._upsert_candidate(
                direction=direction, up_count=up_count, down_count=down_count,
                btc_hour=btc_hour, laggard=laggard, gap=gap, leader_avg=pack_price,
                price=price, target=target, in_band=True, decision="min_order",
            )
            self._throttled(
                "min_order",
                15.0,
                "首笔 $%.2f < 市场最小 %.4g 股(≈$%.2f @ %.3f)，跳过 %s %s",
                ccfg.DCA_START_USD,
                market.min_size,
                required,
                price,
                laggard.symbol.upper(),
                direction,
                level=logging.WARNING,
            )
            return

        opposite = "Down" if direction == "Up" else "Up"
        opposite_price = await asyncio.to_thread(
            clob_price.midpoint, market.token_for(opposite)
        )
        up_required, down_required = self.strategy.required_counts(btc_hour)
        required_here = up_required if direction == "Up" else down_required

        pos = Position(
            round_start=self.round_start,
            end_ts=market.end_ts,
            asset=laggard.symbol,
            side=direction,
            token=market.token_for(direction),
            condition_id=market.condition_id,
            tick_size=market.tick_size,
            neg_risk=market.neg_risk,
            target=target,
            delta_pct=laggard.delta_pct,
            entry_price=price,
            laggard_gap=gap if gap is not None else 0.0,
            leader_avg_price=pack_price or 0.0,
            opposite_price=opposite_price or 0.0,
            entry_elapsed=now - self.round_start,
            btc_hour_pct=btc_hour,
            up_required=up_required,
            down_required=down_required,
            reversion_applied=required_here < self.strategy.p.min_consensus,
            entry_ts=now,
            up_count=up_count,
            down_count=down_count,
        )
        attempt = self._attempts.get(key, 0) + 1
        self._attempts[key] = attempt
        if not await self._place(pos, ccfg.DCA_START_USD, "首笔", attempt):
            return
        self._record_fill(pos, ccfg.DCA_START_USD, price, now)
        self.positions[key] = pos
        self._upsert_candidate(
            direction=direction, up_count=up_count, down_count=down_count,
            btc_hour=btc_hour, laggard=laggard, gap=gap, leader_avg=pack_price,
            price=price, target=target, opposite_price=opposite_price,
            in_band=True, decision="entered", entered=True,
        )
        order_log.warning(
            "%s 建仓: %s %s | 落后者 Δ=%+.3f%% | 共识 U%d/D%d | 入场=%.3f | $%.2f",
            self._tag(),
            pos.asset.upper(),
            pos.side,
            pos.delta_pct,
            up_count,
            down_count,
            price,
            ccfg.DCA_START_USD,
        )

    def _upsert_candidate(
        self,
        *,
        direction: str | None,
        up_count: int,
        down_count: int,
        btc_hour: float | None,
        laggard=None,
        gap: float | None = None,
        leader_avg: float | None = None,
        price: float | None = None,
        target: float | None = None,
        opposite_price: float | None = None,
        in_band: bool = False,
        decision: str = "no_consensus",
        entered: bool = False,
    ) -> None:
        """记录/更新候选快照；按 (窗口,资产) 保留 gap 更大的快照，开仓则强制覆盖。"""
        asset = laggard.symbol if laggard is not None else ""
        key = (self.round_start, asset)
        cur = self.candidates.get(key)
        if cur is not None and not entered:
            if cur.entered:
                return
            if laggard is None:
                return
            if cur.asset and (gap is None or gap <= (cur.gap or 0.0)):
                return
        # 该窗口已有资产候选行时，不再写 no_consensus/no_laggard 汇总行；
        # 反过来，一旦出现资产候选行就丢弃同窗口的汇总行，避免同窗重复。
        if not asset and any(
            a for r, a in self.candidates if r == self.round_start and a
        ):
            return
        if asset:
            self.candidates.pop((self.round_start, ""), None)
        up_req, down_req = self.strategy.required_counts(btc_hour)
        required_here = up_req if direction == "Up" else down_req
        self.candidates[key] = Candidate(
            round_start=self.round_start,
            end_ts=self.end_ts,
            btc_hour_pct=btc_hour,
            up_count=up_count,
            down_count=down_count,
            up_required=up_req,
            down_required=down_req,
            reversion_applied=required_here < self.strategy.p.min_consensus,
            consensus=direction or "",
            asset=asset,
            side=direction if laggard is not None else "",
            delta_pct=laggard.delta_pct if laggard is not None else None,
            gap=gap,
            leader_avg_price=leader_avg,
            price=price if price is not None else (
                laggard.price_for(direction) if laggard is not None else None
            ),
            opposite_price=opposite_price,
            in_band=in_band,
            decision=decision,
            entered=entered,
            target=target,
        )

    # -- DCA -----------------------------------------------------------------
    async def _run_dca(self, now: float, remaining: float) -> None:
        if remaining <= ccfg.DCA_STOP_BEFORE_CLOSE:
            return
        if now - self._last_dca < DCA_CHECK_INTERVAL:
            return
        self._last_dca = now
        for key, pos in list(self.positions.items()):
            if pos.closed or key[0] != self.round_start:
                continue
            price = await asyncio.to_thread(clob_price.midpoint, pos.token)
            if price is None:
                continue
            step = dca_add(
                pos.entry_price,
                price,
                pos.spent,
                pos.levels,
                ccfg.DCA_MAX_USD,
                ccfg.DCA_STEP,
                ccfg.DCA_ADD2_USD,
                ccfg.DCA_ADD3_USD,
            )
            if step is None:
                continue
            if self._open_exposure() + step.amount > ccfg.MAX_EXPOSURE_USD:
                continue
            if now - pos.last_attempt < ccfg.ORDER_RETRY_INTERVAL_SECONDS:
                continue
            pos.last_attempt = now
            if not await self._place(pos, step.amount, f"加仓#{step.level}"):
                continue
            pos.levels.add(step.level)
            self._record_fill(pos, step.amount, price, now, level=step.level)
            order_log.warning(
                "%s 加仓: %s %s | 第 %d 步 $%.2f @ %.3f | 累计 $%.2f",
                self._tag(),
                pos.asset.upper(),
                pos.side,
                step.level,
                step.amount,
                price,
                pos.spent,
            )

    # -- settlement ----------------------------------------------------------
    def _maybe_settle(self, now: float) -> None:
        if now - self._last_settle < SETTLE_CHECK_INTERVAL:
            return
        self._last_settle = now
        for pos in list(self.positions.values()):
            if pos.closed or now < pos.end_ts + SETTLE_DELAY:
                continue
            won = gamma_market.fetch_outcome(pos.round_start, asset=pos.asset)
            if won is not None:
                self._finalize(pos, won, now, source="gamma")
                continue
            # Gamma 尚未出结果（通常有延迟）：先用本地行情近似落账，
            # 后台继续轮询官方结果，出来后若不一致再补一条修正记录。
            age = now - pos.end_ts
            if not pos.settled:
                local = self._local_outcome(pos)
                if local is not None:
                    self._finalize(pos, local, now, source="local")
                    pos.pending = True  # 已判定，释放风控额度，但仍等官方确认
                    continue
            if age > SETTLE_ABANDON_WAIT:
                pos.closed = True
                order_log.warning(
                    "%s 结算放弃: %s %s 超过 %.0fs 仍未取到官方结果%s",
                    self._tag(),
                    pos.asset.upper(),
                    pos.side,
                    SETTLE_ABANDON_WAIT,
                    "（保留本地近似记录）" if pos.settled else "，且无法本地近似，放弃记录",
                )
            elif not pos.pending and age > SETTLE_MAX_WAIT:
                pos.pending = True
                order_log.warning(
                    "%s 结算延迟: %s %s 超过 %.0fs 未出结果，转入待结算队列"
                    "（不再占用风控额度，后台继续轮询）",
                    self._tag(),
                    pos.asset.upper(),
                    pos.side,
                    SETTLE_MAX_WAIT,
                )
        for key, cand in list(self.candidates.items()):
            if now < cand.end_ts + SETTLE_DELAY:
                continue
            self._resolve_candidate(cand)
            self._write_candidate_csv(cand, now)
            del self.candidates[key]

    def _close_point_at(self, asset: str, end_ts: float) -> tuple[int, float] | None:
        """收盘附近的行情点 (tick_ms, value)；距收盘超过容差（断流）返回 None。"""
        end_ms = int(end_ts * 1000)
        point = self.feed.point_at_or_after(asset, end_ms)
        if point is None:
            return None
        tick_ms, _ = point
        if tick_ms - end_ms > LOCAL_SETTLE_TOLERANCE_MS:
            return None
        return point

    def _close_point(self, pos: Position) -> tuple[int, float] | None:
        return self._close_point_at(pos.asset, pos.end_ts)

    def _close_value(self, asset: str, end_ts: float) -> float | None:
        point = self._close_point_at(asset, end_ts)
        return point[1] if point is not None else None

    def _local_outcome(self, pos: Position) -> bool | None:
        """用本地行情近似窗口结算：返回 Up 是否获胜；不可用时返回 None。

        价格源为 chainlink_twap 时即官方结算源；取 end_ts 之后的第一个 tick，
        距收盘超过容差（断流）则视为不可用。
        """
        if pos.target in (None, 0):
            return None
        point = self._close_point(pos)
        if point is None:
            return None
        return point[1] >= pos.target

    def _finalize(self, pos: Position, won_up: bool, now: float, source: str) -> None:
        won = won_up if pos.side == "Up" else not won_up
        prev = pos.won if pos.settled else None
        pos.won = won
        pos.pnl = (pos.shares if won else 0.0) - pos.spent
        pos.settled = True
        pos.settled_by = source
        pos.closed = source == "gamma"
        point = self._close_point(pos)
        if point is not None:
            pos.final_value = point[1]
            if pos.target not in (None, 0):
                pos.final_delta_pct = (point[1] - pos.target) / pos.target * 100.0

        if prev is not None and prev == won:
            return  # 官方结果与本地近似一致，无需重复记录
        if prev is not None:
            order_log.warning(
                "%s 结算修正: %s %s | 本地近似 %s → 官方 %s",
                self._tag(),
                pos.asset.upper(),
                pos.side,
                "WIN" if prev else "LOSE",
                "WIN" if won else "LOSE",
            )
        else:
            order_log.warning(
                "%s 结算(%s): %s %s | %s | 支出 $%.2f 收回 $%.2f | P&L $%+.2f",
                self._tag(),
                source,
                pos.asset.upper(),
                pos.side,
                "WIN" if won else "LOSE",
                pos.spent,
                pos.shares if won else 0.0,
                pos.pnl,
            )
        self._write_trade_csv(pos, now)

    def _write_trade_csv(self, pos: Position, settle_ts: float) -> None:
        path = _log_dir() / TRADES_CSV
        _rotate_csv_if_schema_changed(path, TRADE_FIELDS)
        new_file = not path.exists()
        row = {
            "settle_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(settle_ts)),
            "round_start": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(pos.round_start)),
            "end_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(pos.end_ts)),
            "asset": pos.asset.upper(),
            "side": pos.side,
            "result": "WIN" if pos.won else "LOSE",
            "entry_price": f"{pos.entry_price:.4f}",
            "avg_fill_price": f"{pos.spent / pos.shares:.4f}" if pos.shares else "",
            "spent": f"{pos.spent:.4f}",
            "shares": f"{pos.shares:.4f}",
            "payout": f"{(pos.shares if pos.won else 0.0):.4f}",
            "pnl": f"{pos.pnl:.4f}",
            "fills": pos.fills,
            "laggard_delta_pct": f"{pos.delta_pct:.4f}",
            "laggard_gap": f"{pos.laggard_gap:.4f}",
            "leader_avg_price": f"{pos.leader_avg_price:.4f}",
            "opposite_price": f"{pos.opposite_price:.4f}",
            "entry_elapsed": f"{pos.entry_elapsed:.1f}",
            "btc_hour_pct": "" if pos.btc_hour_pct is None else f"{pos.btc_hour_pct:.4f}",
            "up_count": pos.up_count,
            "down_count": pos.down_count,
            "up_required": pos.up_required,
            "down_required": pos.down_required,
            "reversion_applied": int(pos.reversion_applied),
            "target": f"{pos.target:.6g}",
            "final_value": "" if pos.final_value is None else f"{pos.final_value:.6g}",
            "final_delta_pct": "" if pos.final_delta_pct is None else f"{pos.final_delta_pct:.4f}",
            "settled_by": pos.settled_by,
        }
        with path.open("a", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=TRADE_FIELDS)
            if new_file:
                writer.writeheader()
            writer.writerow(row)

    def _resolve_candidate(self, cand: Candidate) -> None:
        """补全候选的结算结果：优先 Gamma 官方，否则本地收盘近似。"""
        if not cand.asset:
            return
        value = self._close_value(cand.asset, cand.end_ts)
        if value is not None:
            cand.final_value = value
            if cand.target not in (None, 0):
                cand.final_delta_pct = (value - cand.target) / cand.target * 100.0
        won = gamma_market.fetch_outcome(cand.round_start, asset=cand.asset)
        if won is not None:
            cand.up_won = won
            cand.settled_by = "gamma"
        elif value is not None and cand.target not in (None, 0):
            cand.up_won = value >= cand.target
            cand.settled_by = "local"

    def _write_candidate_csv(self, cand: Candidate, now: float) -> None:
        path = _log_dir() / CANDIDATES_CSV
        _rotate_csv_if_schema_changed(path, CANDIDATE_FIELDS)
        new_file = not path.exists()
        row = {
            "round_start": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cand.round_start)),
            "end_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cand.end_ts)),
            "btc_hour_pct": "" if cand.btc_hour_pct is None else f"{cand.btc_hour_pct:.4f}",
            "up_count": cand.up_count,
            "down_count": cand.down_count,
            "up_required": cand.up_required,
            "down_required": cand.down_required,
            "reversion_applied": int(cand.reversion_applied),
            "consensus": cand.consensus,
            "candidate_asset": cand.asset.upper(),
            "candidate_side": cand.side,
            "candidate_delta_pct": "" if cand.delta_pct is None else f"{cand.delta_pct:.4f}",
            "candidate_gap": "" if cand.gap is None else f"{cand.gap:.4f}",
            "leader_avg_price": "" if cand.leader_avg_price is None else f"{cand.leader_avg_price:.4f}",
            "candidate_price": "" if cand.price is None else f"{cand.price:.4f}",
            "opposite_price": "" if cand.opposite_price is None else f"{cand.opposite_price:.4f}",
            "in_band": int(cand.in_band),
            "decision": cand.decision,
            "entered": int(cand.entered),
            "target": "" if cand.target in (None, 0) else f"{cand.target:.6g}",
            "up_won": "" if cand.up_won is None else int(cand.up_won),
            "final_value": "" if cand.final_value is None else f"{cand.final_value:.6g}",
            "final_delta_pct": "" if cand.final_delta_pct is None else f"{cand.final_delta_pct:.4f}",
            "settled_by": cand.settled_by,
        }
        with path.open("a", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=CANDIDATE_FIELDS)
            if new_file:
                writer.writeheader()
            writer.writerow(row)

    # -- risk / helpers ------------------------------------------------------
    def _open_positions(self) -> list[Position]:
        return [
            p for p in self.positions.values() if not p.closed and not p.pending
        ]

    def _open_count(self) -> int:
        return len(self._open_positions())

    def _open_exposure(self) -> float:
        return sum(p.spent for p in self._open_positions())

    def _risk_ok(self, extra: float, count_extra: int = 0) -> bool:
        if self._open_count() + count_extra > ccfg.MAX_POSITIONS:
            return False
        return self._open_exposure() + extra <= ccfg.MAX_EXPOSURE_USD

    def _record_fill(
        self,
        pos: Position,
        amount: float,
        price: float,
        now: float | None = None,
        level: int = 0,
    ) -> None:
        pos.spent += amount
        pos.shares += amount / price
        pos.fills += 1
        pos.last_attempt = time.time() if now is None else now
        self._write_fill_csv(pos, level, price, amount, pos.last_attempt)

    def _write_fill_csv(
        self, pos: Position, level: int, price: float, amount: float, fill_ts: float
    ) -> None:
        path = _log_dir() / FILLS_CSV
        _rotate_csv_if_schema_changed(path, FILL_FIELDS)
        new_file = not path.exists()
        row = {
            "fill_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(fill_ts)),
            "round_start": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(pos.round_start)),
            "asset": pos.asset.upper(),
            "side": pos.side,
            "level": level,
            "price": f"{price:.4f}",
            "amount": f"{amount:.4f}",
            "shares": f"{amount / price:.4f}",
            "cum_spent": f"{pos.spent:.4f}",
        }
        with path.open("a", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=FILL_FIELDS)
            if new_file:
                writer.writeheader()
            writer.writerow(row)

    def _tag(self) -> str:
        return "[DRY]" if ccfg.DRY_RUN else "[LIVE]"

    async def _place(self, pos: Position, amount: float, tag: str, attempt: int = 1) -> bool:
        note = "" if attempt <= 1 else " [重试 #%d]" % (attempt - 1)
        if ccfg.DRY_RUN:
            order_log.warning(
                "%s %s(模拟): %s %s token=%s amount=$%.2f",
                self._tag(),
                tag,
                pos.asset.upper(),
                pos.side,
                pos.token[:14],
                amount,
            )
            return True
        try:
            resp = await asyncio.to_thread(
                trader.market_buy,
                self.client,
                pos.token,
                amount,
                pos.tick_size,
                pos.neg_risk,
            )
            order_log.warning("%s %s 已提交%s: %s", self._tag(), tag, note, resp)
            return True
        except Exception as exc:  # noqa: BLE001
            order_log.error("%s %s 失败%s: %s", self._tag(), tag, note, exc)
            return False


async def main() -> None:
    config.validate()
    ccfg.validate()
    setup_logging()

    mode = "DRY_RUN（模拟，不真实下单）" if ccfg.DRY_RUN else "LIVE（实盘）"
    log.info("=" * 72)
    log.info("Constellation 多资产引擎启动 | 标的: %s", ", ".join(a.upper() for a in ccfg.ASSETS))
    log.info("模式: %s | 价格源: %s | 目标价: %s", mode, config.PRICE_SOURCE, config.TARGET_MODE)
    log.info("策略: %s", ConstellationStrategy(ccfg.to_params()).describe())
    log.info(
        "DCA: 首笔$%.2f + $%.2f/$%.2f(每滑%.2f) 上限$%.2f | 收盘前 %ds 停加",
        ccfg.DCA_START_USD,
        ccfg.DCA_ADD2_USD,
        ccfg.DCA_ADD3_USD,
        ccfg.DCA_STEP,
        ccfg.DCA_MAX_USD,
        ccfg.DCA_STOP_BEFORE_CLOSE,
    )
    log.info(
        "风控: 最多 %d 持仓 / 总敞口 $%.2f | 交易时段(UTC): %s",
        ccfg.MAX_POSITIONS,
        ccfg.MAX_EXPOSURE_USD,
        ccfg.TRADING_HOURS_UTC or "全天",
    )
    log.info("=" * 72)

    client = None
    if not ccfg.DRY_RUN:
        client = trader.make_client()

    feed = MultiPriceFeed(ccfg.ASSETS)
    await feed.start()
    log.info("等待价格流就绪（首轮窗口开盘价）...")

    bot = ConstellationBot(feed, client)
    try:
        while True:
            try:
                await bot.tick()
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                log.error("tick 异常: %s", exc)
            await asyncio.sleep(0.5)
    finally:
        await feed.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已停止。")
