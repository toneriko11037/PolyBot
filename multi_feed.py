"""多资产价格流：为 Constellation 的 6 个标的各维护一条 RTDS 连接。

为什么每个标的一条连接？RTDS 允许多 symbol 订阅，但订阅瞬间推送的历史回填
数组**不带 symbol 字段**，多资产混在一起无法区分归属。每个 symbol 单独一条
连接即可让 `price_feed.PriceFeed` 原样工作（它按 symbol 过滤）。

对外只暴露「按资产」的查询接口，内部复用单资产 `PriceFeed`。
"""

from __future__ import annotations

import logging
import time

import config
from price_feed import PriceFeed

log = logging.getLogger("multi_feed")


class MultiPriceFeed:
    def __init__(
        self,
        assets: list[str],
        source: str | None = None,
    ) -> None:
        self.source = source or config.PRICE_SOURCE
        self.feeds: dict[str, PriceFeed] = {
            asset.lower(): PriceFeed(
                symbol=config.feed_symbol_for(asset, self.source),
                source=self.source,
            )
            for asset in assets
        }

    async def start(self) -> None:
        for feed in self.feeds.values():
            await feed.start()
        log.info(
            "多资产价格流启动: %s",
            ", ".join(f"{a}={f.symbol}" for a, f in self.feeds.items()),
        )

    async def stop(self) -> None:
        for feed in self.feeds.values():
            await feed.stop()

    # -- queries -------------------------------------------------------------
    def latest(self, asset: str) -> float | None:
        feed = self.feeds.get(asset.lower())
        return feed.latest_value if feed else None

    def value_at_or_after(self, asset: str, ts_ms: int) -> float | None:
        feed = self.feeds.get(asset.lower())
        return feed.value_at_or_after(ts_ms) if feed else None

    def earliest_ts(self, asset: str) -> int | None:
        feed = self.feeds.get(asset.lower())
        return feed.earliest_ts() if feed else None

    def change_pct_over(
        self, asset: str, seconds: float, now_ms: int | None = None
    ) -> float | None:
        """资产最近 `seconds` 秒的百分比变化；历史不足时返回 None。"""
        feed = self.feeds.get(asset.lower())
        if feed is None or feed.latest_value in (None, 0):
            return None
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        past = feed.value_at_or_after(now_ms - int(seconds * 1000))
        if past is None or past == 0:
            return None
        return (feed.latest_value - past) / past * 100.0
