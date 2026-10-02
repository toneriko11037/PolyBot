"""Configuration loader: reads .env and exposes typed settings."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

TIMEFRAME_SECONDS = {
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "24h": 86400,
}

CHAINLINK_SYMBOLS = {
    "btc": "btc/usd",
    "eth": "eth/usd",
    "sol": "sol/usd",
    "xrp": "xrp/usd",
    "doge": "doge/usd",
    "bnb": "bnb/usd",
}

BINANCE_SYMBOLS = {
    "btc": "btcusdt",
    "eth": "ethusdt",
    "sol": "solusdt",
    "xrp": "xrpusdt",
    "doge": "dogeusdt",
    "bnb": "bnbusdt",
}


def _str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None else value.strip()


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "y", "on")


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return int(float(value))


def _float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return float(value)


def _optional_float(name: str) -> float | None:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return None
    return float(value)


# --- wallet / auth ----------------------------------------------------------
PRIVATE_KEY = _str("PRIVATE_KEY")
FUNDER_ADDRESS = _str("FUNDER_ADDRESS")
SIGNATURE_TYPE = _str("SIGNATURE_TYPE", "auto").lower()

# --- trading ----------------------------------------------------------------
DRY_RUN = _bool("DRY_RUN", True)

# --- order retry ------------------------------------------------------------
# 下单失败后（网络抖动 / 盘口暂时无卖单等）重新检查条件并重试。
ORDER_RETRY_MAX_ATTEMPTS = _int("ORDER_RETRY_MAX_ATTEMPTS", 5)
ORDER_RETRY_INTERVAL_SECONDS = _float("ORDER_RETRY_INTERVAL_SECONDS", 1.0)

# --- market -----------------------------------------------------------------
ASSET = _str("ASSET", "btc").lower()
TIMEFRAME = _str("TIMEFRAME", "5m").lower()

# --- price source -----------------------------------------------------------
# "chainlink_twap" 才是这些 Up/Down 市场实际使用的结算源（Chainlink BTC/USD TWAP）
PRICE_SOURCE = _str("PRICE_SOURCE", "chainlink_twap").lower()
TWAP_WINDOW_SECONDS = _int("TWAP_WINDOW_SECONDS", 60)

# --- target -----------------------------------------------------------------
TARGET_MODE = _str("TARGET_MODE", "window_open").lower()
MANUAL_TARGET = _optional_float("MANUAL_TARGET")

# --- infra ------------------------------------------------------------------
CLOB_HOST = _str("CLOB_HOST", "https://clob.polymarket.com")
GAMMA_HOST = _str("GAMMA_HOST", "https://gamma-api.polymarket.com")
RTDS_URL = _str("RTDS_URL", "wss://ws-live-data.polymarket.com")
CHAIN_ID = _int("CHAIN_ID", 137)
POLYGON_RPC = _str("POLYGON_RPC", "https://polygon-rpc.com")
LOG_DIR = _str("LOG_DIR", "logs")
LOG_LEVEL = _str("LOG_LEVEL", "INFO").upper()

# --- target capture ---------------------------------------------------------
TARGET_TOLERANCE_MS = _int("TARGET_TOLERANCE_MS", 5000)

# --- price feed watchdog ----------------------------------------------------
# RTDS 连接可能「假活」：连接没断、PING 有回应，但不再推送某标的的行情。
# 超过该秒数没有新行情就强制重连，避免目标价一直「待定」。
FEED_STALL_TIMEOUT_SECONDS = _float("FEED_STALL_TIMEOUT_SECONDS", 30.0)
FEED_WATCHDOG_INTERVAL_SECONDS = _float("FEED_WATCHDOG_INTERVAL_SECONDS", 10.0)


def window_seconds() -> int:
    if TIMEFRAME not in TIMEFRAME_SECONDS:
        raise ValueError(
            f"不支持的 TIMEFRAME={TIMEFRAME!r}，可选: {', '.join(TIMEFRAME_SECONDS)}"
        )
    return TIMEFRAME_SECONDS[TIMEFRAME]


def feed_symbol_for(asset: str, source: str | None = None) -> str:
    """返回某资产在指定价格源下的 feed 符号（多资产引擎使用）。"""
    source = PRICE_SOURCE if source is None else source
    table = BINANCE_SYMBOLS if source == "binance" else CHAINLINK_SYMBOLS
    asset = asset.lower()
    if asset not in table:
        raise ValueError(f"ASSET={asset!r} 在 {source} 源中没有对应符号")
    return table[asset]


def feed_symbol() -> str:
    return feed_symbol_for(ASSET)


def validate() -> None:
    if PRICE_SOURCE not in ("chainlink", "chainlink_twap", "binance"):
        raise ValueError("PRICE_SOURCE 必须是 chainlink、chainlink_twap 或 binance")
    if TARGET_MODE not in ("window_open", "manual"):
        raise ValueError("TARGET_MODE 必须是 window_open 或 manual")
    if TARGET_MODE == "manual" and MANUAL_TARGET is None:
        raise ValueError("TARGET_MODE=manual 时必须设置 MANUAL_TARGET")
    if ORDER_RETRY_MAX_ATTEMPTS < 1:
        raise ValueError("ORDER_RETRY_MAX_ATTEMPTS 至少为 1")
    if ORDER_RETRY_INTERVAL_SECONDS < 0:
        raise ValueError("ORDER_RETRY_INTERVAL_SECONDS 不能为负")
    window_seconds()  # validates TIMEFRAME
    feed_symbol()  # validates ASSET / PRICE_SOURCE
    if not DRY_RUN and not PRIVATE_KEY:
        raise ValueError("非 DRY_RUN 模式必须设置 PRIVATE_KEY")
