"""枚举 Polymarket BTC 5m Up/Down 历史上所有窗口，写入 backtest_data/markets/<day>.csv。

数据源：Gamma /events?series_id=10684，按天切片 + offset 翻页（limit 上限 100）。
可断点续传：已存在的按天文件默认跳过。

用法:
    python -m backtest.fetch_markets --start 2026-02-01 --end 2026-10-01
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import time
from pathlib import Path

from . import http

GAMMA = http.config.GAMMA_HOST
CSV_FIELDS = [
    "window_start", "window_end", "slug", "condition_id",
    "up_token", "down_token", "up_won", "volume", "liquidity", "closed",
]


def _parse_day(value: str) -> _dt.date:
    return _dt.datetime.strptime(value, "%Y-%m-%d").date()


def _window_start(event: dict, market: dict) -> int | None:
    slug = event.get("slug") or ""
    if "-updown-5m-" in slug:
        try:
            return int(slug.rsplit("-", 1)[1])
        except ValueError:
            pass
    st = event.get("startTime") or market.get("startDateIso")
    if st:
        try:
            return int(
                _dt.datetime.fromisoformat(st.replace("Z", "+00:00")).timestamp()
            )
        except ValueError:
            return None
    return None


def _outcome_tokens(outcomes: list, tokens: list) -> tuple[str, str]:
    up = down = None
    for label, token in zip(outcomes, tokens):
        low = str(label).strip().lower()
        if low == "up":
            up = token
        elif low == "down":
            down = token
    if up is None or down is None:
        up, down = tokens[0], tokens[1]
    return str(up), str(down)


def _up_won(outcomes: list, prices: list, closed: bool) -> str:
    if not closed or len(prices) < 2:
        return ""
    for label, price in zip(outcomes, prices):
        if str(label).strip().lower() == "up":
            try:
                return "1" if float(price) >= 0.5 else "0"
            except (TypeError, ValueError):
                return ""
    return ""


def _parse_row(event: dict, market: dict) -> dict | None:
    start = _window_start(event, market)
    if start is None:
        return None
    try:
        outcomes = json.loads(market.get("outcomes") or "[]")
        tokens = json.loads(market.get("clobTokenIds") or "[]")
        prices = json.loads(market.get("outcomePrices") or "[]")
    except (ValueError, TypeError):
        return None
    if len(tokens) < 2:
        return None
    up_token, down_token = _outcome_tokens(outcomes, tokens)
    closed = bool(market.get("closed"))

    def _num(key: str) -> str:
        v = market.get(key)
        return "" if v is None else str(v)

    return {
        "window_start": start,
        "window_end": start + http.WINDOW_SECONDS,
        "slug": event.get("slug") or "",
        "condition_id": str(market.get("conditionId") or ""),
        "up_token": up_token,
        "down_token": down_token,
        "up_won": _up_won(outcomes, prices, closed),
        "volume": _num("volumeNum"),
        "liquidity": _num("liquidityNum"),
        "closed": int(closed),
    }


def fetch_day(day: _dt.date, series_id: int = http.SERIES_ID, max_pages: int = 200) -> list[dict]:
    start_ts = _dt.datetime(day.year, day.month, day.day, tzinfo=_dt.timezone.utc)
    end_ts = start_ts + _dt.timedelta(days=1)
    rows: dict[int, dict] = {}
    offset = 0
    for _ in range(max_pages):
        batch = http.get_json(
            f"{GAMMA}/events",
            {
                "series_id": series_id,
                "limit": 100,
                "offset": offset,
                "order": "startDate",
                "ascending": "true",
                "end_date_min": http.iso(start_ts.timestamp()),
                "end_date_max": http.iso(end_ts.timestamp()),
            },
        )
        if not isinstance(batch, list) or not batch:
            break
        for event in batch:
            market = (event.get("markets") or [{}])[0]
            row = _parse_row(event, market)
            if row:
                rows[row["window_start"]] = row
        if len(batch) < 100:
            break
        offset += len(batch)
    return [rows[k] for k in sorted(rows)]


def _existing_starts(path: Path) -> set[int]:
    """读取已存在的单资产 CSV 中已下载的 window_start，支持断点续传。"""
    done: set[int] = set()
    if not path.exists():
        return done
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                done.add(int(row["window_start"]))
            except (KeyError, ValueError):
                continue
    return done


def fetch_range(
    start: _dt.date,
    end: _dt.date,
    asset: str = "btc",
    force: bool = False,
) -> None:
    asset = asset.lower()
    series_id = http.SERIES_IDS[asset]
    http.ensure_dirs([asset])
    path = http.markets_path(asset)
    done = set() if force else _existing_starts(path)
    mode = "w" if (force or not path.exists()) else "a"
    added = 0
    day = start
    with path.open(mode, newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        if mode == "w":
            writer.writeheader()
        while day <= end:
            rows = [r for r in fetch_day(day, series_id=series_id) if r["window_start"] not in done]
            if rows:
                writer.writerows(rows)
                fh.flush()
                done.update(r["window_start"] for r in rows)
                added += len(rows)
                print(f"  {asset} {day.isoformat()}: +{len(rows)} 个窗口")
            day += _dt.timedelta(days=1)
            time.sleep(0.15)
    print(f"完成 {asset}，共新增 {added} 行 -> {path}")


def fetch_all(
    start: _dt.date,
    end: _dt.date,
    assets: tuple[str, ...] | list[str] | None = None,
    force: bool = False,
) -> None:
    for asset in (assets or http.ASSETS):
        asset_start = _dt.datetime.strptime(http.ASSET_START[asset], "%Y-%m-%d").date()
        lo = max(start, asset_start)
        if lo > end:
            print(f"跳过 {asset}（{http.ASSET_START[asset]} 起才有市场）")
            continue
        fetch_range(lo, end, asset=asset, force=force)


def load_markets(asset: str = "btc") -> list[dict]:
    """读取某资产已下载的市场 CSV，按 window_start 排序。

    BTC 兼容旧布局：若 markets/btc.csv 不存在，则回退读取 markets/<day>.csv。
    """
    path = http.markets_path(asset)
    files = [path] if path.exists() else []
    if not files and asset == "btc":
        files = sorted(http.MARKETS_DIR.glob("*.csv"))
    by_start: dict[int, dict] = {}
    for f in files:
        with f.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                try:
                    by_start[int(row["window_start"])] = row
                except (KeyError, ValueError):
                    continue
    return [by_start[k] for k in sorted(by_start)]


def _parse_assets(raw: str | None) -> list[str]:
    if not raw:
        return list(http.ASSETS)
    return [a.strip().lower() for a in raw.split(",") if a.strip()]


def main() -> None:
    ap = argparse.ArgumentParser(description="枚举 Polymarket 5m 历史窗口（多资产）")
    ap.add_argument("--start", default="2025-12-17", help="起始日 (YYYY-MM-DD)")
    ap.add_argument("--end", default=datetime_today(), help="结束日 (YYYY-MM-DD)")
    ap.add_argument("--asset", default=None, help="资产，逗号分隔；默认全部（btc,eth,sol,xrp,doge,bnb）")
    ap.add_argument("--force", action="store_true", help="覆盖已存在文件")
    args = ap.parse_args()
    fetch_all(_parse_day(args.start), _parse_day(args.end), _parse_assets(args.asset), force=args.force)


def datetime_today() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


if __name__ == "__main__":
    main()
