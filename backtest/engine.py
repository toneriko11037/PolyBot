"""Constellation 回测引擎：按轮次遍历，共识 → 落后者 → 价格带 → 三步入场。

策略逻辑与实盘同源（`constellation_strategy.py`），盘口价用窗口采样点线性插值。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .dataset import Window, token_price


@dataclass
class ConstellationTrade:
    round_start: int
    asset: str
    side: str
    entry_ts: int
    entry_price: float          # 首次入场价
    avg_price: float            # 加权平均成本
    delta_pct: float            # 入场时该资产的涨跌幅
    consensus: int              # 同向资产数
    fills: int                  # 成交笔数（首笔 + 加仓）
    spent: float
    shares: float
    won: bool
    pnl: float


@dataclass
class ConstellationResult:
    trades: list[ConstellationTrade] = field(default_factory=list)
    n_rounds: int = 0
    n_no_consensus: int = 0
    n_no_laggard: int = 0
    n_band_skip: int = 0
    n_risk_skip: int = 0
    n_no_price: int = 0


def _round_asset_price(window: Window, side: str, ts: int) -> float | None:
    """某资产在 ts 时刻 side 侧盘口价（Down 优先真实盘口，否则 1-Up）。"""
    if side == "Up":
        return token_price(window.up_series, ts)
    if window.down_series:
        return token_price(window.down_series, ts)
    up_px = token_price(window.up_series, ts)
    return None if up_px is None else 1.0 - up_px


def simulate_constellation(
    round_starts: list[int],
    windows_by_asset: dict[str, dict[int, Window]],
    spot_by_asset,
    strategy,
    assets: list[str],
    dca_start: float = 5.0,
    dca_step: float = 0.09,
    dca_add2: float = 3.0,
    dca_add3: float = 2.5,
    dca_max: float = 10.5,
    max_positions: int = 3,
    max_exposure: float = 31.5,
    scan_after: int = 90,
    stop_before_close: int = 110,
    fee_rate: float = 0.0,
    slippage: float = 0.0,
    step: int = 5,
) -> ConstellationResult:
    """按轮次遍历：共识扫描 → 落后者 → 价格带 → 三步入场，持有到结算。

    保持与实盘 `constellation.py` 相同的判定顺序；盘口价用窗口采样点线性插值。
    """
    result = ConstellationResult(n_rounds=len(round_starts))
    for start in round_starts:
        # 收集本轮各资产：锚点 + 结算结果
        anchors: dict[str, tuple[Window, float, float]] = {}  # asset -> (window, target, delta_pct)
        for asset in assets:
            window = windows_by_asset.get(asset, {}).get(start)
            if window is None or window.target is None or window.up_won is None:
                continue
            anchors[asset] = (window, window.target, 0.0)
        if len(anchors) < 2:
            continue

        end = max(w.end for w, _, _ in anchors.values())
        spot = spot_by_asset
        entered = False
        for ts in range(start + step, end, step):
            if ts - start < scan_after:
                continue
            if end - ts <= stop_before_close:
                break

            # 计算各资产相对开盘的涨跌幅
            snaps = []
            for asset, (window, target, _) in anchors.items():
                price = spot[asset].close_before(ts)
                if price is None or target == 0:
                    continue
                snaps.append((asset, window, (price - target) / target * 100.0))
            if len(snaps) < 2:
                continue

            # BTC 1h 涨跌幅（近似：从 ts-3600 到 ts 的现货变化）
            btc_hour = None
            if "btc" in spot and "btc" in {s[0] for s in snaps}:
                p_now = spot["btc"].close_before(ts)
                p_past = spot["btc"].close_before(ts - 3600)
                if p_now is not None and p_past:
                    btc_hour = (p_now - p_past) / p_past * 100.0

            from constellation_strategy import AssetSnapshot

            sig_snaps = [AssetSnapshot(a, d) for a, _, d in snaps]
            direction, up_count, down_count = strategy.consensus(sig_snaps, btc_hour)
            if direction is None:
                continue

            priced_snaps = []
            for asset, window, delta in snaps:
                px = _round_asset_price(window, direction, ts)
                if direction == "Up":
                    priced_snaps.append(AssetSnapshot(asset, delta, up_price=px))
                else:
                    priced_snaps.append(AssetSnapshot(asset, delta, down_price=px))
            laggard = strategy.pick_laggard(priced_snaps, direction)
            if laggard is None:
                continue
            lag_window = windows_by_asset[laggard.symbol][start]
            entry_price = laggard.price_for(direction)
            if entry_price is None:
                result.n_no_price += 1
                continue
            if not strategy.in_band(entry_price):
                result.n_band_skip += 1
                continue

            # 三步入场（按后续采样点检测下滑）
            spent = dca_start
            shares = dca_start / max(min(entry_price + slippage, 0.999), 0.001)
            fills = 1
            levels: set[int] = set()
            for t2 in range(ts + step, end, step):
                if end - t2 <= stop_before_close:
                    break
                px = _round_asset_price(lag_window, direction, t2)
                if px is None or spent >= dca_max:
                    continue
                from constellation_strategy import dca_add

                s = dca_add(
                    entry_price, px, spent, levels, dca_max, dca_step, dca_add2, dca_add3
                )
                if s is None:
                    continue
                levels.add(s.level)
                fills += 1
                fill_px = max(min(px + slippage, 0.999), 0.001)
                shares += s.amount / fill_px
                spent += s.amount

            won = lag_window.up_won if direction == "Up" else not lag_window.up_won
            fee = fee_rate * spent
            payout = shares if won else 0.0
            pnl = payout - spent - fee
            result.trades.append(
                ConstellationTrade(
                    round_start=start,
                    asset=laggard.symbol,
                    side=direction,
                    entry_ts=ts,
                    entry_price=entry_price,
                    avg_price=spent / shares if shares else 0.0,
                    delta_pct=laggard.delta_pct,
                    consensus=up_count if direction == "Up" else down_count,
                    fills=fills,
                    spent=spent,
                    shares=shares,
                    won=won,
                    pnl=pnl,
                )
            )
            entered = True
            break
    return result
