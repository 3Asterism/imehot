"""账本状态机的单元测试。重点覆盖「新增/删除/复活」这条生命周期。"""

from __future__ import annotations

import sqlite3
import unittest

from imehot.ledger import _DDL, Ledger
from imehot.native import NativeWord, Snapshot


def word(surface: str, pinyin: str, token: int, syllables: str = "") -> NativeWord:
    return NativeWord(token_id=token, surface=surface, pinyin=pinyin, syllables=syllables)


def snap(*words: NativeWord, tokens: int = 0) -> Snapshot:
    return Snapshot(words=tuple(words), meta={"numberOfTokensTyped": tokens}, fingerprint="fp")


class LedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        # isolation_level=None 对应 open_ledger() 的自动提交模式，
        # 否则 Ledger.apply() 里显式的 BEGIN 会和驱动隐式事务冲突。
        self.con = sqlite3.connect(":memory:", isolation_level=None)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(_DDL)
        self.lg = Ledger(self.con)

    def tearDown(self) -> None:
        self.con.close()

    # ---------------------------------------------------------------- 生命周期

    def test_first_snapshot_adds_all(self) -> None:
        diff = self.lg.apply(snap(word("回测", "huice", 1), word("邹宇", "zouyu", 2)), at=100)
        self.assertEqual((diff.added, diff.dropped, diff.revived), (2, 0, 0))
        self.assertEqual(self.lg.counts()["active"], 2)

    def test_second_snapshot_touches_without_adding(self) -> None:
        s = snap(word("回测", "huice", 1))
        self.lg.apply(s, at=100)
        diff = self.lg.apply(s, at=200)
        self.assertEqual((diff.added, diff.touched), (0, 1))
        self.assertEqual(self.lg.get("回测", "huice")["seen_runs"], 2)  # type: ignore[index]

    def test_disappearance_marks_dropped(self) -> None:
        self.lg.apply(snap(word("回测", "huice", 1), word("邹宇", "zouyu", 2)), at=100)
        diff = self.lg.apply(snap(word("回测", "huice", 1)), at=200)
        self.assertEqual(diff.dropped, 1)
        row = self.lg.get("邹宇", "zouyu")
        assert row is not None
        self.assertEqual(row["state"], "dropped")
        self.assertEqual(row["dropped_at"], 200)

    def test_reappearance_revives(self) -> None:
        s = snap(word("回测", "huice", 1))
        self.lg.apply(s, at=100)
        self.lg.apply(snap(), at=200)
        diff = self.lg.apply(s, at=300)
        self.assertEqual(diff.revived, 1)
        row = self.lg.get("回测", "huice")
        assert row is not None
        self.assertEqual(row["state"], "active")
        self.assertEqual(row["revivals"], 1)
        self.assertIsNone(row["dropped_at"])

    def test_duplicate_surface_collapses(self) -> None:
        # 原生词库里同一个「词面+拼音」可能出现两行，只能算一个词。
        diff = self.lg.apply(snap(word("🏠", "jia", 5), word("🏠", "jia", 7)), at=100)
        self.assertEqual(diff.added, 1)
        self.assertEqual(self.lg.counts()["total"], 1)
        self.assertEqual(self.lg.get("🏠", "jia")["token_id"], 5)  # type: ignore[index]

    def test_injected_row_does_not_collide(self) -> None:
        # 注入的词出现在原生快照里时，不能被当成新词重复插入。
        self.lg.add_manual("回测", "huice", "32", tier="hot")
        self.con.execute(
            "UPDATE words SET source = 'injected' WHERE surface = '回测' AND pinyin = 'huice'"
        )
        diff = self.lg.apply(snap(word("回测", "huice", 9)), at=100)
        self.assertEqual(diff.added, 0)
        row = self.lg.get("回测", "huice")
        assert row is not None
        self.assertEqual(row["source"], "injected")
        self.assertEqual(row["state"], "active")
        self.assertEqual(row["token_id"], 9)

    def test_zero_rows_snapshot_does_not_crash(self) -> None:
        diff = self.lg.apply(snap(), at=100)
        self.assertEqual(diff.total, 0)

    # ---------------------------------------------------------------- 状态变更

    def test_set_tier(self) -> None:
        self.lg.apply(snap(word("回测", "huice", 1)), at=100)
        n = self.lg.set_tier([("回测", "huice")], "hot")
        self.assertEqual(n, 1)
        self.assertEqual(self.lg.counts()["hot"], 1)

    def test_set_state_archive(self) -> None:
        self.lg.apply(snap(word("回测", "huice", 1)), at=100)
        n = self.lg.set_state([("回测", "huice")], "archived")
        self.assertEqual(n, 1)
        self.assertEqual(self.lg.counts()["archived"], 1)

    def test_set_state_rejects_unknown(self) -> None:
        with self.assertRaises(ValueError):
            self.lg.set_state([("a", "b")], "nope")

    def test_set_tier_rejects_unknown(self) -> None:
        with self.assertRaises(ValueError):
            self.lg.set_tier([("a", "b")], "nope")

    def test_delete(self) -> None:
        self.lg.apply(snap(word("回测", "huice", 1)), at=100)
        self.assertEqual(self.lg.delete([("回测", "huice")]), 1)
        self.assertEqual(self.lg.counts()["total"], 0)

    def test_manual_add_is_idempotent(self) -> None:
        self.lg.add_manual("回测", "huice", "32")
        self.lg.add_manual("回测", "huice", "32")
        self.assertEqual(self.lg.counts()["total"], 1)
        self.assertEqual(self.lg.counts()["manual"], 1)

    # ---------------------------------------------------------------- 审计

    def test_events_recorded(self) -> None:
        self.lg.apply(snap(word("回测", "huice", 1)), at=100)
        kinds = [r["kind"] for r in self.lg.events(10)]
        self.assertIn("added", kinds)

    def test_record_run(self) -> None:
        from imehot.ledger import Diff

        self.lg.record_run(1.5, True, Diff(added=1, total=1), reason="test")
        runs = self.lg.last_runs(1)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["reason"], "test")

    def test_filters(self) -> None:
        self.lg.apply(snap(word("回测", "huice", 1), word("邹宇", "zouyu", 2)), at=100)
        self.lg.set_tier([("回测", "huice")], "hot")
        self.assertEqual(len(self.lg.rows(tier="hot")), 1)
        self.assertEqual(len(self.lg.rows(state="active")), 2)
        self.assertEqual(len(self.lg.rows(state="dropped")), 0)


if __name__ == "__main__":
    unittest.main()
