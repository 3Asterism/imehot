"""衰减模型的单元测试。"""

from __future__ import annotations

import sqlite3
import unittest

from imehot.decay import half_life_factor, idle_days, label, score_row

DAY = 86_400

#: 内存库连接留到 tearDownModule 统一关闭。
#: 不这么做的话，连接被 GC 时会在**任意**行触发 ResourceWarning，
#: traceback 指向的位置和真正的泄漏点无关，非常难查。
_CONNS: list[sqlite3.Connection] = []


def make_row(**kw: object) -> sqlite3.Row:
    con = sqlite3.connect(":memory:")
    _CONNS.append(con)
    con.row_factory = sqlite3.Row
    con.execute(
        "CREATE TABLE t (state TEXT, strength REAL, dropped_at INTEGER, last_evidence INTEGER)"
    )
    con.execute(
        "INSERT INTO t VALUES (?, ?, ?, ?)",
        (
            kw.get("state", "active"),
            kw.get("strength", 1.0),
            kw.get("dropped_at"),
            kw.get("last_evidence", 0),
        ),
    )
    return con.execute("SELECT * FROM t").fetchone()


def tearDownModule() -> None:
    for con in _CONNS:
        con.close()
    _CONNS.clear()


class HalfLifeTest(unittest.TestCase):
    def test_zero_idle_is_one(self) -> None:
        self.assertAlmostEqual(half_life_factor(0, 90), 1.0)

    def test_one_half_life(self) -> None:
        self.assertAlmostEqual(half_life_factor(90, 90), 0.5)

    def test_two_half_lives(self) -> None:
        self.assertAlmostEqual(half_life_factor(180, 90), 0.25)

    def test_negative_idle_clamped(self) -> None:
        self.assertAlmostEqual(half_life_factor(-5, 90), 1.0)

    def test_zero_half_life_is_zero(self) -> None:
        self.assertEqual(half_life_factor(10, 0), 0.0)


class ScoreRowTest(unittest.TestCase):
    def test_active_never_decays(self) -> None:
        row = make_row(state="active", strength=1.0, last_evidence=0)
        sc = score_row(row, now=10_000 * DAY, half_life_days=90)
        self.assertEqual(sc.value, 1.0)
        self.assertEqual(sc.idle_days, 0.0)

    def test_archived_is_zero(self) -> None:
        row = make_row(state="archived", strength=5.0)
        self.assertEqual(score_row(row, 0).value, 0.0)

    def test_dropped_decays(self) -> None:
        now = 1_000 * DAY
        row = make_row(state="dropped", strength=1.0, dropped_at=now - 90 * DAY)
        sc = score_row(row, now=now, half_life_days=90)
        self.assertAlmostEqual(sc.value, 0.5, places=6)
        self.assertAlmostEqual(sc.idle_days, 90.0, places=6)

    def test_strength_scales(self) -> None:
        now = 1_000 * DAY
        row = make_row(state="dropped", strength=3.0, dropped_at=now - 90 * DAY)
        self.assertAlmostEqual(score_row(row, now=now, half_life_days=90).value, 1.5, places=6)

    def test_idle_days_uses_dropped_at(self) -> None:
        now = 1_000 * DAY
        row = make_row(state="dropped", dropped_at=now - 10 * DAY)
        self.assertAlmostEqual(idle_days(row, now), 10.0)


class LabelTest(unittest.TestCase):
    def test_buckets(self) -> None:
        self.assertEqual(label(2.5), "🔥🔥")
        self.assertEqual(label(1.0), "🔥")
        self.assertEqual(label(0.3), "·")
        self.assertEqual(label(0.0), "❄")


if __name__ == "__main__":
    unittest.main()
