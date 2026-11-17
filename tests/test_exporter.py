"""导出格式的单元测试：每种格式都要能被下游真正解析。"""

from __future__ import annotations

import json
import plistlib
import sqlite3
import unittest

from imehot.exporter import render
from imehot.ledger import _DDL, Ledger
from imehot.native import NativeWord, Snapshot

NOW = 1_000 * 86_400


class ExportTest(unittest.TestCase):
    def setUp(self) -> None:
        con = sqlite3.connect(":memory:", isolation_level=None)
        con.row_factory = sqlite3.Row
        con.executescript(_DDL)
        self.lg = Ledger(con)
        snap = Snapshot(
            words=(
                NativeWord(1, "回测", "huice", "32"),
                NativeWord(2, "郝晓晞", "haoxiaoxi", "342"),
            ),
            meta={},
            fingerprint="fp",
        )
        self.lg.apply(snap, at=NOW - 90 * 86_400)
        self.rows = self.lg.rows(state="active", order="surface ASC")
        self.con = con

    def tearDown(self) -> None:
        self.con.close()

    def test_tsv_roundtrip(self) -> None:
        text = render(self.rows, "tsv", NOW).decode()
        lines = [ln for ln in text.splitlines() if ln]
        self.assertEqual(lines[0].split("\t")[0], "surface")
        self.assertEqual(len(lines), 1 + len(self.rows))

    def test_json_roundtrip(self) -> None:
        payload = json.loads(render(self.rows, "json", NOW))
        self.assertEqual(payload["count"], 2)
        surfaces = {w["surface"] for w in payload["words"]}
        self.assertEqual(surfaces, {"回测", "郝晓晞"})

    def test_plist_roundtrip(self) -> None:
        items = plistlib.loads(render(self.rows, "plist", NOW))
        self.assertIsInstance(items, list)
        for item in items:
            self.assertIn("phrase", item)
            self.assertIn("shortcut", item)
        shortcuts = {i["shortcut"] for i in items}
        self.assertEqual(shortcuts, {"huice", "haoxiaoxi"})

    def test_rime_roundtrip(self) -> None:
        text = render(self.rows, "rime", NOW).decode()
        body = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
        self.assertEqual(len(body), 2)
        for line in body:
            cols = line.split("\t")
            self.assertEqual(len(cols), 3)
            self.assertTrue(cols[2].isdigit())
        self.assertIn("hao xiao xi\t郝晓晞", text)

    def test_unknown_format_raises(self) -> None:
        with self.assertRaises(ValueError):
            render(self.rows, "xml", NOW)

    def test_empty_export(self) -> None:
        self.assertEqual(json.loads(render([], "json", NOW))["count"], 0)
        self.assertEqual(plistlib.loads(render([], "plist", NOW)), [])


if __name__ == "__main__":
    unittest.main()
