"""Polymarket RTDS WebSocket client (free, no API key).

Streams Chainlink spot or Binance spot prices for the configured symbol and
keeps a short rolling history so the window-open reference price can be
recovered even if the bot starts mid-window.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import websockets

import config

log = logging.getLogger("feed")

HISTORY_MAX_AGE_MS = 2 * 60 * 60 * 1000
PING_INTERVAL = 5.0
RECONNECT_DELAY = 3.0


class PriceFeed:
    def __init__(self, symbol: str | None = None, source: str | None = None) -> None:
        self.symbol = symbol or config.feed_symbol()
        self.source = source or config.PRICE_SOURCE
        self.history: dict[int, float] = {}
        self.latest_ts: int = 0
        self.latest_value: float | None = None
        self.connected = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._connected_at = 0.0
        self.stall_timeout = config.FEED_STALL_TIMEOUT_SECONDS
        self.watchdog_interval = config.FEED_WATCHDOG_INTERVAL_SECONDS

    # -- public API ----------------------------------------------------------
    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="price-feed")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def value_at_or_after(self, ts_ms: int) -> float | None:
        candidates = [ts for ts in self.history if ts >= ts_ms]
        if not candidates:
            return None
        return self.history[min(candidates)]

    def earliest_ts(self) -> int | None:
        return min(self.history) if self.history else None

    # -- internals -----------------------------------------------------------
    def _subscription(self) -> dict:
        if self.source == "binance":
            return {
                "action": "subscribe",
                "subscriptions": [
                    {"topic": "crypto_prices", "type": "update", "filters": self.symbol}
                ],
            }
        filters = json.dumps({"symbol": self.symbol}, separators=(",", ":"))
        if self.source == "chainlink_twap":
            topic = (
                "crypto_prices_twap_sixty"
                if config.TWAP_WINDOW_SECONDS == 60
                else "crypto_prices_twap_thirty"
            )
            return {
                "action": "subscribe",
                "subscriptions": [
                    {"topic": topic, "type": "update", "filters": filters}
                ],
            }
        return {
            "action": "subscribe",
            "subscriptions": [
                {"topic": "crypto_prices_chainlink", "type": "*", "filters": filters}
            ],
        }

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    config.RTDS_URL, ping_interval=None, max_size=None
                ) as ws:
                    log.info(
                        "RTDS connected (%s, symbol=%s)", self.source, self.symbol
                    )
                    self.connected.set()
                    self._connected_at = time.time()
                    await ws.send(json.dumps(self._subscription()))
                    ping_task = asyncio.create_task(self._ping(ws))
                    watchdog_task = asyncio.create_task(self._watchdog(ws))
                    try:
                        async for raw in ws:
                            self._handle(raw)
                    finally:
                        ping_task.cancel()
                        watchdog_task.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on any failure
                self.connected.clear()
                log.warning("RTDS 连接异常: %s，%.0fs 后重连", exc, RECONNECT_DELAY)
                await asyncio.sleep(RECONNECT_DELAY)

    async def _ping(self, ws) -> None:
        try:
            while True:
                await asyncio.sleep(PING_INTERVAL)
                await ws.send("PING")
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - connection closed
            pass

    async def _watchdog(self, ws) -> None:
        """行情静默超过阈值时主动关闭连接，触发外层重连。

        RTDS 可能「假活」：socket 未断开、PING/PONG 正常，但某标的的行情
        推送停止。此时 `async for` 会一直挂着，永远不重连，目标价便一直待定。
        """
        try:
            while True:
                await asyncio.sleep(self.watchdog_interval)
                last = max(self.latest_ts / 1000.0, self._connected_at)
                age = time.time() - last
                if age > self.stall_timeout:
                    log.warning(
                        "RTDS 行情静默 %.0fs，强制重连 (symbol=%s)",
                        age,
                        self.symbol,
                    )
                    await ws.close()
                    return
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - connection already closing
            pass

    def _handle(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "ignore")
        if raw == "PONG":
            return
        try:
            msg = json.loads(raw)
        except ValueError:
            return

        payload = msg.get("payload") or {}
        data = payload.get("data")
        if isinstance(data, list):
            for item in data:
                self._add(item)
        else:
            self._add(payload)

    def _add(self, item: dict) -> None:
        ts = item.get("timestamp")
        value = item.get("value")
        if ts is None or value is None:
            return
        try:
            ts = int(ts)
            value = float(value)
        except (TypeError, ValueError):
            return
        self.history[ts] = value
        if ts >= self.latest_ts:
            self.latest_ts = ts
            self.latest_value = value
        self._prune()

    def _prune(self) -> None:
        cutoff = int(time.time() * 1000) - HISTORY_MAX_AGE_MS
        stale = [ts for ts in self.history if ts < cutoff]
        for ts in stale:
            del self.history[ts]
