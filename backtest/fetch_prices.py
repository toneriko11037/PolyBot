"""下载每个 5m 窗口的盘口价历史（CLOB /prices-history）。

默认只拉 Up token（Down ≈ 1 - Up，可省一半请求）；--both 拉两侧。
可断点续传：backtest_data/prices/<window_start>.json 已存在则跳过。

用法:
    python -m backtest.fetch_prices --start 2026-02-01 --workers 6
    python -m backtest.fetch_prices --both --min-volume 1000
"""

from __future__ import annotations

import argparse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import http
from .fetch_markets import load_markets

CLOB = http.config.CLOB_HOST
_lock = threading.Lock()
_done = 0


def _history(token: str, start: int, end: int) -> list | None:
    """成功返回价格点列表（可能为空），失败返回 None（不缓存，下次重试）。"""
    data = http.get_json(
        f"{CLOB}/prices-history",
        {
            "market": token,
            "startTs": start,
            "endTs": end,
            "fidelity": http.FIDELITY_MIN,
        },
    )
    if isinstance(data, dict):
        return data.get("history") or []
    return None


def _price_path(asset: str, start: int) -> "object":
    # BTC 兼容旧布局：prices/<start>.json 存在则先用旧文件
    from pathlib import Path

    old = http.PRICES_DIR / f"{start}.json"
    if asset == "btc" and old.exists():
        return old
    return http.prices_dir(asset) / f"{start}.json"


def fetch_one(row: dict, asset: str = "btc", both: bool = False) -> tuple[int, bool]:
    start = int(row["window_start"])
    end = int(row["window_end"])
    path = _price_path(asset, start)
    if path.exists():
        return start, False
    up = _history(row["up_token"], start, end)
    if up is None:
        return start, False  # 请求失败，不写文件，留待下次重试
    payload = {"start": start, "up": up}
    if both:
        down = _history(row["down_token"], start, end)
        if down is None:
            return start, False
        payload["down"] = down
    http.write_json(path, payload)
    return start, True


def _parse_day(value: str | None) -> int | None:
    if not value:
        return None
    import datetime as _dt

    d = _dt.datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=_dt.timezone.utc)
    return int(d.timestamp())


def _vol(r: dict) -> float:
    try:
        return float(r.get("volume") or 0)
    except ValueError:
        return 0.0


def _fetch_asset(asset: str, lo: int, hi: int, both: bool, workers: int,
                 min_volume: float) -> None:
    rows = [r for r in load_markets(asset) if lo <= int(r["window_start"]) <= hi]
    if min_volume > 0:
        rows = [r for r in rows if _vol(r) >= min_volume]
    todo = [r for r in rows if not _price_path(asset, int(r["window_start"])).exists()]
    print(f"[{asset}] 窗口 {len(rows)}，待下载 {len(todo)}（并发 {workers}）")
    if not todo:
        return
    global _done
    done_asset = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch_one, r, asset, both) for r in todo]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as exc:  # noqa: BLE001
                print(f"    [warn] {exc}")
            done_asset += 1
            with _lock:
                _done += 1
            if done_asset % 200 == 0 or done_asset == len(todo):
                print(f"  [{asset}] 进度 {done_asset}/{len(todo)}")


def main() -> None:
    ap = argparse.ArgumentParser(description="下载 5m 窗口盘口价历史（多资产）")
    ap.add_argument("--start", default=None, help="起始日 (YYYY-MM-DD)")
    ap.add_argument("--end", default=None, help="结束日 (YYYY-MM-DD)")
    ap.add_argument("--asset", default=None, help="资产，逗号分隔；默认全部")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--both", action="store_true", help="同时下载 Down token")
    ap.add_argument("--min-volume", type=float, default=0.0, help="跳过成交量低于该值的窗口")
    args = ap.parse_args()

    assets = [a.strip().lower() for a in (args.asset or ",".join(http.ASSETS)).split(",") if a.strip()]
    http.ensure_dirs(assets)
    lo = _parse_day(args.start) or 0
    end_day = _parse_day(args.end)
    hi = (end_day + 86400) if end_day is not None else 2**62

    for asset in assets:
        _fetch_asset(asset, lo, hi, args.both, args.workers, args.min_volume)
    print(f"完成 -> {http.PRICES_DIR}")


if __name__ == "__main__":
    main()
