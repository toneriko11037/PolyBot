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
SETTLE_MAX_WAIT = 300.0
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
    "up_count",
    "down_count",
    "target",
]


def setup_logging() -> None:
    log_dir = Path(config.BASE_DIR if hasattr(config, "BASE_DIR") else ".") / ccfg.LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
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
    spent: float = 0.0
    shares: float = 0.0
    levels: set[int] = field(default_factory=set)
    fills: int = 0
    last_attempt: float = 0.0
    closed: bool = False
    won: bool | None = None
    pnl: float = 0.0
    entry_ts: float = 0.0
    up_count: int = 0
    down_count: int = 0


class ConstellationBot:
    def __init__(self, feed: MultiPriceFeed, client) -> None:
        self.feed = feed
        self.client = client
        self.strategy = ConstellationStrategy(ccfg.to_params())
        self.markets: dict[str, Market] = {}
        self.round_start: int | None = None
        self.end_ts: float = 0.0
        self.positions: dict[tuple[int, str], Position] = {}
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
            return

        priced = await self._priced_snapshots(snaps, direction)
        laggard = self.strategy.pick_laggard(priced, direction)
        if laggard is None:
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
        if not self.strategy.in_band(price):
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

        pos = Position(
            round_start=self.round_start,
            end_ts=market.end_ts,
            asset=laggard.symbol,
            side=direction,
            token=market.token_for(direction),
            condition_id=market.condition_id,
            tick_size=market.tick_size,
            neg_risk=market.neg_risk,
            target=self._target(laggard.symbol) or 0.0,
            delta_pct=laggard.delta_pct,
            entry_price=price,
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
            self._record_fill(pos, step.amount, price, now)
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
            if won is None:
                if now > pos.end_ts + SETTLE_MAX_WAIT:
                    pos.closed = True
                    order_log.warning(
                        "%s 结算超时: %s %s 未取到结果，放弃记录",
                        self._tag(),
                        pos.asset.upper(),
                        pos.side,
                    )
                continue
            pos.won = won if pos.side == "Up" else not won
            payout = pos.shares if pos.won else 0.0
            pos.pnl = payout - pos.spent
            pos.closed = True
            order_log.warning(
                "%s 结算: %s %s | %s | 支出 $%.2f 收回 $%.2f | P&L $%+.2f",
                self._tag(),
                pos.asset.upper(),
                pos.side,
                "WIN" if pos.won else "LOSE",
                pos.spent,
                payout,
                pos.pnl,
            )
            self._write_trade_csv(pos, now)

    def _write_trade_csv(self, pos: Position, settle_ts: float) -> None:
        path = Path(config.BASE_DIR) / ccfg.LOG_DIR / TRADES_CSV
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
            "up_count": pos.up_count,
            "down_count": pos.down_count,
            "target": f"{pos.target:.6g}",
        }
        with path.open("a", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=TRADE_FIELDS)
            if new_file:
                writer.writeheader()
            writer.writerow(row)

    # -- risk / helpers ------------------------------------------------------
    def _open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if not p.closed]

    def _open_count(self) -> int:
        return len(self._open_positions())

    def _open_exposure(self) -> float:
        return sum(p.spent for p in self._open_positions())

    def _risk_ok(self, extra: float, count_extra: int = 0) -> bool:
        if self._open_count() + count_extra > ccfg.MAX_POSITIONS:
            return False
        return self._open_exposure() + extra <= ccfg.MAX_EXPOSURE_USD

    def _record_fill(
        self, pos: Position, amount: float, price: float, now: float | None = None
    ) -> None:
        pos.spent += amount
        pos.shares += amount / price
        pos.fills += 1
        pos.last_attempt = time.time() if now is None else now

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
