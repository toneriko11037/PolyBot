"""把 markets / prices / BTC K线 组装成可回测的窗口序列。"""

from __future__ import annotations

import bisect
import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

from . import http
from .fetch_markets import load_markets


@dataclass
class Series:
    """按秒索引的价格序列，提供无未来函数的查询。

    每根 K 线记录 open_time(ts) 与 open/close。ts 即该分钟/秒的起点。
    """

    ts: list[int] = field(default_factory=list)
    open: list[float] = field(default_factory=list)
    close: list[float] = field(default_factory=list)

    def open_at(self, t: float) -> float | None:
        """t 时刻（K 线起点）的开盘价；t 不在边界时取最近的上一根。"""
        i = bisect.bisect_left(self.ts, t)
        if i < len(self.ts) and self.ts[i] == t:
            return self.open[i]
        j = bisect.bisect_right(self.ts, t) - 1
        return self.open[j] if j >= 0 else None

    def close_before(self, t: float) -> float | None:
        """t 时刻已收盘的最后一根 K 线的收盘价（收盘时间严格 <= t，避免未来函数）。

        注意：K 线 `ts` 是 open_time，因此 open_time 落在 (t-interval, t) 的 K 线
        要到 open_time+interval（可能晚于 t）才收盘，不能使用——否则回测会偷看
        最多一个 interval 的未来现货价。
        """
        if not self.ts:
            return None
        interval = self.ts[1] - self.ts[0] if len(self.ts) > 1 else 60
        i = bisect.bisect_right(self.ts, t - interval)
        if i == 0:
            return None
        return self.close[i - 1]


@dataclass
class Window:
    start: int
    end: int
    up_token: str
    down_token: str
    up_won: bool | None
    volume: float
    target: float | None
    up_series: list[tuple[int, float]]
    down_series: list[tuple[int, float]] | None = None


def _series_from_csv(path: Path) -> Series:
    series = Series()
    if not path.exists():
        return series
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                series.ts.append(int(row["open_time"]))
                series.open.append(float(row["open"]))
                series.close.append(float(row["close"]))
            except (KeyError, ValueError):
                continue
    order = sorted(range(len(series.ts)), key=lambda i: series.ts[i])
    series.ts = [series.ts[i] for i in order]
    series.open = [series.open[i] for i in order]
    series.close = [series.close[i] for i in order]
    return series


def load_spot_series(asset: str, interval: str = "1m") -> Series:
    """加载某资产的 Binance 现货序列（BTC 兼容旧 backtest_data/btc 布局）。"""
    symbol = http.SPOT_SYMBOLS.get(asset.lower(), asset.upper())
    path = http.spot_path(symbol, interval)
    if not path.exists() and asset.lower() == "btc":
        path = http.BTC_DIR / f"{symbol.lower()}-{interval}.csv"
    return _series_from_csv(path)


def _load_price_file(start: int, asset: str = "btc") -> dict | None:
    path = http.prices_dir(asset) / f"{start}.json"
    if not path.exists() and asset == "btc":
        path = http.PRICES_DIR / f"{start}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def load_windows(
    interval: str = "1m",
    min_volume: float = 0.0,
    asset: str = "btc",
    start_ts: int | None = None,
    end_ts: int | None = None,
) -> list[Window]:
    """加载某资产的窗口。

    `start_ts` / `end_ts`（含头不含尾）可选：用于在读取盘口 JSON **之前**就跳过
    范围外的窗口，避免为全量历史做几十万次无谓文件读取。
    """
    spot = load_spot_series(asset, interval)
    windows: list[Window] = []
    for row in load_markets(asset):
        try:
            start = int(row["window_start"])
            end = int(row["window_end"])
            volume = float(row.get("volume") or 0)
        except (KeyError, ValueError):
            continue
        if volume < min_volume:
            continue
        if start_ts is not None and start < start_ts:
            continue
        if end_ts is not None and start >= end_ts:
            continue
        up_won = {1: True, 0: False, "1": True, "0": False}.get(row.get("up_won"))
        prices = _load_price_file(start, asset) or {}
        up_series = _pairs(prices.get("up"))
        down_series = _pairs(prices.get("down")) if prices.get("down") else None
        windows.append(
            Window(
                start=start,
                end=end,
                up_token=row["up_token"],
                down_token=row["down_token"],
                up_won=up_won,
                volume=volume,
                target=spot.open_at(start),
                up_series=up_series,
                down_series=down_series,
            )
        )
    return windows


# --- 多资产（Constellation 回测） -------------------------------------------


def load_asset_windows(
    asset: str,
    interval: str = "1m",
    min_volume: float = 0.0,
    start_ts: int | None = None,
    end_ts: int | None = None,
) -> dict[int, Window]:
    """某资产的窗口，按 window_start 建索引。"""
    return {
        w.start: w
        for w in load_windows(
            interval, min_volume, asset=asset, start_ts=start_ts, end_ts=end_ts
        )
    }


def load_round_index(
    assets: tuple[str, ...] | list[str],
    interval: str = "1m",
    min_volume: float = 0.0,
    start_ts: int | None = None,
    end_ts: int | None = None,
) -> tuple[list[int], dict[str, dict[int, Window]], dict[str, Series]]:
    """构建 Constellation 回测所需的「轮次 → 各资产窗口」索引。

    只保留在窗口开盘有现货价的资产；返回 (轮次列表, {asset: {start: Window}},
    {asset: Series})。`start_ts` / `end_ts` 用于限定读取范围。
    """
    windows_by_asset: dict[str, dict[int, Window]] = {}
    spot_by_asset: dict[str, Series] = {}
    starts: set[int] = set()
    for asset in assets:
        windows = load_asset_windows(
            asset, interval, min_volume, start_ts=start_ts, end_ts=end_ts
        )
        windows = {s: w for s, w in windows.items() if w.target is not None}
        windows_by_asset[asset] = windows
        spot_by_asset[asset] = load_spot_series(asset, interval)
        starts.update(windows)
    return sorted(starts), windows_by_asset, spot_by_asset


def _pairs(raw: object) -> list[tuple[int, float]]:
    out: list[tuple[int, float]] = []
    if isinstance(raw, list):
        for item in raw:
            try:
                if isinstance(item, dict):
                    out.append((int(item["t"]), float(item["p"])))
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    out.append((int(item[0]), float(item[1])))
            except (KeyError, ValueError, TypeError):
                continue
    out.sort()
    return out


def token_price(series: list[tuple[int, float]], t: float) -> float | None:
    """盘口价在 t 时刻的估计值。

    盘口价采样约每 60s 一个点，直接用“t 之前最近一个点”会引入最多 ~57s
    的滞后（价格已随行情变化，滞后点系统性更有利）。这里做线性插值，
    端点外用最近点。
    """
    if not series:
        return None
    ts = [p[0] for p in series]
    i = bisect.bisect_left(ts, t)
    if i == 0:
        return series[0][1]
    if i >= len(ts):
        return series[-1][1]
    t0, p0 = series[i - 1]
    t1, p1 = series[i]
    if t1 == t0:
        return p1
    w = (t - t0) / (t1 - t0)
    return p0 + (p1 - p0) * w
