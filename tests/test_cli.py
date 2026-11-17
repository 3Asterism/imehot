"""CLI 参数解析的单元测试（不触碰真实系统）。"""

from __future__ import annotations

import contextlib
import io
import unittest

from imehot.cli import build_parser


class ParserTest(unittest.TestCase):
    def test_all_subcommands_parse(self) -> None:
        p = build_parser()
        cases = [
            ["run"],
            ["run", "--quiet", "--force"],
            ["status"],
            ["words"],
            ["words", "-s", "all", "-t", "hot", "-c", "suspect", "-n", "5", "--json"],
            ["promote"],
            ["promote", "--apply", "--include-english"],
            ["review"],
            ["confirm", "回测"],
            ["reject", "所思路"],
            ["reject", "所思路", "--no-archive"],
            ["prune", "--older-than", "30"],
            ["export", "-f", "plist"],
            ["export", "-f", "tsv", "-c", "all"],
            ["inject", "x.tsv", "--experimental", "--dry-run"],
            ["decay", "--experimental"],
            ["install"],
            ["uninstall", "--purge"],
            ["doctor"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                ns = p.parse_args(argv)
                self.assertTrue(callable(ns.func))

    def test_run_defaults(self) -> None:
        ns = build_parser().parse_args(["run"])
        self.assertFalse(ns.quiet)
        self.assertFalse(ns.force)

    def test_prune_defaults(self) -> None:
        ns = build_parser().parse_args(["prune"])
        self.assertEqual(ns.older_than, 180.0)
        self.assertFalse(ns.hard)

    def test_inject_requires_experimental_flag_not_enforced_by_parser(self) -> None:
        ns = build_parser().parse_args(["inject", "x.tsv"])
        self.assertFalse(ns.experimental)

    def test_unknown_subcommand_exits(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(["nope"])


if __name__ == "__main__":
    unittest.main()
