"""CLOB 盘口价读取：用于 Constellation 的入场价格带与隐含概率差判断。

Polymarket CLOB 公开的 `/midpoint` 端点无需鉴权，返回 token 的中间价。

一次扫描会对多个 token 并发请求，因此这里按线程复用 `requests.Session`：
同一线程后续请求复用已建立的 TCP/TLS 连接，省掉重复握手。
`Session` 并非线程安全，故用 thread-local 存放，每个工作线程各持一个。
"""

from __future__ import annotations

import logging
import threading

import requests

import config

log = logging.getLogger("clob")

_local = threading.local()


def _session() -> requests.Session:
    """返回当前线程专属的 Session（惰性创建，连接可复用）。"""
    session = getattr(_local, "session", None)
    if session is None:
        session = requests.Session()
        _local.session = session
    return session


def midpoint(token_id: str, timeout: float = 5.0) -> float | None:
    """返回 token 的盘口中间价（0~1）；不可用时返回 None。"""
    if not token_id:
        return None
    url = f"{config.CLOB_HOST}/midpoint"
    params = {"token_id": token_id}
    price: float | None = None
    for attempt in (1, 2):
        try:
            resp = _session().get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            raw = resp.json().get("mid")
            price = float(raw) if raw is not None else None
            break
        except requests.exceptions.ConnectionError as exc:
            # 连接池里的旧连接可能已被服务端关闭：丢弃并重试一次（超时不重试，避免拖慢）
            if attempt == 1 and not isinstance(exc, requests.exceptions.Timeout):
                _local.session = None
                continue
            log.debug("midpoint 读取失败 (%s): %s", token_id[:12], exc)
            return None
        except (requests.RequestException, ValueError, TypeError) as exc:
            log.debug("midpoint 读取失败 (%s): %s", token_id[:12], exc)
            return None
    if price is None or not (0.0 < price < 1.0):
        return None
    return price
