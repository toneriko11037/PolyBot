"""Gamma API helpers: deterministically locate the live Up/Down market."""

from __future__ import annotations

import datetime as _dt
import json
import time
from dataclasses import dataclass

import requests

import config


@dataclass
class Market:
    slug: str
    title: str
    window_start: int
    end_ts: float
    condition_id: str
    up_token: str
    down_token: str
    tick_size: str
    min_size: float
    neg_risk: bool
    accepting: bool

    def token_for(self, side: str) -> str:
        return self.up_token if side.lower() == "up" else self.down_token


def window_start(now: float | None = None) -> int:
    now = time.time() if now is None else now
    duration = config.window_seconds()
    return int(now // duration) * duration


def build_slug(asset: str, timeframe: str, start: int) -> str:
    return f"{asset.lower()}-updown-{timeframe.lower()}-{start}"


def _parse_iso(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return _dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _map_outcome_tokens(outcomes: list[str], token_ids: list[str]) -> tuple[str, str]:
    up = down = None
    for label, token in zip(outcomes, token_ids):
        low = str(label).strip().lower()
        if low == "up":
            up = token
        elif low == "down":
            down = token
    if up is None or down is None:
        # fall back to documented ordering: Up = index 0, Down = index 1
        up, down = token_ids[0], token_ids[1]
    return str(up), str(down)


def fetch_market(
    start: int | None = None,
    asset: str | None = None,
    timeframe: str | None = None,
    timeout: float = 10.0,
) -> Market | None:
    """Fetch the market for the given window start (default: current window).

    `asset` / `timeframe` default to the single-asset config so existing callers
    are unaffected; the multi-asset engine passes them explicitly.
    """
    start = window_start() if start is None else start
    asset = config.ASSET if asset is None else asset
    timeframe = config.TIMEFRAME if timeframe is None else timeframe
    slug = build_slug(asset, timeframe, start)
    url = f"{config.GAMMA_HOST}/events"
    try:
        resp = requests.get(url, params={"slug": slug}, timeout=timeout)
        resp.raise_for_status()
        events = resp.json()
    except requests.RequestException:
        return None

    if not events:
        return None

    event = events[0]
    markets = event.get("markets") or []
    if not markets:
        return None

    raw = markets[0]
    try:
        outcomes = json.loads(raw.get("outcomes") or "[]")
        token_ids = json.loads(raw.get("clobTokenIds") or "[]")
    except (ValueError, TypeError):
        return None

    if len(token_ids) < 2:
        return None

    up_token, down_token = _map_outcome_tokens(outcomes, token_ids)
    end_ts = _parse_iso(raw.get("endDate")) or (start + config.window_seconds())

    return Market(
        slug=slug,
        title=event.get("title") or raw.get("question") or slug,
        window_start=start,
        end_ts=float(end_ts),
        condition_id=str(raw.get("conditionId") or ""),
        up_token=up_token,
        down_token=down_token,
        tick_size=str(raw.get("orderPriceMinTickSize") or "0.01"),
        min_size=float(raw.get("orderMinSize") or 5),
        neg_risk=bool(raw.get("negRisk") or False),
        accepting=bool(raw.get("acceptingOrders", True)),
    )


def fetch_outcome(
    start: int,
    asset: str | None = None,
    timeframe: str | None = None,
    timeout: float = 10.0,
) -> bool | None:
    """已结算窗口的 Up 是否获胜；未结算或读取失败返回 None。"""
    asset = config.ASSET if asset is None else asset
    timeframe = config.TIMEFRAME if timeframe is None else timeframe
    slug = build_slug(asset, timeframe, start)
    try:
        resp = requests.get(
            f"{config.GAMMA_HOST}/events", params={"slug": slug}, timeout=timeout
        )
        resp.raise_for_status()
        events = resp.json()
    except requests.RequestException:
        return None
    if not events:
        return None
    markets = events[0].get("markets") or []
    if not markets:
        return None
    raw = markets[0]
    if not raw.get("closed"):
        return None
    try:
        outcomes = json.loads(raw.get("outcomes") or "[]")
        prices = json.loads(raw.get("outcomePrices") or "[]")
    except (ValueError, TypeError):
        return None
    for label, price in zip(outcomes, prices):
        if str(label).strip().lower() == "up":
            try:
                return float(price) >= 0.5
            except (TypeError, ValueError):
                return None
    return None
