"""Idempotently patch py_order_utils to accept Polymarket signature type 3 (POLY_1271).

The published py-order-utils (0.3.2) only allows signature types 0/1/2, so orders
for Polymarket deposit wallets (SIGNATURE_TYPE=3) fail locally with
"Invalid order inputs" before ever reaching the exchange. This script adds
POLY_1271=3 support. Safe to run repeatedly.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

SUPPORTED = 3


def _package_dir(name: str) -> Path:
    spec = importlib.util.find_spec(name)
    if spec is None or spec.origin is None:
        raise SystemExit(
            f"[patch] 未找到依赖 {name}，请先运行 pip install -r requirements.txt"
        )
    return Path(spec.origin).parent


def _patch_signatures(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    if "POLY_1271" in text:
        return False
    text = text.rstrip() + (
        "\n\n# EIP1271 signatures signed by EOAs that own Polymarket deposit wallets"
        "\nPOLY_1271 = 3\n"
    )
    path.write_text(text, encoding="utf-8")
    return True


def _patch_order_builder(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    if "POLY_1271" in text:
        return False
    text = text.replace(
        "from ..model.signatures import EOA, POLY_GNOSIS_SAFE, POLY_PROXY",
        "from ..model.signatures import EOA, POLY_GNOSIS_SAFE, POLY_PROXY, POLY_1271",
    )
    text = text.replace(
        "[EOA, POLY_GNOSIS_SAFE, POLY_PROXY]",
        "[EOA, POLY_GNOSIS_SAFE, POLY_PROXY, POLY_1271]",
    )
    path.write_text(text, encoding="utf-8")
    return True


def _patch_model_init(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    if "POLY_1271" in text:
        return False
    text = text.replace(
        "from py_order_utils.model.signatures import EOA, POLY_PROXY, POLY_GNOSIS_SAFE",
        "from py_order_utils.model.signatures import "
        "EOA, POLY_PROXY, POLY_GNOSIS_SAFE, POLY_1271",
    )
    path.write_text(text, encoding="utf-8")
    return True


def main() -> int:
    pkg = _package_dir("py_order_utils")
    targets = [
        ("model/signatures.py", _patch_signatures),
        ("builders/order_builder.py", _patch_order_builder),
        ("model/__init__.py", _patch_model_init),
    ]
    for rel, func in targets:
        path = pkg / rel
        if not path.exists():
            raise SystemExit(f"[patch] 找不到文件: {path}")
        if func(path):
            print(f"[patch] 已修改 {rel} -> 支持 signatureType={SUPPORTED}")
        else:
            print(f"[patch] {rel} 已是最新，跳过")
    print("[patch] signatureType=3 (POLY_1271) 支持已就绪")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
