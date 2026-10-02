"""回测公共工具：带重试的 HTTP、路径常量、时间解析。"""

from __future__ import annotations

import datetime as _dt
import json
import random
import time
from pathlib import Path

import requests

import config

BASE_DIR = Path(config.BASE_DIR)
DATA_DIR = BASE_DIR / "backtest_data"
MARKETS_DIR = DATA_DIR / "markets"      # markets/<asset>.csv（BTC 兼容旧 markets/<day>.csv）
PRICES_DIR = DATA_DIR / "prices"        # prices/<asset>/<window_start>.json（BTC 兼容旧 prices/<start>.json）
SPOT_DIR = DATA_DIR / "spot"            # spot/<symbol>-1m.csv
BTC_DIR = DATA_DIR / "btc"              # 旧路径，仅读取时兜底

# 各资产的 Polymarket 5m Up/Down series id
SERIES_IDS = {
    "btc": 10684,
    "eth": 10683,
    "xrp": 10685,
    "sol": 10686,
    "doge": 11325,
    "bnb": 11326,
}
ASSETS = tuple(SERIES_IDS)
# Binance 现货符号（用于近似各资产的 Chainlink 结算源）
SPOT_SYMBOLS = {
    "btc": "BTCUSDT",
    "eth": "ETHUSDT",
    "sol": "SOLUSDT",
    "xrp": "XRPUSDT",
    "doge": "DOGEUSDT",
    "bnb": "BNBUSDT",
}

SERIES_ID = SERIES_IDS["btc"]  # 兼容旧引用
WINDOW_SECONDS = 300
FIDELITY_MIN = 1  # prices-history 的 fidelity 单位是分钟

# 各资产数据实际可用的起始日（更早的日期没有市场）
ASSET_START = {
    "btc": "2025-12-17",
    "eth": "2025-12-17",
    "xrp": "2025-12-17",
    "sol": "2025-12-17",
    "doge": "2026-04-01",
    "bnb": "2026-04-01",
}


def markets_path(asset: str) -> Path:
    return MARKETS_DIR / f"{asset.lower()}.csv"


def prices_dir(asset: str) -> Path:
    return PRICES_DIR / asset.lower()


def spot_path(symbol: str, interval: str = "1m") -> Path:
    return SPOT_DIR / f"{symbol.lower()}-{interval}.csv"

_session: requests.Session | None = None


def session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"User-Agent": "btc5m-backtest/1.0"})
    return _session


def get_json(url: str, params: dict | None = None, timeout: float = 25.0,
             retries: int = 6) -> object | None:
    """GET -> JSON，带指数退避重试（CLOB 偶发 SSL EOF）。失败返回 None。"""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            resp = session().get(url, params=params, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (404, 422):
                return None
            last = RuntimeError(f"HTTP {resp.status_code}")
        except Exception as exc:  # noqa: BLE001 - 网络抖动
            last = exc
        time.sleep(min(8.0, 0.5 * (2 ** attempt)) + random.random() * 0.3)
    if last:
        print(f"    [warn] GET {url} 失败: {last}")
    return None


def iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def day_key(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%d")


def ensure_dirs(assets: tuple[str, ...] | list[str] | None = None) -> None:
    for d in (MARKETS_DIR, PRICES_DIR, SPOT_DIR, BTC_DIR):
        d.mkdir(parents=True, exist_ok=True)
    for asset in (assets or ASSETS):
        prices_dir(asset).mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload: object) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)
