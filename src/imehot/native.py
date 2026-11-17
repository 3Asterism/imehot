"""只读访问 macOS 原生简体拼音输入法（SCIM）的学习数据。

原生输入法把「学到了什么」放在两个**可解析**的文件里：

``lexdb.dat``
    标准 SQLite，``Words`` 表保存词面与读音::

        CREATE TABLE Words (
            TokenID               INTEGER PRIMARY KEY,
            Surface               BLOB,   -- UTF-8 词面
            PinyinReading         BLOB,   -- 无调拼音，ASCII
            PinyinSyllableLengths BLOB    -- 每音节字符数，ASCII 数字串
        )

``meta.dat``
    bplist，保存累计上屏量、上次衰减/修剪时间等记账字段。

``dynamic.dat`` 是私有归档格式。已验证其中**不含**可映射到 TokenID 的权重
（头部第 4 个 uint32 恰好等于文件长度，TokenID 在其中几乎不出现），
因此本模块刻意不解析它。决策记录见 ``docs/adr/0001``。

设计约束：本模块**只读**。写入能力在 :mod:`imehot.injector` 里单独隔离。
"""

from __future__ import annotations

import contextlib
import os
import plistlib
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import CF_EPOCH_OFFSET, Paths


class NativeAccessError(RuntimeError):
    """无法读取原生输入法数据（通常是缺少「完全磁盘访问权限」）。"""


@dataclass(frozen=True)
class NativeWord:
    """``lexdb.dat`` 中的一行。"""

    token_id: int
    surface: str
    pinyin: str
    syllables: str

    @property
    def spaced_pinyin(self) -> str:
        """按音节切分后的读音，如 ``hao xiao xi``。"""
        return split_pinyin(self.pinyin, self.syllables)


@dataclass(frozen=True)
class Snapshot:
    """一次完整的原生状态采样。"""

    words: tuple[NativeWord, ...]
    meta: dict[str, Any]
    fingerprint: str

    @property
    def max_token_id(self) -> int:
        return max((w.token_id for w in self.words), default=0)

    @property
    def tokens_typed(self) -> int:
        v = self.meta.get("numberOfTokensTyped")
        return int(v) if isinstance(v, (int, float)) else 0


# --------------------------------------------------------------------------- 时间


def cf_to_unix(value: Any) -> float | None:
    """CoreFoundation 绝对时间 → Unix 时间戳。"""
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return float(value) + CF_EPOCH_OFFSET


def unix_to_cf(value: float) -> float:
    """Unix 时间戳 → CoreFoundation 绝对时间。"""
    return float(value) - CF_EPOCH_OFFSET


# --------------------------------------------------------------------------- 解码


def _decode(raw: bytes | None) -> str:
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", "replace")


def parse_syllable_lengths(pinyin: str, syllables: str) -> list[int] | None:
    """解析音节长度串；任何不一致都返回 ``None``，绝不抛异常。

    ``str.isdigit()`` 对 ``'²'``（上标二）、``'①'`` 之类也返回 ``True``，
    但 ``int('²')`` 会抛 ``ValueError``。原生 BLOB 是私有格式，不能假设
    永远只含 ASCII 数字 —— 一条脏数据就能让后台代理永久崩在同一个地方。
    """
    if not pinyin or not syllables:
        return None
    if not syllables.isascii() or not syllables.isdigit():
        return None
    steps = [int(ch) for ch in syllables]
    if any(step <= 0 for step in steps) or sum(steps) != len(pinyin):
        return None
    return steps


def split_pinyin(pinyin: str, syllables: str) -> str:
    """按 ``PinyinSyllableLengths`` 把连写拼音切成空格分隔的音节。

    快照里的长度串不可信时，退回原样返回。
    """
    steps = parse_syllable_lengths(pinyin, syllables)
    if steps is None:
        return pinyin
    parts: list[str] = []
    cursor = 0
    for step in steps:
        parts.append(pinyin[cursor : cursor + step])
        cursor += step
    return " ".join(parts)


# --------------------------------------------------------------------------- 指纹


def fingerprint(paths: Paths) -> str | None:
    """廉价的变更指纹：两个文件的 mtime(ns) + size。

    这是整个插件的性能关键——绝大多数触发都止步于此，不会打开 SQLite。
    """
    parts: list[str] = []
    for p in (paths.lexdb, paths.meta):
        try:
            st = p.stat()
        except OSError:
            return None
        parts.append(f"{st.st_mtime_ns}:{st.st_size}")
    return "|".join(parts)


# --------------------------------------------------------------------------- 读取


def _load_lexdb(paths: Paths) -> tuple[NativeWord, ...]:
    """复制一份再打开，避免与正在运行的输入法争锁或读到半写状态。"""
    src = paths.lexdb
    tmp_path: Path | None = None
    try:
        paths.data_dir.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="lexdb-", suffix=".dat", dir=str(paths.data_dir))
        tmp_path = Path(name)
        # mkstemp 返回的 fd 我们自己关，内容用 copyfile 覆盖
        os.close(fd)
        shutil.copyfile(src, tmp_path)
    except PermissionError as exc:
        _cleanup(tmp_path)
        raise NativeAccessError(tcc_hint(str(exc))) from exc
    except OSError as exc:
        _cleanup(tmp_path)
        raise NativeAccessError(f"无法读取 {src}: {exc}") from exc

    try:
        con = sqlite3.connect(f"file:{tmp_path}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT TokenID, Surface, PinyinReading, PinyinSyllableLengths "
                "FROM Words ORDER BY TokenID"
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error as exc:
        raise NativeAccessError(f"无法解析 {src}: {exc}") from exc
    finally:
        _cleanup(tmp_path)

    words: list[NativeWord] = []
    for token_id, surface, reading, lengths in rows:
        text = _decode(surface)
        if not text:
            continue
        words.append(
            NativeWord(
                token_id=int(token_id),
                surface=text,
                pinyin=_decode(reading),
                syllables=_decode(lengths),
            )
        )
    return tuple(words)


def _load_meta(paths: Paths) -> dict[str, Any]:
    try:
        raw = paths.meta.read_bytes()
    except OSError as exc:
        raise NativeAccessError(f"无法读取 {paths.meta}: {exc}") from exc
    try:
        data = plistlib.loads(raw)
    except Exception as exc:
        raise NativeAccessError(f"无法解析 {paths.meta}: {exc}") from exc
    if not isinstance(data, dict):
        raise NativeAccessError(f"{paths.meta} 不是字典")
    return data


def _cleanup(path: Path | None) -> None:
    if path is None:
        return
    with contextlib.suppress(OSError):
        path.unlink()


def tcc_hint(detail: str = "") -> str:
    """TCC（隐私权限）拦截时给出的可操作提示。

    关键事实：launchd 拉起的进程**不会弹授权框**，而是直接收到
    ``PermissionError: [Errno 1] Operation not permitted``。
    用户如果不认识这个错误，只会看到"后台任务悄悄不工作"。
    """
    import sys

    exe = sys.executable
    return (
        "没有权限读取输入法词库（macOS 隐私保护拦截）。\n"
        f"{detail}\n\n"
        "请把下面这个可执行文件加入「完全磁盘访问权限」：\n"
        f"  {exe}\n\n"
        "  系统设置 → 隐私与安全性 → 完全磁盘访问权限 → 点 + →\n"
        "  按 ⌘⇧G 粘贴上面路径 → 选中并允许 → 重启终端/注销重登。\n\n"
        "注意：需要授权的是**实际运行的那个解释器**。从终端手动跑时是终端在"
        "代你持有权限，后台代理跑时则是解释器自己，两者互相不能顶替。"
    )


def read_snapshot(paths: Paths) -> Snapshot:
    """读取当前原生状态。调用前应保证 :func:`fingerprint` 已有变化。"""
    fp = fingerprint(paths)
    if fp is None:
        raise NativeAccessError(
            "找不到原生输入法语言模型目录；请确认已启用「简体拼音」输入法，"
            "并已授予本程序「完全磁盘访问权限」。"
        )
    return Snapshot(words=_load_lexdb(paths), meta=_load_meta(paths), fingerprint=fp)


def read_meta_only(paths: Paths) -> dict[str, Any]:
    return _load_meta(paths)


def now() -> int:
    return int(time.time())
