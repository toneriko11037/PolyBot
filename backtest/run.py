"""回测命令行入口。

示例:
    python -m backtest.run fetch-markets --start 2026-02-01
    python -m backtest.run fetch-spot --method daily --start 2026-02-01
    python -m backtest.run fetch-prices --start 2026-02-01 --workers 6
    python -m backtest.run constellation --assets btc,eth,sol,xrp,doge,bnb \
        --params "min_consensus=4|5,laggard_gap=0.15"
"""

from __future__ import annotations

import argparse
import datetime as _dt
from pathlib import Path

from . import http
from .dataset import load_round_index
from .engine import simulate_constellation
from .grid import expand_grid
from .report import (
    constellation_metrics,
    print_constellation_metrics,
    print_grid,
    write_constellation_trades,
)


def _day_ts(value: str | None, end: bool = False) -> int | None:
    if not value:
        return None
    d = _dt.datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=_dt.timezone.utc)
    if end:
        d += _dt.timedelta(days=1)
    return int(d.timestamp())


def cmd_fetch_markets(args) -> None:
    from .fetch_markets import _parse_assets, _parse_day, datetime_today, fetch_all

    fetch_all(
        _parse_day(args.start),
        _parse_day(args.end or datetime_today()),
        _parse_assets(getattr(args, "asset", None)),
        force=args.force,
    )


def cmd_fetch_prices(args) -> None:
    import sys
    from . import fetch_prices

    sys.argv = ["fetch_prices", "--workers", str(args.workers)]
    for name in ("start", "end", "min_volume", "asset"):
        val = getattr(args, name, None)
        if val is not None:
            sys.argv += [f"--{name.replace('_', '-')}", str(val)]
    if args.both:
        sys.argv.append("--both")
    fetch_prices.main()


def cmd_fetch_spot(args) -> None:
    from . import fetch_btc

    start = _dt.datetime.strptime(args.start, "%Y-%m-%d").date()
    end = _dt.datetime.strptime(
        args.end or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d"), "%Y-%m-%d"
    ).date()
    assets = [a.strip().lower() for a in args.assets.split(",") if a.strip()]
    fetch_btc.fetch_spot_all(start, end, assets, method=getattr(args, "method", "daily"))


def cmd_constellation(args) -> None:
    """Constellation 多资产回测：共识 + 落后者 + 三步入场。"""
    http.ensure_dirs()
    assets = [a.strip().lower() for a in args.assets.split(",") if a.strip()]
    lo = _day_ts(args.start)
    hi = _day_ts(args.end, end=True)
    round_starts, windows_by_asset, spot_by_asset = load_round_index(
        assets,
        interval="1m",
        min_volume=args.min_volume,
        start_ts=lo,
        end_ts=hi,
    )
    round_starts = [
        s for s in round_starts
        if (lo is None or s >= lo) and (hi is None or s < hi)
    ]
    if not round_starts:
        print("没有可用轮次：请先 fetch-markets / fetch-prices / fetch-spot 多资产数据。")
        return

    from constellation_strategy import ConstellationParams, ConstellationStrategy

    params = _parse_params(args.params)
    combos = expand_grid(params) if params else [{}]
    print(
        f"资产 {','.join(a.upper() for a in assets)} | 轮次 {len(round_starts)} | "
        f"组合 {len(combos)} | 费率 {args.fee_rate*100:g}% | 滑点 {args.slippage:g}"
    )

    rows = []
    last_result = None
    for combo in combos:
        p = {
            "move_pct": float(combo.get("move_pct", 0.03)),
            "avg_move_pct": float(combo.get("avg_move_pct", 0.0)),
            "laggard_gap": float(combo.get("laggard_gap", 0.15)),
            "min_consensus": int(combo.get("min_consensus", 4)),
            "btc_reversion_pct": float(combo.get("btc_reversion_pct", 2.0)),
            "reversion_relax": int(combo.get("reversion_relax", 1)),
            "entry_min": float(combo.get("entry_min", 0.38)),
            "entry_max": float(combo.get("entry_max", 0.68)),
        }
        strategy = ConstellationStrategy(ConstellationParams(**p))
        result = simulate_constellation(
            round_starts,
            windows_by_asset,
            spot_by_asset,
            strategy,
            assets,
            dca_start=float(combo.get("dca_start", 5.0)),
            dca_step=float(combo.get("dca_step", 0.09)),
            dca_add2=float(combo.get("dca_add2", 3.0)),
            dca_add3=float(combo.get("dca_add3", 2.5)),
            dca_max=float(combo.get("dca_max", 10.5)),
            max_positions=int(combo.get("max_positions", 3)),
            max_exposure=float(combo.get("max_exposure", 31.5)),
            scan_after=int(combo.get("scan_after", 90)),
            stop_before_close=int(combo.get("stop_before_close", 110)),
            fee_rate=args.fee_rate,
            slippage=args.slippage,
            step=args.step,
        )
        label = "constellation" + (
            "(" + ",".join(f"{k}={v}" for k, v in combo.items()) + ")" if combo else ""
        )
        if len(combos) == 1:
            print_constellation_metrics(label, result)
        else:
            rows.append((label, constellation_metrics(result)))
        last_result = result

    if rows:
        print_grid(rows)
    if args.trades_out and last_result is not None:
        write_constellation_trades(Path(args.trades_out), last_result)
        print(f"\n成交明细已写入 {args.trades_out}")


def _parse_params(raw: str | None) -> dict:
    if not raw:
        return {}
    params = {}
    for item in raw.split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        params[key.strip()] = value.strip()
    return params


def main() -> None:
    ap = argparse.ArgumentParser(description="Polymarket 5m Constellation 回测")
    sub = ap.add_subparsers(dest="cmd", required=True)

    fm = sub.add_parser("fetch-markets", help="枚举历史窗口（多资产）")
    fm.add_argument("--start", default="2025-12-17")
    fm.add_argument("--end", default=None)
    fm.add_argument("--asset", default=None, help="逗号分隔；默认全部")
    fm.add_argument("--force", action="store_true")
    fm.set_defaults(func=cmd_fetch_markets)

    fp = sub.add_parser("fetch-prices", help="下载盘口价历史（多资产）")
    fp.add_argument("--start", default=None)
    fp.add_argument("--end", default=None)
    fp.add_argument("--asset", default=None, help="逗号分隔；默认全部")
    fp.add_argument("--workers", type=int, default=6)
    fp.add_argument("--both", action="store_true")
    fp.add_argument("--min-volume", type=float, default=0.0, dest="min_volume")
    fp.set_defaults(func=cmd_fetch_prices)

    fs = sub.add_parser("fetch-spot", help="下载多资产现货 1m K 线（Binance）")
    fs.add_argument("--method", choices=("daily", "rest"), default="daily")
    fs.add_argument("--assets", default="btc,eth,sol,xrp,doge,bnb")
    fs.add_argument("--start", default="2025-12-17")
    fs.add_argument("--end", default=None)
    fs.set_defaults(func=cmd_fetch_spot)

    co = sub.add_parser("constellation", help="Constellation 多资产回测（共识+落后者+三步入场）")
    co.add_argument("--assets", default="btc,eth,sol,xrp,doge,bnb")
    co.add_argument("--params", default=None,
                    help='如 "min_consensus=4|5,laggard_gap=0.15,entry_min=0.38,entry_max=0.68"')
    co.add_argument("--fee-rate", type=float, default=0.0, dest="fee_rate")
    co.add_argument("--slippage", type=float, default=0.0)
    co.add_argument("--min-volume", type=float, default=0.0, dest="min_volume")
    co.add_argument("--start", default=None)
    co.add_argument("--end", default=None)
    co.add_argument("--step", type=int, default=5)
    co.add_argument("--trades-out", default=None, dest="trades_out")
    co.set_defaults(func=cmd_constellation)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
