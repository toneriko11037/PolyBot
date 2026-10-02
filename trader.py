"""Polymarket CLOB client wrapper: wallet detection, auth and market buys."""

from __future__ import annotations

import logging

import requests

import config

log = logging.getLogger("trader")


def derive_address(private_key: str) -> str:
    from eth_account import Account

    key = private_key if private_key.startswith("0x") else "0x" + private_key
    return Account.from_key(key).address


def _rpc(method: str, params: list) -> str | None:
    try:
        resp = requests.post(
            config.POLYGON_RPC,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("result")
    except Exception as exc:  # noqa: BLE001
        log.warning("Polygon RPC 调用失败 (%s): %s", method, exc)
        return None


def detect_signature_type(private_key: str, funder: str) -> int:
    """Best-effort on-chain wallet type detection (0/1/2)."""
    eoa = derive_address(private_key)
    if not funder or funder.lower() == eoa.lower():
        return 0

    code = _rpc("eth_getCode", [funder, "latest"])
    if not code or code in ("0x", "0x0"):
        raise ValueError(
            f"FUNDER_ADDRESS={funder} 在 Polygon 上没有合约代码，可能是错误地址"
        )

    owners = _rpc("eth_call", [{"to": funder, "data": "0xa0e67e2b"}, "latest"])
    if owners and owners not in ("0x", "0x0"):
        return 2  # Gnosis Safe
    return 1  # Polymarket proxy（若为新版 deposit wallet，请手动设 3）


def resolve_signature_type(private_key: str, funder: str) -> int:
    raw = config.SIGNATURE_TYPE
    if raw == "auto":
        detected = detect_signature_type(private_key, funder) if private_key else 0
        log.info("SIGNATURE_TYPE=auto -> 检测为 %s", detected)
        return detected
    return int(raw)


def make_client():
    from py_clob_client_v2 import ClobClient

    if not config.PRIVATE_KEY:
        raise ValueError("缺少 PRIVATE_KEY，无法建立交易客户端")

    funder = config.FUNDER_ADDRESS or derive_address(config.PRIVATE_KEY)
    signature_type = resolve_signature_type(config.PRIVATE_KEY, funder)

    client = ClobClient(
        config.CLOB_HOST,
        chain_id=config.CHAIN_ID,
        key=config.PRIVATE_KEY,
        signature_type=signature_type,
        funder=funder,
    )
    client.set_api_creds(client.create_or_derive_api_key())
    log.info(
        "CLOB 客户端就绪 | funder=%s signature_type=%s", funder, signature_type
    )
    return client


def market_buy(
    client,
    token_id: str,
    amount_usd: float,
    tick_size: str | None = None,
    neg_risk: bool | None = None,
) -> dict:
    """Immediate market buy by USD amount, Fill-Or-Kill."""
    from py_clob_client_v2 import (
        MarketOrderArgs,
        OrderType,
        PartialCreateOrderOptions,
        Side,
    )

    args = MarketOrderArgs(token_id=token_id, amount=float(amount_usd), side=Side.BUY)
    options = None
    if tick_size is not None:
        options = PartialCreateOrderOptions(
            tick_size=str(tick_size), neg_risk=bool(neg_risk)
        )
    resp = client.create_and_post_market_order(
        order_args=args, options=options, order_type=OrderType.FOK
    )
    _check_order_response(resp)
    return resp


def _check_order_response(resp) -> None:
    """Treat an HTTP-200 body carrying an error as a failure so it can be retried."""
    if not isinstance(resp, dict):
        return
    if resp.get("success") is False or resp.get("errorMsg"):
        raise RuntimeError(resp.get("errorMsg") or resp.get("error") or str(resp))


def account_summary(client) -> str:
    """Return a human-readable balance/allowance summary (best effort)."""
    lines: list[str] = []
    try:
        from py_clob_client_v2 import BalanceAllowanceParams, AssetType

        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        info = client.get_balance_allowance(params)
        lines.append(f"COLLATERAL: {info}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"无法读取余额/授权: {exc}")
    return "\n".join(lines)
