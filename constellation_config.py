"""Constellation 引擎配置：全部通过 `.env` 覆盖，默认值取自 KumiBot 官网公开规则。

导入 `config` 会触发 `.env` 载入，因此这里直接用 `os.getenv` 读取即可。
"""

from __future__ import annotations

import datetime as _dt
import os

import config
from constellation_strategy import ConstellationParams

# --- 标的 -------------------------------------------------------------------
ASSETS = [
    a.strip().lower()
    for a in os.getenv("CONSTELLATION_ASSETS", "btc,eth,sol,xrp,doge,bnb").split(",")
    if a.strip()
]

# --- 信号参数 ---------------------------------------------------------------
MOVE_PCT = float(os.getenv("CONSTELLATION_MOVE_PCT", "0.03"))
# 队伍平均涨幅门槛（近似官网 UP/DOWN threshold）；0 = 关闭（仅用逐资产 MOVE_PCT）
AVG_MOVE_PCT = float(os.getenv("CONSTELLATION_MIN_AVG_MOVE_PCT", "0"))
# 「隐含概率差」：落后者盘口价要比队伍均价低多少才算真落后（官网 laggard gap）
LAGGARD_GAP = float(os.getenv("CONSTELLATION_LAGGARD_GAP", "0.15"))
MIN_CONSENSUS = int(float(os.getenv("CONSTELLATION_MIN_CONSENSUS", "4")))
BTC_REVERSION_PCT = float(os.getenv("CONSTELLATION_BTC_REVERSION_PCT", "2.0"))
REVERSION_RELAX = int(float(os.getenv("CONSTELLATION_REVERSION_RELAX", "1")))
ENTRY_MIN = float(os.getenv("CONSTELLATION_ENTRY_MIN", "0.38"))
ENTRY_MAX = float(os.getenv("CONSTELLATION_ENTRY_MAX", "0.68"))
# 共识扫描在本窗口开始后多少秒启动（官网 T+90s）
SCAN_AFTER_SECONDS = int(float(os.getenv("CONSTELLATION_SCAN_AFTER_SECONDS", "90")))

# --- DCA（三步入场） ---------------------------------------------------------
DCA_START_USD = float(os.getenv("CONSTELLATION_DCA_START_USD", "5.0"))
DCA_STEP = float(os.getenv("CONSTELLATION_DCA_STEP", "0.09"))
DCA_ADD2_USD = float(os.getenv("CONSTELLATION_DCA_ADD2_USD", "3.0"))
DCA_ADD3_USD = float(os.getenv("CONSTELLATION_DCA_ADD3_USD", "2.5"))
DCA_MAX_USD = float(os.getenv("CONSTELLATION_DCA_MAX_USD", "10.5"))
DCA_STOP_BEFORE_CLOSE = int(float(os.getenv("CONSTELLATION_DCA_STOP_BEFORE_CLOSE", "110")))

# --- 风险上限 ---------------------------------------------------------------
MAX_POSITIONS = int(float(os.getenv("CONSTELLATION_MAX_POSITIONS", "3")))
MAX_EXPOSURE_USD = float(os.getenv("CONSTELLATION_MAX_EXPOSURE_USD", "31.5"))

# --- 交易时段（UTC），例如 "9-17" 或 "0-8,13-23"；留空 = 全天 ---------------
TRADING_HOURS_UTC = os.getenv("CONSTELLATION_TRADING_HOURS_UTC", "").strip()

# --- 运行参数（与单资产 bot 共用） -------------------------------------------
DRY_RUN = config.DRY_RUN
ORDER_RETRY_MAX_ATTEMPTS = config.ORDER_RETRY_MAX_ATTEMPTS
ORDER_RETRY_INTERVAL_SECONDS = config.ORDER_RETRY_INTERVAL_SECONDS
TARGET_TOLERANCE_MS = config.TARGET_TOLERANCE_MS
LOG_DIR = config.LOG_DIR
LOG_LEVEL = config.LOG_LEVEL


def to_params() -> ConstellationParams:
    return ConstellationParams(
        move_pct=MOVE_PCT,
        avg_move_pct=AVG_MOVE_PCT,
        laggard_gap=LAGGARD_GAP,
        min_consensus=MIN_CONSENSUS,
        btc_reversion_pct=BTC_REVERSION_PCT,
        reversion_relax=REVERSION_RELAX,
        entry_min=ENTRY_MIN,
        entry_max=ENTRY_MAX,
    )


def _parse_hours(raw: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            lo_s, hi_s = chunk.split("-", 1)
            ranges.append((int(lo_s), int(hi_s)))
        else:
            h = int(chunk)
            ranges.append((h, h + 1))
    return ranges


def in_trading_hours(ts: float | None = None) -> bool:
    """当前 UTC 小时是否在允许的交易时段内；未配置则全天允许。"""
    if not TRADING_HOURS_UTC:
        return True
    ts = _dt.datetime.now(_dt.timezone.utc).timestamp() if ts is None else ts
    hour = _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).hour
    for lo, hi in _parse_hours(TRADING_HOURS_UTC):
        if lo <= hi:
            if lo <= hour < hi:
                return True
        elif hour >= lo or hour < hi:  # 跨午夜
            return True
    return False


def validate() -> None:
    if not ASSETS:
        raise ValueError("CONSTELLATION_ASSETS 不能为空")
    for asset in ASSETS:
        config.feed_symbol_for(asset, config.PRICE_SOURCE)  # 校验符号存在
    if MIN_CONSENSUS < 1 or MIN_CONSENSUS > len(ASSETS):
        raise ValueError("CONSTELLATION_MIN_CONSENSUS 必须在 1 与资产数之间")
    if not (0 < ENTRY_MIN < ENTRY_MAX < 1):
        raise ValueError("入场价带需满足 0 < ENTRY_MIN < ENTRY_MAX < 1")
    if not (0 <= LAGGARD_GAP < 1):
        raise ValueError("CONSTELLATION_LAGGARD_GAP 需满足 0 <= gap < 1")
    if AVG_MOVE_PCT < 0:
        raise ValueError("CONSTELLATION_MIN_AVG_MOVE_PCT 不能为负")
    if DCA_START_USD <= 0 or DCA_MAX_USD < DCA_START_USD:
        raise ValueError("DCA 金额非法：需 0 < DCA_START_USD <= DCA_MAX_USD")
    if DCA_STEP <= 0:
        raise ValueError("CONSTELLATION_DCA_STEP 必须大于 0")
    if DCA_STOP_BEFORE_CLOSE < 0 or SCAN_AFTER_SECONDS < 0:
        raise ValueError("时间参数不能为负")
    if MAX_POSITIONS < 1 or MAX_EXPOSURE_USD <= 0:
        raise ValueError("风险上限参数非法")
