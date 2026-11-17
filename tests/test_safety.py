"""安全与「不打扰」的守卫测试。

这些断言不是测功能，而是测**边界**。任何一条挂掉，都说明有人不小心把
项目推进了"会监视用户 / 会打断输入"的方向。

**诚实说明**：这是一个 denylist 冒烟测试，不是形式化证明。它挡得住"顺手加一行
`os.kill`"，挡不住刻意规避（`ctypes` 动态调用、`getattr` 拼字符串等）。
真正的保证来自代码量小 + 人工审查。
"""

from __future__ import annotations

import ast
import pathlib
import plistlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "imehot"

#: 一旦在源码里出现，就说明开始做键盘/屏幕/剪贴板采集了。见 ADR 0002。
FORBIDDEN_APIS = (
    "CGEventTap",
    "CGEventTapCreate",
    "CGEventPost",
    "CGEventCreate",
    "IOHIDManager",
    "addGlobalMonitorForEvents",
    "CGWindowListCreateImage",
    "CGDisplayStream",
    "NSPasteboard",
    "NSEvent",
    "AppKit",
    "pynput",
    "Quartz",
    "pyobjc",
    "ctypes",
    "screencapture",
)

#: 外部命令白名单之外的东西。`imehot` 只允许调 launchctl / pgrep / ps / 解释器。
FORBIDDEN_COMMANDS = ("osascript", "pbcopy", "pbpaste", "screencapture", "curl", "wget")

FORBIDDEN_NETWORK = ("urllib", "requests", "http.client", "socket.", "ftplib", "smtplib")

#: 绝不允许出现在后台 `run` 路径里的名字（覆盖 `from x import stop_ime` 这种写法）。
PROCESS_KILLING_NAMES = (
    "stop_ime",
    "ime_pids",
    "ime_running",
    "wait_ime_back",
    "apply_plan",
    "force_decay",
    "kill",
    "killpg",
    "system",
    "Popen",
    "run",
)


def _module_sources() -> list[tuple[str, str]]:
    """递归扫描整个包（含将来可能出现的子包）。"""
    return [
        (str(p.relative_to(SRC)), p.read_text(encoding="utf-8")) for p in sorted(SRC.rglob("*.py"))
    ]


class NoSnoopingTest(unittest.TestCase):
    def test_no_snooping_apis(self) -> None:
        for name, text in _module_sources():
            for token in FORBIDDEN_APIS:
                with self.subTest(module=name, token=token):
                    self.assertNotIn(token, text, f"{name} 出现了被禁止的 API：{token}")

    def test_no_network(self) -> None:
        for name, text in _module_sources():
            for token in FORBIDDEN_NETWORK:
                with self.subTest(module=name, token=token):
                    self.assertNotIn(token, text, f"{name} 出现了网络调用：{token}")

    def test_no_forbidden_subprocess_targets(self) -> None:
        """禁止通过子进程间接读剪贴板/截屏/联网。"""
        for name, text in _module_sources():
            for token in FORBIDDEN_COMMANDS:
                with self.subTest(module=name, token=token):
                    self.assertNotIn(f'"{token}"', text, f"{name} 调用了外部命令 {token}")
                    self.assertNotIn(f"'{token}'", text, f"{name} 调用了外部命令 {token}")


class DaemonIsReadOnlyTest(unittest.TestCase):
    """后台代理只允许跑 `imehot run`，而 `run` 不允许碰输入法进程。"""

    def _paths(self) -> object:
        from imehot.config import Paths

        return Paths(home=pathlib.Path("/tmp/x"), data_dir=pathlib.Path("/tmp/x/data"))

    def test_launchd_runs_only_run_quiet(self) -> None:
        from imehot import launchd

        payload = launchd.render(self._paths())  # type: ignore[arg-type]
        self.assertEqual(payload["ProgramArguments"][-4:], ["-m", "imehot", "run", "--quiet"])
        for sub in ("inject", "prune", "decay", "uninstall", "install"):
            self.assertNotIn(sub, payload["ProgramArguments"])

    def test_launchd_is_not_a_daemon(self) -> None:
        """没有 KeepAlive，才谈得上"不常驻"。"""
        from imehot import launchd

        payload = launchd.render(self._paths())  # type: ignore[arg-type]
        self.assertNotIn("KeepAlive", payload)
        self.assertTrue(payload["RunAtLoad"])
        self.assertIn("StartInterval", payload)
        self.assertIn("WatchPaths", payload)

    def test_launchd_plist_is_valid(self) -> None:
        from imehot import launchd
        from imehot.config import BUNDLE_ID

        blob = plistlib.dumps(launchd.render(self._paths()))  # type: ignore[arg-type]
        data = plistlib.loads(blob)
        self.assertEqual(data["Label"], BUNDLE_ID)
        self.assertEqual(data["ProcessType"], "Background")
        self.assertTrue(data["LowPriorityIO"])
        self.assertGreaterEqual(int(data["Nice"]), 10)
        self.assertGreaterEqual(int(data["ThrottleInterval"]), 30)

    def test_launchd_pythonpath_is_outside_tcc_protected_dirs(self) -> None:
        """代理加载的代码不能放在 ~/Documents、~/Desktop、~/Downloads。

        这三个目录受 TCC 保护，launchd 拉起的进程读不到、也不会弹授权框，
        结果是进程卡死在 import 阶段（实测 last exit code = never exited）。
        """
        from imehot import launchd

        payload = launchd.render(self._paths())  # type: ignore[arg-type]
        pythonpath = payload["EnvironmentVariables"]["PYTHONPATH"]
        for guarded in ("/Documents", "/Desktop", "/Downloads"):
            self.assertNotIn(guarded, pythonpath)

    def test_cmd_run_never_touches_ime_process(self) -> None:
        """用 AST 同时检查 Name 与 Attribute，覆盖 `from x import stop_ime`。"""
        tree = ast.parse((SRC / "cli.py").read_text(encoding="utf-8"))
        fn = next(
            (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "cmd_run"),
            None,
        )
        self.assertIsNotNone(fn, "找不到 cmd_run")
        assert fn is not None

        names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}

        # run 里唯一允许的 injector 调用是扫临时文件
        for bad in PROCESS_KILLING_NAMES:
            with self.subTest(name=bad):
                self.assertNotIn(bad, names)
        for bad in ("kill", "killpg", "system", "Popen", "stop_ime", "ime_pids"):
            with self.subTest(attr=bad):
                self.assertNotIn(bad, attrs)
        for module in ("subprocess", "signal"):
            with self.subTest(module=module):
                self.assertNotIn(module, names)


class AtomicWriteTest(unittest.TestCase):
    def test_lexdb_writes_are_atomic(self) -> None:
        text = (SRC / "injector.py").read_text(encoding="utf-8")
        self.assertIn("os.replace(tmp, target)", text)

    def test_meta_write_is_atomic_and_verified(self) -> None:
        text = (SRC / "injector.py").read_text(encoding="utf-8")
        self.assertIn("os.replace(tmp, paths.meta)", text)
        self.assertNotIn("paths.meta.write_bytes(", text)
        # 换入前必须能回读
        self.assertIn("plistlib.loads(tmp.read_bytes())", text)

    def test_backup_never_overwrites(self) -> None:
        """备份必须用 mkstemp，不能靠秒级时间戳拼文件名。"""
        text = (SRC / "injector.py").read_text(encoding="utf-8")
        self.assertIn("mkstemp(", text)
        self.assertIn("dir=str(paths.backups)", text)

    def test_decay_backs_up_lexdb_too(self) -> None:
        """原生修剪可能删 lexdb 里的词，只备份 meta 是自欺欺人。"""
        text = (SRC / "injector.py").read_text(encoding="utf-8")
        self.assertIn("backup(paths, paths.lexdb)", text)


class PurgeSafetyTest(unittest.TestCase):
    """`uninstall --purge` 递归删目录，必须拒绝危险的 IMEHOT_DATA_DIR。"""

    def _paths(self, data: pathlib.Path, home: pathlib.Path) -> object:
        from imehot.config import Paths

        return Paths(home=home, data_dir=data)

    def test_refuses_root(self) -> None:
        p = self._paths(pathlib.Path("/"), pathlib.Path("/tmp/h"))
        ok, why = p.purge_is_safe()  # type: ignore[attr-defined]
        self.assertFalse(ok)
        self.assertIn("根目录", why)

    def test_refuses_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            data = home / "Library" / "Application Support" / "IMEHot"
            data.mkdir(parents=True)
            ok, _ = self._paths(data, home).purge_is_safe()  # type: ignore[attr-defined]
            self.assertFalse(ok, "没有标记文件时也必须拒绝")
            (data / ".imehot-root").write_text("x")
            ok, _ = self._paths(data, home).purge_is_safe()  # type: ignore[attr-defined]
            self.assertTrue(ok)

    def test_refuses_home_itself(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            ok, why = self._paths(home, home).purge_is_safe()  # type: ignore[attr-defined]
            self.assertFalse(ok)
            self.assertIn("HOME", why)

    def test_refuses_documents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            docs = home / "Documents"
            docs.mkdir()
            (docs / ".imehot-root").write_text("x")
            ok, _ = self._paths(docs, home).purge_is_safe()  # type: ignore[attr-defined]
            self.assertFalse(ok)


class SweepTest(unittest.TestCase):
    def test_sweep_covers_native_temp_names(self) -> None:
        from imehot import injector

        text = injector.__file__
        source = pathlib.Path(text).read_text(encoding="utf-8")
        self.assertIn('"lexdb-*.dat"', source)
        self.assertIn('"lexdb-work-*.dat"', source)


if __name__ == "__main__":
    unittest.main()
