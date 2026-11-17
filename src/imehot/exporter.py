"""导出：把账本变成可迁移的资产。

支持四种格式，覆盖不同的下游消费者：

``tsv``    人读 + 脚本处理
``json``   完整字段，便于二次开发
``plist``  原生输入法「编辑自定义短语」可直接拖入的格式
``rime``   供 RIME / 雾凇等方案导入的码表（``pinyin<TAB>surface<TAB>weight``）

没有任何格式包含按键记录——我们本来就没有按键记录。
"""

from __future__ import annotations

import json
import os
import plistlib
import sqlite3
import sys
from pathlib import Path
from typing import Any

from . import decay
from .config import DEFAULT_HALF_LIFE_DAYS
from .ledger import effective_confidence
from .native import split_pinyin

FORMATS = ("tsv", "json", "plist", "rime")
_TSV_HEADER = (
    "surface",
    "pinyin",
    "key",
    "state",
    "tier",
    "source",
    "confidence",
    "suspect_of",
    "born_at",
    "idle_days",
    "score",
)


def _record(row: sqlite3.Row, now: int, half_life: float) -> dict[str, Any]:
    sc = decay.score_row(row, now, half_life)
    return {
        "surface": row["surface"],
        "pinyin": row["pinyin"],
        "key": row["pinyin"],
        "spaced_pinyin": split_pinyin(row["pinyin"], row["syllables"] or ""),
        "state": row["state"],
        "tier": row["tier"],
        "source": row["source"],
        # 置信度是派生视图，导出时带上，方便用户在别的机器上复核为什么某条被标记
        "confidence": effective_confidence(row, now),
        "suspect_of": row["suspect_of"],
        "token_id": row["token_id"],
        "born_at": row["born_at"],
        "dropped_at": row["dropped_at"],
        "revivals": row["revivals"],
        "seen_runs": row["seen_runs"],
        "idle_days": round(sc.idle_days, 2),
        "score": round(sc.value, 4),
    }


def render(
    rows: list[sqlite3.Row],
    fmt: str,
    now: int,
    half_life: float = DEFAULT_HALF_LIFE_DAYS,
) -> bytes:
    if fmt not in FORMATS:
        raise ValueError(f"未知格式 {fmt!r}，可选 {', '.join(FORMATS)}")

    records = [_record(r, now, half_life) for r in rows]

    if fmt == "json":
        payload = {
            "generator": "imehot",
            "count": len(records),
            "half_life_days": half_life,
            "words": records,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2).encode() + b"\n"

    if fmt == "tsv":
        lines = ["\t".join(_TSV_HEADER)]
        for rec in records:
            lines.append("\t".join(str(rec[k]) for k in _TSV_HEADER))
        return ("\n".join(lines) + "\n").encode()

    if fmt == "rime":
        out = ["# RIME 用户码表 — 由 imehot 导出", "# pinyin\tsurface\tweight", ""]
        for rec in records:
            weight = max(1, round(rec["score"] * 100))
            out.append(f"{rec['spaced_pinyin']}\t{rec['surface']}\t{weight}")
        return ("\n".join(out) + "\n").encode()

    # plist：原生「编辑自定义短语」可直接拖入
    items = [{"phrase": rec["surface"], "shortcut": rec["key"]} for rec in records]
    return plistlib.dumps(items, fmt=plistlib.FMT_XML)


def export(
    rows: list[sqlite3.Row],
    fmt: str,
    now: int,
    out: Path | None = None,
    half_life: float = DEFAULT_HALF_LIFE_DAYS,
) -> int:
    """写出导出结果，返回词条数。``out`` 为空时写到标准输出。"""
    blob = render(rows, fmt, now, half_life)
    if out is None:
        sys.stdout.buffer.write(blob)
        sys.stdout.buffer.flush()
    else:
        # 原子写：中途失败不会留下半个文件，也就不会让你误以为导出成功。
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(f".{out.name}.tmp.{os.getpid()}")
        try:
            tmp.write_bytes(blob)
            os.replace(tmp, out)
        finally:
            tmp.unlink(missing_ok=True)
    return len(rows)
