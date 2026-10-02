"""Constellation 策略：多资产共识 + 落后者补涨/补跌，纯函数化便于测试。

依据 KumiBot 官网公开的规则（5minutebot.com）：

1) 共识扫描（T+90s 起）：各资产相对本窗口开盘价的涨跌幅，>move_pct 记为「跟涨」，
   <-move_pct 记为「跟跌」；同向数量达到 `min_consensus`（默认 4/6）才触发。
2) 落后者（官网口径）：用「隐含概率差」（laggard gap）衡量——落后者在共识方向上的
   Polymarket 盘口价，要比「跟风队伍」的平均盘口价至少低 `laggard_gap`（默认 0.15），
   才算真正掉队、值得一博。完全同步（无人掉队）时不开仓。
3) 入场价带：选出落后者的盘口价必须落在 `[entry_min,entry_max]`（默认 0.38–0.68），
   确保买到的是接近五五开、还有上行空间的那一侧。
4) BTC 反转过滤：过去 1 小时 BTC 涨/跌 ≥ `btc_reversion_pct`（默认 2%）时，放松
   逆向一侧的共识数量要求（`reversion_relax`，默认 1），博弈超涨/超跌回归。

DCA 是逐笔状态机，放在 `dca_add()` 里；实盘引擎按它决定每次加仓金额。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AssetSnapshot:
    """某个资产在本轮某一时刻的状态。"""

    symbol: str
    delta_pct: float
    up_price: float | None = None
    down_price: float | None = None

    def price_for(self, side: str) -> float | None:
        return self.up_price if side == "Up" else self.down_price


@dataclass(frozen=True)
class ConstellationSignal:
    target: str | None
    side: str | None
    consensus: str | None
    up_count: int
    down_count: int
    laggard_delta: float | None = None
    entry_price: float | None = None
    reason: str = ""


@dataclass(frozen=True)
class ConstellationParams:
    move_pct: float = 0.03
    # 队伍平均涨幅门槛（近似官网 UP/DOWN threshold）；<=0 关闭该额外过滤
    avg_move_pct: float = 0.0
    laggard_gap: float = 0.15
    min_consensus: int = 4
    btc_reversion_pct: float = 2.0
    reversion_relax: int = 1
    entry_min: float = 0.38
    entry_max: float = 0.68


class ConstellationStrategy:
    name = "constellation"

    def __init__(self, params: ConstellationParams | None = None) -> None:
        self.p = params or ConstellationParams()

    def required_counts(self, btc_hour_pct: float | None) -> tuple[int, int]:
        """返回 (Up 所需共识数, Down 所需共识数)，含 BTC 反转放松。"""
        up_req = down_req = self.p.min_consensus
        if btc_hour_pct is not None:
            if btc_hour_pct >= self.p.btc_reversion_pct:
                down_req = max(1, down_req - self.p.reversion_relax)
            elif btc_hour_pct <= -self.p.btc_reversion_pct:
                up_req = max(1, up_req - self.p.reversion_relax)
        return up_req, down_req

    def avg_move(self, snaps: list[AssetSnapshot], direction: str) -> float:
        """「跟风队伍」的平均涨幅（绝对百分比）。

        近似官网 Constellation 的 UP/DOWN threshold —— 官网门槛对比的是
        跟风资产的**平均**涨幅，而不是逐个资产。无跟风资产时返回 0。
        """
        if direction == "Up":
            moves = [s.delta_pct for s in snaps if s.delta_pct > self.p.move_pct]
        else:
            moves = [-s.delta_pct for s in snaps if s.delta_pct < -self.p.move_pct]
        return sum(moves) / len(moves) if moves else 0.0

    def consensus(
        self, snaps: list[AssetSnapshot], btc_hour_pct: float | None = None
    ) -> tuple[str | None, int, int]:
        """返回 (方向, 跟涨数, 跟跌数)；方向为 None 表示未达共识。"""
        up_count = sum(1 for s in snaps if s.delta_pct > self.p.move_pct)
        down_count = sum(1 for s in snaps if s.delta_pct < -self.p.move_pct)
        up_req, down_req = self.required_counts(btc_hour_pct)
        up_ok = up_count >= up_req
        down_ok = down_count >= down_req
        if self.p.avg_move_pct > 0:
            if up_ok and self.avg_move(snaps, "Up") < self.p.avg_move_pct:
                up_ok = False
            if down_ok and self.avg_move(snaps, "Down") < self.p.avg_move_pct:
                down_ok = False
        if not up_ok and not down_ok:
            return None, up_count, down_count
        if up_ok and down_ok:
            return ("Up" if up_count >= down_count else "Down"), up_count, down_count
        return ("Up" if up_ok else "Down"), up_count, down_count

    def pick_laggard(
        self, snaps: list[AssetSnapshot], direction: str
    ) -> AssetSnapshot | None:
        """按官网「隐含概率差」挑落后者。

        队伍（leaders）= 与共识同向的资产；它们对应方向的盘口价均值即「队伍价位」。
        落后者 = 未跟上的资产里，盘口价比队伍价位低至少 `laggard_gap` 的那一个
        （取盘口价最低者）。无人掉队 / 缺少价格时返回 None。
        """
        if direction == "Up":
            leaders = [s for s in snaps if s.delta_pct > self.p.move_pct]
            laggards = [s for s in snaps if s.delta_pct <= self.p.move_pct]
        else:
            leaders = [s for s in snaps if s.delta_pct < -self.p.move_pct]
            laggards = [s for s in snaps if s.delta_pct >= -self.p.move_pct]

        leader_prices = [
            p for s in leaders if (p := s.price_for(direction)) is not None
        ]
        if not leader_prices:
            return None
        pack_price = sum(leader_prices) / len(leader_prices)

        candidates = [
            s
            for s in laggards
            if (p := s.price_for(direction)) is not None
            and pack_price - p >= self.p.laggard_gap
        ]
        if not candidates:
            return None
        return min(
            candidates, key=lambda s: (s.price_for(direction), abs(s.delta_pct))
        )

    def in_band(self, price: float | None) -> bool:
        return price is not None and self.p.entry_min <= price <= self.p.entry_max

    def decide(
        self,
        snaps: list[AssetSnapshot],
        btc_hour_pct: float | None = None,
    ) -> ConstellationSignal:
        direction, up_count, down_count = self.consensus(snaps, btc_hour_pct)
        if direction is None:
            return ConstellationSignal(
                None, None, None, up_count, down_count, reason="no_consensus"
            )

        laggard = self.pick_laggard(snaps, direction)
        if laggard is None:
            return ConstellationSignal(
                None, None, direction, up_count, down_count, reason="no_laggard"
            )

        price = laggard.price_for(direction)
        if price is None:
            return ConstellationSignal(
                laggard.symbol, None, direction, up_count, down_count,
                laggard.delta_pct, reason="no_price",
            )
        if not (self.p.entry_min <= price <= self.p.entry_max):
            return ConstellationSignal(
                laggard.symbol, None, direction, up_count, down_count,
                laggard.delta_pct, price, reason="price_band",
            )
        return ConstellationSignal(
            laggard.symbol,
            direction,
            direction,
            up_count,
            down_count,
            laggard.delta_pct,
            price,
            reason="signal",
        )

    def describe(self) -> str:
        p = self.p
        avg = f"avg_move>={p.avg_move_pct:g}%, " if p.avg_move_pct > 0 else ""
        return (
            f"constellation(consensus>={p.min_consensus}, move>{p.move_pct:g}%, "
            f"{avg}laggard_gap>={p.laggard_gap:g}, "
            f"band={p.entry_min:g}-{p.entry_max:g}, "
            f"btc_revert={p.btc_reversion_pct:g}%)"
        )


@dataclass(frozen=True)
class DcaStep:
    level: int
    amount: float


def dca_add(
    entry_price: float,
    current_price: float,
    spent: float,
    levels: set[int],
    max_usd: float,
    step: float,
    add2: float,
    add3: float,
) -> DcaStep | None:
    """三步入场：首笔在外，价格每下滑 `step` 触发一次加仓。

    第 1 次下滑加 `add2`，第 2 次下滑加 `add3`，累计不超过 `max_usd`。
    返回 None 表示本次不加仓。
    """
    if spent >= max_usd:
        return None
    slide = entry_price - current_price
    amount = 0.0
    level = 0
    if 1 not in levels and slide >= step:
        level, amount = 1, add2
    elif 2 not in levels and slide >= 2 * step:
        level, amount = 2, add3
    if level == 0:
        return None
    amount = min(amount, max_usd - spent)
    if amount <= 0:
        return None
    return DcaStep(level, amount)
