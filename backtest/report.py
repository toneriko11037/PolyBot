"""回测指标与报告输出（Constellation）。"""

from __future__ import annotations

import csv
from pathlib import Path


def constellation_metrics(result) -> dict:
    trades = result.trades
    n = len(trades)
    if n == 0:
        return {
            "trades": 0, "win_rate": 0.0, "total_stake": 0.0, "pnl": 0.0,
            "roi": 0.0, "avg_pnl": 0.0, "profit_factor": 0.0, "max_drawdown": 0.0,
            "avg_entry": 0.0, "avg_fills": 0.0,
        }
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    total_stake = sum(t.spent for t in trades)
    pnl = sum(t.pnl for t in trades)
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    equity = peak = max_dd = 0.0
    for t in trades:
        equity += t.pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "trades": n,
        "win_rate": sum(1 for t in trades if t.won) / n,
        "total_stake": total_stake,
        "pnl": pnl,
        "roi": pnl / total_stake if total_stake else 0.0,
        "avg_pnl": pnl / n,
        "profit_factor": (gross_win / gross_loss) if gross_loss else float("inf"),
        "max_drawdown": max_dd,
        "avg_entry": sum(t.avg_price for t in trades) / n,
        "avg_fills": sum(t.fills for t in trades) / n,
    }


def print_constellation_metrics(name: str, result) -> dict:
    m = constellation_metrics(result)
    print(f"\n=== Constellation: {name} ===")
    print(
        f"  轮次 {result.n_rounds} | 成交 {m['trades']} | "
        f"无共识窗口 {result.n_no_consensus} | 无落后者 {result.n_no_laggard} | "
        f"价带跳过 {result.n_band_skip} | 缺价 {result.n_no_price}"
    )
    if m["trades"] == 0:
        print("  无成交。")
        return m
    pf = m["profit_factor"]
    print(
        f"  结算胜率 {m['win_rate']*100:.1f}% | 总投入 ${m['total_stake']:.0f} | "
        f"PnL ${m['pnl']:+.2f} | ROI {m['roi']*100:+.2f}%"
    )
    print(
        f"  单笔均值 ${m['avg_pnl']:+.3f} | 盈亏比(PF) "
        f"{'inf' if pf == float('inf') else f'{pf:.2f}'} | "
        f"最大回撤 ${m['max_drawdown']:.2f} | 均入场价 {m['avg_entry']:.3f} | "
        f"均成交笔数 {m['avg_fills']:.2f}"
    )
    return m


def write_constellation_trades(path: Path, result) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "round_start", "asset", "side", "entry_ts", "entry_price", "avg_price",
            "delta_pct", "consensus", "fills", "spent", "shares", "won", "pnl",
        ])
        for t in result.trades:
            writer.writerow([
                t.round_start, t.asset, t.side, t.entry_ts, f"{t.entry_price:.4f}",
                f"{t.avg_price:.4f}", f"{t.delta_pct:.4f}", t.consensus, t.fills,
                f"{t.spent:.2f}", f"{t.shares:.4f}", int(t.won), f"{t.pnl:.4f}",
            ])


def print_grid(rows: list[tuple[str, dict]]) -> None:
    if not rows:
        return
    print("\n" + "=" * 78)
    print("参数网格结果（按总 PnL 降序）")
    print(f"{'策略/参数':<44}{'成交':>6}{'胜率':>8}{'PnL':>10}{'ROI':>9}{'回撤':>9}")
    print("-" * 78)
    for label, m in sorted(rows, key=lambda r: r[1]["pnl"], reverse=True):
        print(
            f"{label:<44}{m['trades']:>6}{m['win_rate']*100:>7.1f}%"
            f"{m['pnl']:>10.2f}{m['roi']*100:>8.2f}%{m['max_drawdown']:>9.2f}"
        )
    print("=" * 78)
