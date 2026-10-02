"""下载多资产 Binance 现货 1m K 线，作为各自 Chainlink 价格的近似。

- REST /api/v3/klines 适合近期；
- data.binance.vision 每日归档 zip 能取到更久历史（推荐）。

用法:
    python -m backtest.fetch_btc --method daily --start 2026-02-01
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import io
import time
import zipfile
from pathlib import Path

import requests

from . import http

BINANCE_REST = "https://api.binance.com"
BINANCE_VISION = "https://data.binance.vision"
SYMBOL = "BTCUSDT"  # 兼容旧引用
FIELDS = ["open_time", "open", "high", "low", "close", "volume"]


def _day(value: str) -> _dt.date:
    return _dt.datetime.strptime(value, "%Y-%m-%d").date()


def _append_rows(path: Path, rows: list[list[str]]) -> None:
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if new:
            writer.writerow(FIELDS)
        writer.writerows(rows)


def _to_seconds(raw: str) -> int:
    """Binance 归档的 open_time 可能是秒/毫秒/微秒，统一成秒。"""
    v = int(raw)
    while v > 10**11:  # > ~5138 年（秒）说明还有更高精度
        v //= 1000
    return v


def _last_ts(path: Path) -> int | None:
    """已下载 csv 的最后一根 open_time（秒），用于断点续传。"""
    if not path.exists():
        return None
    last: int | None = None
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                last = int(row["open_time"])
            except (KeyError, ValueError):
                continue
    return last


def fetch_1m(start: _dt.date, end: _dt.date, symbol: str = SYMBOL, path: Path | None = None) -> None:
    symbol = symbol.upper()
    if path is None:
        http.ensure_dirs()
        path = http.spot_path(symbol, "1m")
    start_ms = int(
        _dt.datetime(start.year, start.month, start.day, tzinfo=_dt.timezone.utc).timestamp() * 1000
    )
    end_ms = int(
        (_dt.datetime(end.year, end.month, end.day, tzinfo=_dt.timezone.utc)
         + _dt.timedelta(days=1)).timestamp() * 1000
    )
    resume = _last_ts(path)
    cursor = max(start_ms, (resume + 60) * 1000) if resume is not None else start_ms
    total = 0
    while cursor < end_ms:
        data = http.get_json(
            f"{BINANCE_REST}/api/v3/klines",
            {"symbol": symbol, "interval": "1m", "startTime": cursor,
             "endTime": min(cursor + 999 * 60000, end_ms - 1), "limit": 1000},
        )
        if not isinstance(data, list) or not data:
            break
        rows = [[_to_seconds(str(k[0])), k[1], k[2], k[3], k[4], k[5]] for k in data]
        _append_rows(path, rows)
        total += len(rows)
        cursor = int(data[-1][0]) + 60000
        print(f"  {symbol} 1m 已写入 {total} 根 (到 {http.iso(cursor/1000)})")
        time.sleep(0.1)
    print(f"完成 {symbol} -> {path}")


def fetch_1m_daily(
    start: _dt.date, end: _dt.date, symbol: str = SYMBOL, path: Path | None = None
) -> None:
    """按天从 data.binance.vision 下载 1m 归档（比 REST 能取到更久历史）。"""
    symbol = symbol.upper()
    if path is None:
        http.ensure_dirs()
        path = http.spot_path(symbol, "1m")
    done = _last_ts(path)
    day = start
    total = 0
    while day <= end:
        if done is not None and int(_dt.datetime(day.year, day.month, day.day,
                                                  tzinfo=_dt.timezone.utc).timestamp()) <= done:
            day += _dt.timedelta(days=1)
            continue
        ds = day.isoformat()
        url = (
            f"{BINANCE_VISION}/data/spot/daily/klines/{symbol}/1m/"
            f"{symbol}-1m-{ds}.zip"
        )
        try:
            resp = requests.get(url, timeout=60)
            if resp.status_code != 200:
                day += _dt.timedelta(days=1)
                continue
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                with zf.open(zf.namelist()[0]) as fh:
                    reader = csv.reader(io.TextIOWrapper(fh, encoding="utf-8"))
                    rows = []
                    for row in reader:
                        if len(row) < 6 or not row[0].isdigit():
                            continue
                        rows.append([_to_seconds(row[0]), row[1], row[2], row[3], row[4], row[5]])
            _append_rows(path, rows)
            total += len(rows)
            print(f"  {symbol} {ds}: {len(rows)} 根 (累计 {total})")
        except Exception as exc:  # noqa: BLE001
            print(f"  {symbol} {ds}: 跳过 ({exc})")
        day += _dt.timedelta(days=1)
        time.sleep(0.15)
    print(f"完成 {symbol} -> {path}")


def fetch_spot_all(start: _dt.date, end: _dt.date, assets=None, method: str = "daily") -> None:
    """下载多资产 Binance 1m 现货（近似各自 Chainlink 结算源）。

    method="daily" 用 data.binance.vision 日归档（历史更长，推荐）；
    method="rest" 用 REST /klines（只覆盖近期）。
    """
    http.ensure_dirs()
    names = assets or http.ASSETS
    for asset in names:
        symbol = http.SPOT_SYMBOLS[asset]
        if method == "rest":
            fetch_1m(start, end, symbol=symbol)
        else:
            fetch_1m_daily(start, end, symbol=symbol)


def main() -> None:
    ap = argparse.ArgumentParser(description="下载 Binance 现货 1m K 线（多资产）")
    ap.add_argument("--method", choices=("daily", "rest"), default="daily")
    ap.add_argument("--asset", default=None, help="资产，逗号分隔；默认全部")
    ap.add_argument("--start", default="2025-12-17")
    ap.add_argument("--end", default=_dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d"))
    args = ap.parse_args()
    assets = [a.strip().lower() for a in (args.asset or ",".join(http.ASSETS)).split(",") if a.strip()]
    fetch_spot_all(_day(args.start), _day(args.end), assets, method=args.method)


if __name__ == "__main__":
    main()
