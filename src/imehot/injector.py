"""实验性写回：向原生词库注入热词、或请求原生输入法自己衰减一次。

**默认关闭，每个入口都要求显式 ``--experimental``。**

为什么单独隔离
--------------
:mod:`imehot.native` 只读，任何情况下都不会改动原生数据。写入是另一类风险：

* ``lexdb.dat`` 的 schema 是私有实现，系统升级可能变更；
* 输入法进程可能在我们写完之后用自己的内存状态覆盖回去；
* ``TokenID`` 的分配规则未知（实测从 ``0xC00000`` 连续递增）。

因此这里的所有操作都遵守同一套纪律：

1. 先做**完整备份**到 ``backups/``（用 ``mkstemp`` 保证不会撞名覆盖）；
2. 在**临时副本**上改，``PRAGMA integrity_check`` 通过后才原子替换；
3. 用 ``os.replace`` 换 inode —— 输入法持有的旧 fd 仍指向旧 inode，
   不会读到半写文件，也不会因并发写而损坏；
4. ``--dry-run`` 可以在不落盘的情况下看到将要发生的每一步。

关于"备份"的诚实边界
--------------------
``decay`` 会让**原生输入法**执行一次衰减/修剪，它可能删除 ``lexdb.dat`` 里的词。
所以 ``force_decay`` 会同时备份 ``meta.dat`` 和 ``lexdb.dat`` —— 只备份前者
是自欺欺人。
"""

from __future__ import annotations

import contextlib
import os
import plistlib
import shutil
import signal
import sqlite3
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Paths
from .native import NativeAccessError, cf_to_unix, unix_to_cf

#: 简体拼音输入法扩展的进程名（用于 ``pgrep -x``，精确匹配）。
SCIM_PROCESS_NAME = "SCIM_Extension"

#: 该进程可执行文件的路径特征，用于二次校验，避免误杀同名进程。
SCIM_EXECUTABLE_HINT = "SCIM_Extension.appex/Contents/MacOS/SCIM_Extension"

#: ``force_decay`` 默认把衰减指针往回拨多少天。
_FORCE_DECAY_DAYS = 400


@dataclass(frozen=True)
class InjectionPlan:
    surface: str
    pinyin: str  # 空格分隔的音节
    syllables: str
    token_id: int = 0

    @property
    def reading(self) -> str:
        return self.pinyin.replace(" ", "")


@dataclass
class InjectResult:
    planned: list[InjectionPlan]
    skipped: list[tuple[str, str]]  # (surface, 原因)
    backups: list[Path] = None  # type: ignore[assignment]
    applied: bool = False

    def __post_init__(self) -> None:
        if self.backups is None:
            self.backups = []


# --------------------------------------------------------------------------- 备份


def _fsync(path: Path) -> None:
    """把文件内容刷到磁盘。掉电场景下 ``copy2`` 不代表已落盘。"""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def backup(paths: Paths, source: Path) -> Path:
    """把原生文件整体备份到 ``backups/``，返回备份路径。

    用 ``mkstemp`` 而不是按时间戳拼文件名：同一秒内连续两次写回时，
    时间戳会撞名，``copy2`` 会把**上一次的原始备份**覆盖掉，
    而那份才是真正的回滚点。
    """
    paths.backups.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    fd, name = tempfile.mkstemp(
        prefix=f"{source.name}.{stamp}.", suffix=".bak", dir=str(paths.backups)
    )
    os.close(fd)
    dest = Path(name)
    try:
        shutil.copy2(source, dest)
        _fsync(dest)
        _fsync_dir(paths.backups)
    except OSError:
        dest.unlink(missing_ok=True)
        raise
    return dest


# --------------------------------------------------------------------------- 计划


def _syllable_lengths(pinyin: str) -> str:
    parts = [p for p in pinyin.split(" ") if p]
    if len(parts) < 1:
        raise ValueError("拼音为空")
    if any(not p.isascii() or not p.isalpha() for p in parts):
        raise ValueError(f"拼音只能包含 ASCII 字母: {pinyin!r}")
    return "".join(str(len(p)) for p in parts)


def plan(paths: Paths, wanted: list[tuple[str, str]]) -> InjectResult:
    """计算注入计划。``wanted`` 为 ``(词面, 空格分隔拼音)`` 列表。"""
    con, tmp = _open_lexdb_copy(paths)
    try:
        existing = {
            (r[0], r[1])
            for r in con.execute(
                "SELECT CAST(Surface AS TEXT), CAST(PinyinReading AS TEXT) FROM Words"
            )
        }
        max_id = con.execute("SELECT COALESCE(MAX(TokenID), 0) FROM Words").fetchone()[0]
    finally:
        con.close()
        _discard(tmp)

    planned: list[InjectionPlan] = []
    skipped: list[tuple[str, str]] = []
    next_id = int(max_id) + 1
    seen: set[tuple[str, str]] = set()

    for surface, spaced in wanted:
        surface = surface.strip()
        if not surface:
            skipped.append((surface, "空词面"))
            continue
        try:
            lengths = _syllable_lengths(spaced)
        except ValueError as exc:
            skipped.append((surface, str(exc)))
            continue
        reading = spaced.replace(" ", "")
        key = (surface, reading)
        if key in existing:
            skipped.append((surface, "已存在于原生词库"))
            continue
        if key in seen:
            skipped.append((surface, "输入中重复"))
            continue
        seen.add(key)
        planned.append(InjectionPlan(surface, spaced, lengths, next_id))
        next_id += 1

    return InjectResult(planned=planned, skipped=skipped)


# --------------------------------------------------------------------------- 写入


def apply_plan(paths: Paths, result: InjectResult, dry_run: bool = False) -> InjectResult:
    if dry_run or not result.planned:
        return result

    result.backups.append(backup(paths, paths.lexdb))
    work, tmp = _open_lexdb_copy(paths, writable=True)
    try:
        for item in result.planned:
            work.execute(
                "INSERT INTO Words (TokenID, Surface, PinyinReading, PinyinSyllableLengths)"
                " VALUES (?, ?, ?, ?)",
                (
                    item.token_id,
                    item.surface.encode(),
                    item.reading.encode(),
                    item.syllables.encode(),
                ),
            )
        work.commit()
        ok = work.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise NativeAccessError(f"注入后完整性检查失败: {ok}")
    except Exception:
        work.close()
        _discard(tmp)
        raise
    else:
        work.close()

    _replace_into(tmp, paths.lexdb, expected_rows=len(result.planned))
    result.applied = True
    return result


def remove(
    paths: Paths, keys: list[tuple[str, str]], dry_run: bool = False
) -> tuple[int, Path | None]:
    """从原生词库删除词条。``keys`` 为 ``(词面, 拼音)``。返回 (删除数, 备份路径)。"""
    if dry_run or not keys:
        return 0, None

    bak = backup(paths, paths.lexdb)
    work, tmp = _open_lexdb_copy(paths, writable=True)
    n = 0
    try:
        for surface, reading in keys:
            cur = work.execute(
                "DELETE FROM Words"
                " WHERE CAST(Surface AS TEXT) = ?"
                "   AND CAST(PinyinReading AS TEXT) = ?",
                (surface, reading.replace(" ", "")),
            )
            n += cur.rowcount
        work.commit()
        ok = work.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise NativeAccessError(f"删除后完整性检查失败: {ok}")
        if n == 0:
            # 一行都没删掉，就没有必要重写原生文件（换 inode、触发 launchd、
            # 承担一次写风险）。直接放弃这次副本。
            work.close()
            _discard(tmp)
            return 0, bak
    except Exception:
        work.close()
        _discard(tmp)
        raise
    else:
        work.close()

    _replace_into(tmp, paths.lexdb)
    return n, bak


def _replace_into(tmp: Path, target: Path, expected_rows: int | None = None) -> None:
    """把临时副本原子换入目标位置，并做换入后回验。

    跨卷时 ``os.replace`` 会抛 ``EXDEV``；这里提前判断并给出可操作的错误，
    而不是让用户看到一个裸的 errno。
    """
    try:
        if tmp.stat().st_dev != target.parent.stat().st_dev:
            _discard(tmp)
            raise NativeAccessError(
                "临时文件与原文件不在同一卷上，无法原子替换。"
                "请把 IMEHOT_DATA_DIR 设在和 ~/Library 同一个卷上。"
            )
        os.replace(tmp, target)
    except OSError as exc:
        _discard(tmp)
        raise NativeAccessError(f"替换 {target} 失败: {exc}") from exc

    _fsync_dir(target.parent)

    if expected_rows is None:
        return
    con = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    try:
        total = con.execute("SELECT COUNT(*) FROM Words").fetchone()[0]
    except sqlite3.Error as exc:
        raise NativeAccessError(f"替换后无法读取 {target}: {exc}") from exc
    finally:
        con.close()
    if total <= 0:
        raise NativeAccessError(f"替换后 {target} 为空，请从 backups/ 恢复")


# --------------------------------------------------------------------------- 衰减


def force_decay(
    paths: Paths, dry_run: bool = False, days: int = _FORCE_DECAY_DAYS
) -> tuple[dict[str, Any], list[Path]]:
    """改写 ``meta.dat`` 让原生输入法在下次落盘时执行一次衰减/修剪。

    这是「清理很久不用的词」最温和的做法：我们不动词表，
    只把输入法自己的记账指针往回拨，让它用**自己的模型**做清理。

    重要：原生修剪**可能删除 lexdb 里的词**，所以这里同时备份 lexdb。
    """
    raw = paths.meta.read_bytes()
    meta = plistlib.loads(raw)
    now_cf = unix_to_cf(time.time())

    before = {
        "lastDecay": cf_to_unix(meta.get("lastDecay")),
        "lastPruning": cf_to_unix(meta.get("lastPruning")),
        "wordsSinceLastDecay": meta.get("wordsSinceLastDecay"),
    }

    meta["lastDecay"] = now_cf - days * 86400
    meta["lastPruning"] = now_cf - days * 86400
    meta["wordsSinceLastDecay"] = max(int(meta.get("wordsSinceLastDecay") or 0), 1_000_000)

    after = {
        "lastDecay": cf_to_unix(meta.get("lastDecay")),
        "lastPruning": cf_to_unix(meta.get("lastPruning")),
        "wordsSinceLastDecay": meta.get("wordsSinceLastDecay"),
    }

    changed = {k: (before[k], after[k]) for k in before}
    if dry_run:
        return changed, []

    backups = [backup(paths, paths.meta), backup(paths, paths.lexdb)]

    # 保留原文件权限。mkstemp 建出来是 0600，但 write_bytes 新建是 0644；
    # meta.dat 里有累计上屏量和衰减时间，不能因为一次写回就把权限放宽。
    try:
        original_mode = stat.S_IMODE(paths.meta.stat().st_mode)
    except OSError:
        original_mode = 0o600

    tmp = paths.data_dir / f"meta.dat.tmp.{os.getpid()}"
    try:
        tmp.write_bytes(plistlib.dumps(meta, fmt=plistlib.FMT_BINARY))
        # 回验：能重新解析成 dict 才换入
        if not isinstance(plistlib.loads(tmp.read_bytes()), dict):
            raise NativeAccessError("生成的 meta.dat 无法回读")
        _fsync(tmp)
        if tmp.stat().st_dev != paths.meta.parent.stat().st_dev:
            raise NativeAccessError(
                "临时文件与 meta.dat 不在同一卷，无法原子替换。"
                "请把 IMEHOT_DATA_DIR 设在和 ~/Library 同一个卷上。"
            )
        os.chmod(tmp, 0o600)
        os.replace(tmp, paths.meta)
        os.chmod(paths.meta, original_mode)
        _fsync_dir(paths.meta.parent)
    finally:
        tmp.unlink(missing_ok=True)
    return changed, backups


# --------------------------------------------------------------------------- 临时副本


def _open_lexdb_copy(paths: Paths, writable: bool = False) -> tuple[sqlite3.Connection, Path]:
    """把 lexdb 复制到数据目录再打开，避免与输入法争锁。

    返回 ``(连接, 临时文件路径)``。调用方**必须**在关闭连接后自己处理这个
    临时文件：要么 ``os.replace`` 换入，要么 :func:`_discard` 删掉。
    """
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="lexdb-work-", suffix=".dat", dir=str(paths.data_dir))
    os.close(fd)
    tmp = Path(name)
    try:
        shutil.copyfile(paths.lexdb, tmp)
    except OSError as exc:
        _discard(tmp)
        raise NativeAccessError(f"无法读取 {paths.lexdb}: {exc}") from exc
    try:
        con = sqlite3.connect(str(tmp))
        con.row_factory = sqlite3.Row
        return con, tmp
    except sqlite3.Error as exc:
        _discard(tmp)
        raise NativeAccessError(f"无法打开 {paths.lexdb} 的副本: {exc}") from exc


def _discard(path: Path) -> None:
    """删除临时文件，顺带清掉 SQLite 可能留下的 journal。"""
    for p in (path, path.with_name(path.name + "-journal"), path.with_name(path.name + "-wal")):
        with contextlib.suppress(OSError):
            p.unlink()


def sweep_temp(paths: Paths, max_age_seconds: float = 3600) -> int:
    """清理上次异常退出遗留的临时副本。返回删除数量。"""
    if not paths.data_dir.is_dir():
        return 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    patterns = ("lexdb-*.dat", "lexdb-work-*.dat", "meta.dat.tmp.*")
    for pattern in patterns:
        for p in paths.data_dir.glob(pattern):
            try:
                if p.stat().st_mtime > cutoff:
                    continue
                _discard(p)
                removed += 1
            except OSError:
                continue
    return removed


# --------------------------------------------------------------------------- 输入法进程控制


def ime_pids() -> list[int]:
    """当前运行的 ``SCIM_Extension`` 进程号。

    用 ``pgrep -x`` 精确匹配进程名，再用可执行文件路径二次校验。
    早期版本用 ``pgrep -f SCIM_Extension``，它会匹配**任何命令行里含这个字符串**
    的进程（比如你正好在 shell 里 grep 它），然后被 SIGKILL —— 这是不可接受的。
    """
    proc = subprocess.run(
        ["pgrep", "-x", SCIM_PROCESS_NAME], capture_output=True, text=True, check=False
    )
    pids = [int(x) for x in proc.stdout.split() if x.strip().isdigit()]
    verified: list[int] = []
    for pid in pids:
        try:
            cmd = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
        except OSError:
            continue
        if SCIM_EXECUTABLE_HINT in cmd:
            verified.append(pid)
    return verified


def ime_running() -> bool:
    return bool(ime_pids())


def stop_ime(timeout: float = 5.0) -> bool:
    """停止输入法扩展，返回是否已确认退出。

    实测：``SCIM_Extension`` 忽略 ``SIGTERM``，必须 ``SIGKILL``。
    杀掉之后 launchd 会在下一次需要输入时按需重启。

    注意：这**会丢掉正在组字（还没上屏）的拼音和候选**。调用方有责任
    先确认用户不在输入状态。
    """
    pids = ime_pids()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not ime_pids():
            return True
        time.sleep(0.05)
    return not ime_pids()


def wait_ime_back(timeout: float = 8.0) -> bool:
    """写完之后等输入法回来。

    它通常由 launchd 按需拉起（下一次聚焦输入框时）。等不到也不一定是故障，
    所以调用方只应把它当成提示依据，而不是失败条件。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ime_pids():
            return True
        time.sleep(0.2)
    return bool(ime_pids())


# --------------------------------------------------------------------------- 回滚

_KIND_BY_PREFIX = (("lexdb.dat", "lexdb"), ("meta.dat", "meta"))


@dataclass(frozen=True)
class BackupInfo:
    path: Path
    kind: str
    size: int
    valid: bool
    detail: str


def _kind_of(name: str) -> str | None:
    for prefix, kind in _KIND_BY_PREFIX:
        if name.startswith(prefix):
            return kind
    return None


def list_backups(paths: Paths) -> list[BackupInfo]:
    """列出所有备份并就地验证是否可用于回滚。"""
    if not paths.backups.is_dir():
        return []
    out: list[BackupInfo] = []
    for p in sorted(paths.backups.iterdir(), reverse=True):
        if not p.is_file():
            continue
        kind = _kind_of(p.name)
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if kind is None:
            out.append(BackupInfo(p, "unknown", size, False, "无法识别的备份类型"))
            continue
        valid, detail = _validate_backup(p, kind)
        out.append(BackupInfo(p, kind, size, valid, detail))
    return out


def _validate_backup(path: Path, kind: str) -> tuple[bool, str]:
    if kind == "lexdb":
        con = None
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            ok = con.execute("PRAGMA integrity_check").fetchone()[0]
            if ok != "ok":
                return False, f"完整性检查失败: {ok}"
            n = con.execute("SELECT COUNT(*) FROM Words").fetchone()[0]
            return True, f"{n} 词"
        except sqlite3.Error as exc:
            return False, f"无法解析: {exc}"
        finally:
            if con is not None:
                con.close()
    try:
        data = plistlib.loads(path.read_bytes())
    except Exception as exc:
        return False, f"无法解析: {exc}"
    if not isinstance(data, dict):
        return False, "不是字典"
    return True, f"{len(data)} 个字段"


def restore(paths: Paths, backup_file: Path, dry_run: bool = False) -> Path:
    """把一份备份写回原生位置。返回恢复前做的安全备份路径。

    顺序：验证备份 → 停输入法（调用方负责）→ 备份当前文件 →
    复制到数据目录 → 原子替换。
    """
    kind = _kind_of(backup_file.name)
    if kind is None:
        raise NativeAccessError(f"{backup_file.name} 不是可识别的备份（lexdb.dat / meta.dat）")
    target = paths.lexdb if kind == "lexdb" else paths.meta

    valid, detail = _validate_backup(backup_file, kind)
    if not valid:
        raise NativeAccessError(f"备份不可用：{detail}")

    if dry_run:
        return target

    # 恢复本身也是一次写操作，所以先把"现在的"再备份一次，避免恢复错了没退路。
    backup(paths, target)

    tmp = paths.data_dir / f"restore-{os.getpid()}-{backup_file.name}"
    try:
        shutil.copyfile(backup_file, tmp)
        _fsync(tmp)
        if tmp.stat().st_dev != target.parent.stat().st_dev:
            raise NativeAccessError("备份与目标不在同一卷，无法原子替换")
        os.replace(tmp, target)
        _fsync_dir(target.parent)
    finally:
        tmp.unlink(missing_ok=True)

    valid, detail = _validate_backup(target, kind)
    if not valid:
        raise NativeAccessError(f"恢复后验证失败：{detail}")
    return target
