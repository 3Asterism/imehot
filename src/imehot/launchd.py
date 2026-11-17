"""launchd 用户代理的安装与卸载。

性能设计的核心就在这个文件里：**不用轮询，也不用常驻进程**。

``WatchPaths``
    launchd 用 kqueue/FSEvents 在内核里守着我们关心的路径。文件没动就
    完全不启动进程 —— 空闲成本严格为 **0 CPU / 0 内存**。

``ThrottleInterval``
    文件系统事件会成串到达（原生输入法一次 flush 会同时改写多个文件），
    用节流把它们压成一次运行。

``StartInterval``
    兜底。万一 WatchPaths 因为文件被替换而漏掉事件，最多延迟这么久补上。
    因为命中「指纹未变」时进程会在几十毫秒内退出，这个兜底几乎不要钱。

``ProcessType=Background`` + ``LowPriorityIO`` + ``Nice``
表明这是后台杂务，让 launchd 和内核把 IO/CPU 优先级压到最低，
不跟用户正在用的程序抢资源。
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import BUNDLE_ID, Paths

DEFAULT_THROTTLE_SECONDS = 60
DEFAULT_START_INTERVAL = 1800

#: 代码实际兼容的最低版本（已在 3.9 / 3.12 / 3.14 上跑过全量测试）。
MIN_PYTHON = (3, 9)

#: 优先使用的解释器：本项目自带安装的 3.14 排在最前。
_PREFERRED = ("~/.local/python/3.14/bin/python3.14", "~/.local/python/3.14/bin/python3")


def python_version(executable: str) -> tuple[int, int] | None:
    """探测解释器版本；不可执行或报错时返回 None。"""
    try:
        proc = subprocess.run(
            [executable, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        major, minor = proc.stdout.strip().split(".")
        return int(major), int(minor)
    except ValueError:
        return None


def interpreter_candidates() -> list[str]:
    """按优先级列出候选解释器（去重、只保留真实存在的）。"""
    raw: list[str | None] = [os.environ.get("IMEHOT_PYTHON")]
    raw += [str(Path(p).expanduser()) for p in _PREFERRED]
    raw += [sys.executable, shutil.which("python3"), "/usr/bin/python3"]
    out: list[str] = []
    for item in raw:
        if item and item not in out and Path(item).exists():
            out.append(item)
    return out


def pick_interpreter() -> tuple[str, tuple[int, int]]:
    """挑一个满足最低版本要求的解释器。

    失败时抛 ``RuntimeError``，并在消息里列出所有试过的路径 —— 静默回退
    到系统自带的 Python 3.9 再莫名报语法错误，是最难查的一类故障。
    """
    tried: list[str] = []
    for exe in interpreter_candidates():
        version = python_version(exe)
        tried.append(f"{exe} → {'.'.join(map(str, version)) if version else '不可用'}")
        if version and version >= MIN_PYTHON:
            return exe, version
    raise RuntimeError(
        f"找不到 Python ≥ {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 的解释器。已尝试：\n  "
        + "\n  ".join(tried)
        + "\n\n安装一个即可：https://www.python.org/downloads/"
    )


def src_root() -> Path:
    """``sys.path`` 上需要的目录（``src/``）。"""
    return Path(__file__).resolve().parents[1]


def deploy_root(paths: Paths) -> Path:
    """代理实际加载代码的位置。

    为什么要把代码复制出来跑，而不是直接指向仓库
    --------------------------------------------------
    ``~/Documents``、``~/Desktop``、``~/Downloads`` 都是 **TCC 保护目录**。
    launchd 拉起的进程既没有继承终端的权限，也没有交互界面去弹授权框，
    结果就是 Python 在 ``site``/import 阶段直接卡死（实测：进程挂在
    ``Py_InitializeFromConfig → Py_Import_ImportModule``，永不退出，
    ``last exit code = (never exited)``）。

    ``~/Library/Application Support`` 不在 TCC 保护名单里，所以把代码部署到
    这里，代理就能在没有额外授权的情况下启动。
    """
    return paths.data_dir / "lib"


def deploy(paths: Paths) -> Path:
    """把 ``src/imehot`` 同步到部署目录，返回该目录。"""
    import shutil

    target = deploy_root(paths) / "imehot"
    source = Path(__file__).resolve().parent
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return target.parent


def render(
    paths: Paths,
    *,
    throttle: int = DEFAULT_THROTTLE_SECONDS,
    start_interval: int = DEFAULT_START_INTERVAL,
    interpreter: str | None = None,
    pythonpath: str | None = None,
) -> dict[str, Any]:
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    python = interpreter or sys.executable or "/usr/bin/python3"
    watch = [str(paths.lm_dir), str(paths.lexdb), str(paths.meta)]
    return {
        "Label": BUNDLE_ID,
        "ProgramArguments": [python, "-m", "imehot", "run", "--quiet"],
        "EnvironmentVariables": {
            # 默认就是部署目录，**不是**仓库的 src/：仓库通常在 ~/Documents 下，
            # 那是 TCC 保护目录，launchd 拉起的进程读不到，会卡死在 import 阶段。
            "PYTHONPATH": pythonpath or str(deploy_root(paths)),
            "PYTHONUNBUFFERED": "1",
        },
        "WatchPaths": watch,
        "ThrottleInterval": int(throttle),
        "StartInterval": int(start_interval),
        "RunAtLoad": True,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
        "StandardOutPath": str(paths.log),
        "StandardErrorPath": str(paths.log),
    }


def install(
    paths: Paths,
    *,
    throttle: int = DEFAULT_THROTTLE_SECONDS,
    start_interval: int = DEFAULT_START_INTERVAL,
    interpreter: str | None = None,
) -> Path:
    lib = deploy(paths)
    payload = render(
        paths,
        throttle=throttle,
        start_interval=start_interval,
        interpreter=interpreter,
        pythonpath=str(lib),
    )
    paths.launch_agent.parent.mkdir(parents=True, exist_ok=True)
    paths.launch_agent.write_bytes(plistlib.dumps(payload))
    _bootout(paths)
    _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(paths.launch_agent)], check=True)
    _run(["launchctl", "enable", f"gui/{os.getuid()}/{BUNDLE_ID}"], check=False)
    return paths.launch_agent


def uninstall(paths: Paths) -> bool:
    import shutil

    _bootout(paths)
    lib = deploy_root(paths)
    if lib.exists():
        shutil.rmtree(lib, ignore_errors=True)
    if paths.launch_agent.exists():
        paths.launch_agent.unlink()
        return True
    return False


def _bootout(paths: Paths) -> None:
    _run(["launchctl", "bootout", f"gui/{os.getuid()}/{BUNDLE_ID}"], check=False)
    if paths.launch_agent.exists():
        _run(["launchctl", "bootout", f"gui/{os.getuid()}", str(paths.launch_agent)], check=False)


def kickstart() -> None:
    _run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{BUNDLE_ID}"], check=False)


@dataclass(frozen=True)
class AgentStatus:
    installed: bool
    loaded: bool
    detail: str


def status(paths: Paths) -> AgentStatus:
    installed = paths.launch_agent.exists()
    if not installed:
        return AgentStatus(False, False, "未安装")
    proc = _run(["launchctl", "print", f"gui/{os.getuid()}/{BUNDLE_ID}"], check=False)
    if proc.returncode != 0:
        return AgentStatus(True, False, "plist 存在但未加载")
    detail = ""
    for line in proc.stdout.splitlines():
        s = line.strip()
        if s.startswith(("state =", "pid =", "last exit code =")):
            detail += s + "; "
    return AgentStatus(True, True, detail or "已加载")


def _run(cmd: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} 失败 (exit {proc.returncode}): {proc.stderr.strip()}")
    return proc
