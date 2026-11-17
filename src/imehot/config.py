"""路径与常量解析。

所有路径均可通过环境变量覆盖，便于测试、沙箱运行与容器化部署：

    IMEHOT_HOME        伪造的 HOME（默认取真实 HOME）
    IMEHOT_DATA_DIR    运行数据目录（默认 ~/Library/Application Support/IMEHot）
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "IMEHot"
BUNDLE_ID = "com.lixiaoxiong.imehot"

#: 默认半衰期（天）。参考青简输入法的 90 天冷却与 librime 的指数遗忘。
DEFAULT_HALF_LIFE_DAYS = 90.0

#: CoreFoundation 绝对时间（2001-01-01T00:00:00Z）与 Unix 纪元的秒差。
CF_EPOCH_OFFSET = 978_307_200

#: 原生简体拼音输入法的语言模型目录（相对于 HOME）。
_SCIM_LM = (
    "Library/Containers/com.apple.inputmethod.SCIM/Data/Library/LanguageModeling/zh_Hans-dynamic.lm"
)


@dataclass(frozen=True)
class Paths:
    """一次解析、处处复用的路径集合。"""

    home: Path
    data_dir: Path

    # ---- 我们自己的数据 ----
    @property
    def db(self) -> Path:
        return self.data_dir / "state.db"

    @property
    def fingerprint(self) -> Path:
        return self.data_dir / "fingerprint"

    @property
    def log(self) -> Path:
        return self.data_dir / "imehot.log"

    @property
    def backups(self) -> Path:
        return self.data_dir / "backups"

    @property
    def exports(self) -> Path:
        return self.data_dir / "exports"

    # ---- launchd ----
    @property
    def launch_agent(self) -> Path:
        return self.home / "Library/LaunchAgents" / f"{BUNDLE_ID}.plist"

    # ---- 原生输入法数据 ----
    @property
    def lm_dir(self) -> Path:
        return self.home / _SCIM_LM

    @property
    def lexdb(self) -> Path:
        return self.lm_dir / "lexdb.dat"

    @property
    def meta(self) -> Path:
        return self.lm_dir / "meta.dat"

    @property
    def dynamic(self) -> Path:
        return self.lm_dir / "dynamic.dat"

    @property
    def marker(self) -> Path:
        """数据目录归属标记。

        ``uninstall --purge`` 会递归删除整个数据目录，所以必须先证明
        "这个目录确实是 imehot 建的"。如果有人把 ``IMEHOT_DATA_DIR``
        指向 ``$HOME`` 或 ``/``，没有这层校验就会造成灾难。
        """
        return self.data_dir / ".imehot-root"

    def ensure(self) -> Paths:
        """创建所有可写目录与归属标记（幂等）。

        目录权限收到 0700：``state.db`` 里是「你学过哪些词」，
        SECURITY.md 把它当个人数据，文件系统权限必须跟上。
        """
        for d in (self.data_dir, self.backups, self.exports):
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
            with contextlib.suppress(OSError):
                d.chmod(0o700)
        if not self.marker.exists():
            self.marker.write_text("imehot data directory\n", encoding="utf-8")
        return self

    def purge_is_safe(self) -> tuple[bool, str]:
        """判断 ``rmtree(data_dir)`` 是否安全。"""
        try:
            target = self.data_dir.resolve()
        except OSError as exc:
            return False, f"无法解析数据目录: {exc}"
        home = self.home.resolve() if self.home.exists() else self.home
        if target == Path("/") or target.parent == Path("/"):
            return False, "拒绝删除根目录或其直接子目录"
        if target == home:
            return False, "拒绝删除 HOME 本身"
        for d in ("Documents", "Desktop", "Downloads", "Library", "Pictures", "Music"):
            if target == home / d:
                return False, f"拒绝删除 {d} 本身"
        if not target.exists():
            return True, ""
        if not self.marker.exists():
            return False, f"{target} 里没有 imehot 标记文件，拒绝删除"
        return True, ""


def resolve_paths() -> Paths:
    home = Path(os.environ.get("IMEHOT_HOME") or Path.home()).expanduser()
    raw = os.environ.get("IMEHOT_DATA_DIR")
    data_dir = (
        Path(raw).expanduser() if raw else home / "Library" / "Application Support" / APP_NAME
    )
    return Paths(home=home, data_dir=data_dir)
