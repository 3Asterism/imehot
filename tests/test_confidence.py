"""置信度模型与错字过滤的测试（ADR 0005）。

注意：置信度是**查询时现算的派生视图**，不是账本里的列。
所以断言要写成 `effective_confidence(row, now)`，而不是查某个字段。
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from imehot.ledger import (
    _DDL,
    SCHEMA_VERSION,
    Ledger,
    _migrate,
    effective_confidence,
    open_ledger,
)
from imehot.native import NativeWord, Snapshot

DAY = 86_400


def w(surface: str, pinyin: str, syllables: str, token: int) -> NativeWord:
    return NativeWord(token_id=token, surface=surface, pinyin=pinyin, syllables=syllables)


def snap(*words: NativeWord, tokens: int = 0) -> Snapshot:
    return Snapshot(words=tuple(words), meta={"numberOfTokensTyped": tokens}, fingerprint="fp")


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.con = sqlite3.connect(":memory:", isolation_level=None)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(_DDL)
        self.lg = Ledger(self.con)

    def tearDown(self) -> None:
        self.con.close()

    def row(self, surface: str) -> sqlite3.Row:
        r = self.con.execute("SELECT * FROM words WHERE surface = ?", (surface,)).fetchone()
        assert r is not None, surface
        return r

    def conf(self, surface: str, now: int) -> str:
        return effective_confidence(self.row(surface), now)


class TypoDetectionTest(Base):
    def test_real_user_case_marked_as_suspect(self) -> None:
        """说思路 被打成 所思路：先学到的那个记上 suspect_of。"""
        diff = self.lg.apply(
            snap(
                w("所思路", "suosilu", "322", 1),
                w("说思路", "shuosilu", "422", 2),
            ),
            at=0,
        )
        self.assertEqual(diff.suspects, 1)
        self.assertEqual(self.row("所思路")["suspect_of"], "说思路")
        self.assertEqual(self.conf("所思路", 0), "suspect")
        self.assertEqual(self.conf("说思路", 0), "candidate")

    def test_suspect_never_confirms_by_cooling(self) -> None:
        """冷静期不能让疑似错字自动转正，否则"错字不进热词库"是空话。"""
        self.lg.apply(
            snap(w("所思路", "suosilu", "322", 1), w("说思路", "shuosilu", "422", 2)),
            at=0,
        )
        self.assertEqual(self.conf("所思路", 30 * DAY), "suspect")
        self.assertEqual(self.conf("说思路", 30 * DAY), "confirmed")

    def test_suspect_confirms_after_revival(self) -> None:
        """被原生删掉又学回来 = 确实在用，这是唯一的自动例外。"""
        s = snap(w("所思路", "suosilu", "322", 1), w("说思路", "shuosilu", "422", 2))
        self.lg.apply(s, at=0)
        self.lg.apply(snap(w("说思路", "shuosilu", "422", 2)), at=100)
        self.lg.apply(s, at=200)
        self.assertEqual(self.conf("所思路", 200), "confirmed")

    def test_chain_keeps_only_last(self) -> None:
        self.lg.apply(
            snap(
                w("所思路", "suosilu", "322", 1),
                w("所示录", "suoshilu", "332", 2),
                w("说思路", "shuosilu", "422", 3),
            ),
            at=0,
        )
        self.assertEqual(self.conf("所思路", 0), "suspect")
        self.assertEqual(self.conf("所示录", 0), "suspect")
        self.assertEqual(self.conf("说思路", 0), "candidate")

    def test_far_apart_tokens_not_paired(self) -> None:
        diff = self.lg.apply(
            snap(w("所思路", "suosilu", "322", 1), w("说思路", "shuosilu", "422", 20)),
            at=0,
        )
        self.assertEqual(diff.suspects, 0)
        self.assertEqual(self.conf("所思路", 0), "candidate")

    def test_different_reading_not_paired(self) -> None:
        diff = self.lg.apply(snap(w("回测", "huice", "32", 1), w("孟豪", "menghao", "43", 2)), at=0)
        self.assertEqual(diff.suspects, 0)

    def test_first_batch_is_not_judged(self) -> None:
        """首次导入/重建账本时整批历史词一起进来，不能当纠正对判。"""
        words = [w(f"词{i}一二", f"cisan{i}", "323", i) for i in range(1, 21)]
        diff = self.lg.apply(snap(*words), at=0)
        self.assertEqual(diff.suspects, 0)

    def test_bad_syllables_do_not_crash(self) -> None:
        """一条脏音节数据不能让整批处理抛异常。"""
        diff = self.lg.apply(
            snap(
                w("所思路", "suosilu", "²²²", 1),
                w("说思路", "shuosilu", "422", 2),
            ),
            at=0,
        )
        self.assertEqual(diff.added, 2)


class ConfidenceLifecycleTest(Base):
    def test_new_word_is_candidate_then_confirms_by_age(self) -> None:
        self.lg.apply(snap(w("回测", "huice", "32", 1)), at=0)
        self.assertEqual(self.conf("回测", 0), "candidate")
        self.assertEqual(self.conf("回测", int(2.1 * DAY)), "confirmed")

    def test_revival_confirms_immediately(self) -> None:
        s = snap(w("回测", "huice", "32", 1))
        self.lg.apply(s, at=0)
        self.lg.apply(snap(), at=100)
        self.lg.apply(s, at=200)
        self.assertEqual(self.conf("回测", 200), "confirmed")

    def test_override_rejected_wins(self) -> None:
        s = snap(w("所思路", "suosilu", "322", 1))
        self.lg.apply(s, at=0)
        self.lg.set_override([("所思路", "suosilu")], "rejected")
        self.lg.apply(snap(), at=100)
        self.lg.apply(s, at=200)
        self.assertEqual(self.conf("所思路", 10 * DAY), "rejected")

    def test_override_can_be_cleared(self) -> None:
        self.lg.apply(snap(w("回测", "huice", "32", 1)), at=0)
        self.lg.set_override([("回测", "huice")], "rejected")
        self.assertEqual(self.conf("回测", 10 * DAY), "rejected")
        self.lg.set_override([("回测", "huice")], None)
        self.assertEqual(self.conf("回测", 10 * DAY), "confirmed")

    def test_manual_add_is_confirmed(self) -> None:
        self.lg.add_manual("回测", "huice", "32")
        self.assertEqual(self.conf("回测", 0), "confirmed")

    def test_rows_filter_by_derived_confidence(self) -> None:
        self.lg.apply(
            snap(w("所思路", "suosilu", "322", 1), w("说思路", "shuosilu", "422", 2)),
            at=0,
        )
        self.assertEqual(len(self.lg.rows(confidence="suspect", now=0)), 1)
        self.assertEqual(len(self.lg.rows(confidence="confirmed", now=0)), 0)

    def test_confidence_counts(self) -> None:
        self.lg.apply(
            snap(w("所思路", "suosilu", "322", 1), w("说思路", "shuosilu", "422", 2)),
            at=0,
        )
        counts = self.lg.confidence_counts(now=0)
        self.assertEqual(counts["suspect"], 1)
        self.assertEqual(counts["candidate"], 1)


class ManualSourceTest(Base):
    def test_manual_row_in_snapshot_is_not_recounted(self) -> None:
        """source='manual' 的行出现在快照里时，不能被每轮当成新词。"""
        self.lg.add_manual("回测", "huice", "32")
        self.con.execute("UPDATE words SET source = 'manual'")
        s = snap(w("回测", "huice", "32", 9))
        first = self.lg.apply(s, at=0)
        second = self.lg.apply(s, at=100)
        self.assertEqual(first.added, 0)
        self.assertEqual(second.added, 0)
        self.assertEqual(self.row("回测")["state"], "active")

    def test_manual_row_gets_dropped_when_gone(self) -> None:
        self.lg.add_manual("回测", "huice", "32")
        self.con.execute("UPDATE words SET source = 'manual'")
        self.lg.apply(snap(w("回测", "huice", "32", 9)), at=0)
        diff = self.lg.apply(snap(), at=100)
        self.assertEqual(diff.dropped, 1)
        self.assertEqual(self.row("回测")["state"], "dropped")


class StateConsistencyTest(Base):
    def test_set_state_keeps_dropped_at_consistent(self) -> None:
        self.lg.apply(snap(w("回测", "huice", "32", 1)), at=0)
        self.lg.set_state([("回测", "huice")], "dropped")
        self.assertIsNotNone(self.row("回测")["dropped_at"])
        self.lg.set_state([("回测", "huice")], "active")
        self.assertIsNone(self.row("回测")["dropped_at"])

    def test_rows_rejects_arbitrary_order(self) -> None:
        with self.assertRaises(ValueError):
            self.lg.rows(order="surface; DROP TABLE words")


class BatchTest(Base):
    def test_batch_records_token_range(self) -> None:
        self.lg.apply(snap(w("回测", "huice", "32", 100), w("孟豪", "menghao", "43", 105)), at=0)
        batch = self.lg.last_batches(1)[0]
        self.assertEqual((batch["token_min"], batch["token_max"]), (100, 105))

    def test_all_new_words_get_batch_id(self) -> None:
        self.lg.apply(snap(w("回测", "huice", "32", 100), w("孟豪", "menghao", "43", 105)), at=0)
        ids = {r["batch_id"] for r in self.con.execute("SELECT batch_id FROM words")}
        self.assertEqual(len(ids), 1)
        self.assertIsNotNone(ids.pop())


class MigrationTest(unittest.TestCase):
    V1 = """
    CREATE TABLE words (
        surface TEXT NOT NULL, pinyin TEXT NOT NULL,
        syllables TEXT NOT NULL DEFAULT '', token_id INTEGER,
        tier TEXT NOT NULL DEFAULT 'ledger', source TEXT NOT NULL DEFAULT 'native',
        state TEXT NOT NULL DEFAULT 'active', born_at INTEGER NOT NULL,
        born_token INTEGER, dropped_at INTEGER, last_evidence INTEGER NOT NULL,
        revivals INTEGER NOT NULL DEFAULT 0, seen_runs INTEGER NOT NULL DEFAULT 0,
        strength REAL NOT NULL DEFAULT 1.0, note TEXT,
        PRIMARY KEY (surface, pinyin)
    );
    """

    def test_v1_migrates(self) -> None:
        con = sqlite3.connect(":memory:", isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            con.executescript(self.V1)
            con.execute(
                "INSERT INTO words (surface, pinyin, born_at, last_evidence)"
                " VALUES ('回测', 'huice', 1, 1)"
            )
            _migrate(con)
            cols = {r[1] for r in con.execute("PRAGMA table_info(words)")}
            for expected in ("override", "batch_id", "suspect_of"):
                self.assertIn(expected, cols)
            row = con.execute("SELECT * FROM words").fetchone()
            assert row is not None
            # 老数据没有 override，按年龄自然判定
            self.assertIsNone(row["override"])
            self.assertEqual(effective_confidence(row, 30 * DAY), "confirmed")
        finally:
            con.close()

    def test_migrate_is_idempotent(self) -> None:
        con = sqlite3.connect(":memory:", isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            con.executescript(_DDL)
            _migrate(con)
            _migrate(con)
        finally:
            con.close()

    def test_migrate_without_batches_table(self) -> None:
        """只有 words 表的老库也不能让迁移崩。"""
        con = sqlite3.connect(":memory:", isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            con.executescript(self.V1)
            _migrate(con)
            cols = {r[1] for r in con.execute("PRAGMA table_info(words)")}
            self.assertIn("override", cols)
        finally:
            con.close()

    def test_open_ledger_on_real_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.db"
            con = open_ledger(path)
            try:
                self.assertTrue(path.exists())
                row = con.execute(
                    "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                ).fetchone()
                assert row is not None
                self.assertEqual(row["value"], str(SCHEMA_VERSION))
                # 账本里是个人数据，权限必须是 0600
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            finally:
                con.close()


if __name__ == "__main__":
    unittest.main()
