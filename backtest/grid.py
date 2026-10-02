"""参数网格展开：把用 `|` 分隔的参数值展开成多个组合。

Constellation 回测用，例如 `min_consensus=4|5,laggard_gap=0.15`。
"""

from __future__ import annotations

import itertools


def expand_grid(params: dict) -> list[dict]:
    """把用 `|` 或 `,` 分隔的参数值展开成多个参数组合。"""
    keys = list(params.keys())
    values = []
    for key in keys:
        raw = params[key]
        if isinstance(raw, str) and "|" in raw:
            values.append([v.strip() for v in raw.split("|")])
        elif isinstance(raw, str) and "," in raw:
            values.append([v.strip() for v in raw.split(",")])
        else:
            values.append([raw])
    combos = []
    for combo in itertools.product(*values):
        combo_dict = dict(zip(keys, combo))
        combo_dict = {k: _coerce(v) for k, v in combo_dict.items()}
        combos.append(combo_dict)
    return combos


def _coerce(value):
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            try:
                return float(value)
            except ValueError:
                return value
    return value
