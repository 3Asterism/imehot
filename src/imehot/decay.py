"""衰减与打分。

刻意**不预计算**衰减：账本只存原始事实（出生/消失时刻、复活次数），
分数在查询时按当前参数现算。这样调整半衰期只是改一个数字，不会污染历史数据。

模型（参考三国开源实现）：

====================  ====================================================
librime               ``UserDbValue{commits, dee, tick}``，
                      ``d_new = d0 + d_old * exp((t_old - t_new) / 200)``
青简 Qingjian         90 天半衰，**冷却只影响排序，记录永存**
WindInput             三层：24h 草稿 → 5000 条临时 → 永久用户词库
====================  ====================================================

我们取三者的交集：指数衰减 + 半衰期可配 + 原始记录永不丢失。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .config import DEFAULT_HALF_LIFE_DAYS

SECONDS_PER_DAY = 86_400.0


@dataclass(frozen=True)
class Score:
    """一个词条的打分明细，便于在 UI/CLI 里解释「为什么它排这么后」。"""

    value: float
    idle_days: float
    reason: str


def half_life_factor(idle_days: float, half_life_days: float = DEFAULT_HALF_LIFE_DAYS) -> float:
    """指数衰减系数：每过一个半衰期减半。"""
    if half_life_days <= 0:
        return 0.0
    return float(0.5 ** (max(0.0, idle_days) / half_life_days))


def score_row(
    row: sqlite3.Row,
    now: int,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> Score:
    """按行内事实计算分数。

    * ``active``：原生输入法仍然保留它 —— 说明它是活的，不衰减。
    * ``dropped``：原生已经把它删了 —— 从删除时刻开始按半衰期衰减。
    * ``archived``：已归档，不计分。
    """
    strength = float(row["strength"] or 1.0)
    state = row["state"]

    if state == "archived":
        return Score(0.0, 0.0, "已归档")
    if state == "active":
        return Score(strength, 0.0, "原生词库仍在，不衰减")

    dropped_at = row["dropped_at"] or row["last_evidence"] or now
    idle_days = max(0.0, (now - int(dropped_at)) / SECONDS_PER_DAY)
    value = strength * half_life_factor(idle_days, half_life_days)
    return Score(value, idle_days, f"原生已删除，闲置 {idle_days:.1f} 天")


def idle_days(row: sqlite3.Row, now: int) -> float:
    ref = row["dropped_at"] or row["last_evidence"] or now
    return max(0.0, (now - int(ref)) / SECONDS_PER_DAY)


def label(value: float) -> str:
    """把分数映射成人类可读的热度档位。"""
    if value >= 2.0:
        return "🔥🔥"
    if value >= 1.0:
        return "🔥"
    if value >= 0.25:
        return "·"
    return "❄"
