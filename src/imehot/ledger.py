"""热词账本：把原生快照的差异持久化成可查询的生命周期记录。

核心思想（见 ``docs/adr/0001``）：**原生输入法就是"这个词还在用吗"的权威
裁判**。我们不猜测、不监听键盘，只忠实地记录每个词在原生词库里的进出：

``active``     此刻存在于 ``lexdb.dat``
``dropped``    曾存在，后来被原生输入法自己移除
``archived``   由本工具按策略归档（不再参与导出）

关于「置信度」的一个重要设计修正（见 ADR 0005 的修订）
------------------------------------------------------
第一版把 ``confidence`` 当成一个**持久列**写入，并让导出/提升默认只看
``confirmed``。这是错的，有两个原因：

1. **它是推断，不是事实。**「这个像不像错字」由 TokenID 相邻性和拼音相似度
   推导出来，属于启发式。把推断存成列并让它决定数据去留，等于把猜测当观测，
   与 ADR 0004 拒绝预计算 decay 的理由自相矛盾。
2. **冷静期会卡死。** 早期实现只在词库发生变化时才推进 ``seen_runs``，
   于是"词库这几天没变化"就等于"冷静期永远不结束"，新用户默认导出为空。

现在的做法：**置信度是查询时现算的派生视图**，账本只存原始事实
（出生时刻、复活次数、是否成对被怀疑、以及用户显式的 override）。
换算规则或阈值可以随时改，历史数据不用重写。
"""

from __future__ import annotations

import contextlib
import sqlite3
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import similarity
from .native import NativeWord, Snapshot

SCHEMA_VERSION = 3

#: 判定"纠正对"时允许的 TokenID 间隔（同一批输入里连续学到的词）。
TYPO_WINDOW = 3

#: 冷静期：新词观察这么久之后才视为"确认"。按时间算，不按采样次数算 ——
#: 采样次数取决于词库有没有变化，用它当门槛会让冷静期永远不结束。
CONFIRM_MIN_AGE_DAYS = 2.0

#: 首批导入的安全阀：一次新增这么多词，说明是首次导入/重建账本，
#: 而不是一次真实的输入会话，不做纠正对判定。
FIRST_BATCH_MAX_NEW = 12

_DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS words (
    surface       TEXT    NOT NULL,
    pinyin        TEXT    NOT NULL,
    syllables     TEXT    NOT NULL DEFAULT '',
    token_id      INTEGER,
    tier          TEXT    NOT NULL DEFAULT 'ledger',
    source        TEXT    NOT NULL DEFAULT 'native',
    state         TEXT    NOT NULL DEFAULT 'active',
    override      TEXT,
    born_at       INTEGER NOT NULL,
    born_token    INTEGER,
    dropped_at    INTEGER,
    last_evidence INTEGER NOT NULL,
    revivals      INTEGER NOT NULL DEFAULT 0,
    seen_runs     INTEGER NOT NULL DEFAULT 0,
    strength      REAL    NOT NULL DEFAULT 1.0,
    batch_id      INTEGER,
    suspect_of    TEXT,
    note          TEXT,
    PRIMARY KEY (surface, pinyin)
);

CREATE INDEX IF NOT EXISTS idx_words_state    ON words(state);
CREATE INDEX IF NOT EXISTS idx_words_tier     ON words(tier);
CREATE INDEX IF NOT EXISTS idx_words_evidence ON words(last_evidence);
-- 注意：依赖 v2+ 新增列的索引必须在 _migrate() 之后创建。
-- 放在这里会让老库在 CREATE TABLE 被跳过时直接报 "no such column"。

CREATE TABLE IF NOT EXISTS batches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL,
    n_new         INTEGER NOT NULL,
    tokens_before INTEGER,
    tokens_after  INTEGER,
    token_min     INTEGER,
    token_max     INTEGER
);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    kind    TEXT    NOT NULL,
    surface TEXT,
    pinyin  TEXT,
    detail  TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    duration_ms REAL    NOT NULL,
    changed     INTEGER NOT NULL,
    n_words     INTEGER NOT NULL,
    added       INTEGER NOT NULL DEFAULT 0,
    dropped     INTEGER NOT NULL DEFAULT 0,
    revived     INTEGER NOT NULL DEFAULT 0,
    reason      TEXT
);
"""

_STATES = ("active", "dropped", "archived")
_TIERS = ("ledger", "hot")
_CONFIDENCE = ("suspect", "candidate", "confirmed", "rejected")
_OVERRIDES = ("confirmed", "rejected")

#: ``rows(order=...)`` 的白名单。拼 SQL 之前先查表，避免未来把 CLI 参数拼进去。
_ORDER_WHITELIST = (
    "last_evidence DESC, surface ASC",
    "last_evidence DESC",
    "born_at DESC",
    "born_at ASC",
    "surface ASC",
    "surface DESC",
)


@dataclass(frozen=True)
class Diff:
    """一次快照比对的结果。"""

    added: int = 0
    dropped: int = 0
    revived: int = 0
    touched: int = 0
    total: int = 0
    suspects: int = 0
    batch_id: int | None = None

    @property
    def changed(self) -> bool:
        return bool(self.added or self.dropped or self.revived)


# --------------------------------------------------------------------------- 置信度（派生）


def effective_confidence(
    row: sqlite3.Row | dict[str, Any],
    now: int,
    *,
    min_age_days: float = CONFIRM_MIN_AGE_DAYS,
) -> str:
    """由原始事实现算置信度。不读任何缓存列。

    优先顺序：
    1. 用户显式 override（这是**事实**，不是推断）；
    2. 被纠正对检测标记过 → ``suspect``，除非它后来复活过（说明确实在用）；
    3. 复活过 → ``confirmed``；
    4. 出生已超过冷静期 → ``confirmed``；
    5. 否则 ``candidate``。
    """
    override = row["override"]
    if override in _OVERRIDES:
        return str(override)
    if row["suspect_of"]:
        return "confirmed" if (row["revivals"] or 0) >= 1 else "suspect"
    if (row["revivals"] or 0) >= 1:
        return "confirmed"
    if now - int(row["born_at"]) >= int(min_age_days * 86_400):
        return "confirmed"
    return "candidate"


def open_ledger(path: Path) -> sqlite3.Connection:
    """打开（必要时创建并迁移）账本。使用 WAL 以降低写放大与锁竞争。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), isolation_level=None, timeout=5.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(_DDL)
    _migrate(con)
    con.execute(
        "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    # 账本里是"你学过哪些词"，属于个人数据。0600 与 SECURITY.md 的威胁模型对齐。
    _harden(path)
    return con


def _harden(path: Path) -> None:
    """把账本和它的目录收到只有本人可读。

    ``state.db`` 里是"你学过哪些词"的完整列表，SECURITY.md 把它当个人数据，
    但 ``sqlite3.connect`` 默认按 umask 生成 0644，多用户 Mac 上别人能读。
    """
    for target, mode in ((path, 0o600), (path.parent, 0o700)):
        with contextlib.suppress(OSError):
            target.chmod(mode)


def _migrate(con: sqlite3.Connection) -> None:
    """幂等迁移。每一列单独判断，容忍并发打开时的重复 ALTER。"""
    cols = {row[1] for row in con.execute("PRAGMA table_info(words)")}
    wanted = {
        "batch_id": "ALTER TABLE words ADD COLUMN batch_id INTEGER",
        "suspect_of": "ALTER TABLE words ADD COLUMN suspect_of TEXT",
        "override": "ALTER TABLE words ADD COLUMN override TEXT",
    }
    for column, statement in wanted.items():
        if column in cols:
            continue
        try:
            con.execute(statement)
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise

    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "batches" in tables:
        batch_cols = {row[1] for row in con.execute("PRAGMA table_info(batches)")}
        for column in ("token_min", "token_max"):
            if column not in batch_cols:
                try:
                    con.execute(f"ALTER TABLE batches ADD COLUMN {column} INTEGER")
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise


class Ledger:
    """账本的高级操作封装。"""

    def __init__(self, con: sqlite3.Connection) -> None:
        self.con = con

    # ------------------------------------------------------------------ 写入

    def apply(
        self,
        snap: Snapshot,
        at: int | None = None,
        *,
        typo_threshold: float = similarity.DEFAULT_THRESHOLD,
        typo_window: int = TYPO_WINDOW,
    ) -> Diff:
        """把一次原生快照并入账本，返回差异统计。"""
        now = int(at if at is not None else time.time())

        current: dict[tuple[str, str], NativeWord] = {}
        for w in snap.words:
            key = (w.surface, w.pinyin)
            prev = current.get(key)
            if prev is None or w.token_id < prev.token_id:
                current[key] = w

        # 注入/手工加入的词一旦出现在原生词库里，就应按原生词的生命周期管理。
        # 只查 native 会让 source='manual' 的行每轮都被当成新词重复计数。
        existing = {
            (r["surface"], r["pinyin"]): r
            for r in self.con.execute(
                "SELECT * FROM words WHERE source IN ('native', 'injected', 'manual')"
            )
        }

        added_rows: list[NativeWord] = []
        added = dropped = revived = touched = 0
        self.con.execute("BEGIN")
        try:
            for key, w in current.items():
                row = existing.get(key)
                if row is None:
                    self._insert_native(w, now)
                    added_rows.append(w)
                    added += 1
                elif row["state"] == "active":
                    self.con.execute(
                        "UPDATE words SET seen_runs = seen_runs + 1, token_id = ?,"
                        " syllables = ? WHERE surface = ? AND pinyin = ?",
                        (w.token_id, w.syllables, w.surface, w.pinyin),
                    )
                    touched += 1
                else:
                    self.con.execute(
                        "UPDATE words SET state = 'active', dropped_at = NULL,"
                        " revivals = revivals + 1, last_evidence = ?, token_id = ?,"
                        " syllables = ? WHERE surface = ? AND pinyin = ?",
                        (now, w.token_id, w.syllables, w.surface, w.pinyin),
                    )
                    self._event(now, "revived", w.surface, w.pinyin, f"from={row['state']}")
                    revived += 1

            for key, row in existing.items():
                if row["state"] == "active" and key not in current:
                    self.con.execute(
                        "UPDATE words SET state = 'dropped', dropped_at = ?"
                        " WHERE surface = ? AND pinyin = ?",
                        (now, *key),
                    )
                    self._event(now, "dropped", row["surface"], row["pinyin"], None)
                    dropped += 1

            batch_id = None
            suspects = 0
            if added_rows:
                ordered = sorted(added_rows, key=lambda w: w.token_id)
                batch_id = self._new_batch(now, ordered, snap.tokens_typed)
                # 首次导入/重建账本时，整批历史词会一起进来；它们的 TokenID 相邻
                # 只代表"很久以前先后学到"，不代表同一次输入里的先错后对。
                if len(ordered) <= FIRST_BATCH_MAX_NEW:
                    suspects = self._mark_corrections(
                        ordered, batch_id, now, typo_threshold, typo_window
                    )

            self.con.execute("COMMIT")
        except Exception:
            with contextlib.suppress(sqlite3.Error):
                self.con.execute("ROLLBACK")
            raise

        return Diff(
            added=added,
            dropped=dropped,
            revived=revived,
            touched=touched,
            total=len(current),
            suspects=suspects,
            batch_id=batch_id,
        )

    def _insert_native(self, w: NativeWord, now: int) -> None:
        self.con.execute(
            "INSERT INTO words (surface, pinyin, syllables, token_id, tier, source,"
            " state, born_at, born_token, last_evidence, seen_runs)"
            " VALUES (?, ?, ?, ?, 'ledger', 'native', 'active', ?, ?, ?, 1)"
            " ON CONFLICT(surface, pinyin) DO UPDATE SET"
            " state = 'active', dropped_at = NULL, token_id = excluded.token_id,"
            " syllables = excluded.syllables, last_evidence = excluded.last_evidence,"
            " seen_runs = words.seen_runs + 1",
            (w.surface, w.pinyin, w.syllables, w.token_id, now, w.token_id, now),
        )
        self._event(now, "added", w.surface, w.pinyin, f"token={w.token_id}")

    def _new_batch(self, now: int, ordered: list[NativeWord], tokens_after: int | None) -> int:
        before = self.con.execute(
            "SELECT tokens_after FROM batches ORDER BY id DESC LIMIT 1"
        ).fetchone()
        tokens = [w.token_id for w in ordered]
        cur = self.con.execute(
            "INSERT INTO batches (ts, n_new, tokens_before, tokens_after, token_min, token_max)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                now,
                len(ordered),
                before["tokens_after"] if before else None,
                tokens_after,
                min(tokens),
                max(tokens),
            ),
        )
        batch_id = int(cur.lastrowid or 0)
        for w in ordered:
            self.con.execute(
                "UPDATE words SET batch_id = ? WHERE surface = ? AND pinyin = ?",
                (batch_id, w.surface, w.pinyin),
            )
        return batch_id

    def _mark_corrections(
        self,
        ordered: list[NativeWord],
        batch_id: int,
        now: int,
        threshold: float,
        window: int,
    ) -> int:
        """在同一批次里找"先选错、后改对"的词对，把前者记上 ``suspect_of``。

        依据见 ``docs/adr/0005``：TokenID 单调递增 = 学习先后顺序。
        相邻两个词读音高度相似时，TokenID 较小的那个是先被选中的。

        这里只记录**这一对关系**（可复算的派生信息），不记录"置信度"这种
        综合判断 —— 后者由 :func:`effective_confidence` 在查询时算。
        """
        marked = 0
        for i in range(len(ordered) - 1):
            a = ordered[i]
            b = ordered[i + 1]
            if b.token_id - a.token_id > window:
                continue
            if not similarity.looks_like_correction(
                a.pinyin,
                a.syllables,
                a.surface,
                b.pinyin,
                b.syllables,
                b.surface,
                threshold=threshold,
            ):
                continue
            cur = self.con.execute(
                "UPDATE words SET suspect_of = ? WHERE surface = ? AND pinyin = ?"
                " AND suspect_of IS NULL",
                (b.surface, a.surface, a.pinyin),
            )
            if cur.rowcount:
                marked += 1
                self._event(now, "suspect", a.surface, a.pinyin, f"→{b.surface}")
        return marked

    def record_run(
        self, duration_ms: float, changed: bool, diff: Diff, reason: str = "watch"
    ) -> None:
        self.con.execute(
            "INSERT INTO runs (ts, duration_ms, changed, n_words, added, dropped, revived, reason)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(time.time()),
                round(duration_ms, 3),
                int(changed),
                diff.total,
                diff.added,
                diff.dropped,
                diff.revived,
                reason,
            ),
        )

    def _event(self, ts: int, kind: str, surface: str, pinyin: str, detail: str | None) -> None:
        self.con.execute(
            "INSERT INTO events (ts, kind, surface, pinyin, detail) VALUES (?, ?, ?, ?, ?)",
            (ts, kind, surface, pinyin, detail),
        )

    def event(
        self,
        kind: str,
        surface: str | None,
        pinyin: str | None = None,
        detail: str | None = None,
    ) -> None:
        self._event(int(time.time()), kind, surface or "", pinyin or "", detail)

    # ------------------------------------------------------------------ 状态变更

    def set_tier(self, keys: Iterable[tuple[str, str]], tier: str) -> int:
        if tier not in _TIERS:
            raise ValueError(f"未知 tier: {tier}")
        n = 0
        self.con.execute("BEGIN")
        try:
            for surface, pinyin in keys:
                cur = self.con.execute(
                    "UPDATE words SET tier = ? WHERE surface = ? AND pinyin = ?",
                    (tier, surface, pinyin),
                )
                n += cur.rowcount
                if cur.rowcount:
                    self._event(int(time.time()), f"tier:{tier}", surface, pinyin, None)
            self.con.execute("COMMIT")
        except Exception:
            self._rollback()
            raise
        return n

    def set_override(self, keys: Iterable[tuple[str, str]], override: str | None) -> int:
        """记录**用户的显式判断**（这是事实，不是推断）。``None`` 表示撤销。"""
        if override is not None and override not in _OVERRIDES:
            raise ValueError(f"未知 override: {override}")
        n = 0
        now = int(time.time())
        self.con.execute("BEGIN")
        try:
            for surface, pinyin in keys:
                cur = self.con.execute(
                    "UPDATE words SET override = ?, last_evidence = ?"
                    " WHERE surface = ? AND pinyin = ?",
                    (override, now, surface, pinyin),
                )
                n += cur.rowcount
                if cur.rowcount:
                    self._event(now, f"override:{override or 'clear'}", surface, pinyin, None)
            self.con.execute("COMMIT")
        except Exception:
            self._rollback()
            raise
        return n

    def set_state(self, keys: Iterable[tuple[str, str]], state: str) -> int:
        if state not in _STATES:
            raise ValueError(f"未知 state: {state}")
        n = 0
        now = int(time.time())
        # dropped_at 必须和 state 保持一致，否则导出里会出现
        # "active 但 dropped_at 非空"这种自相矛盾的行。
        dropped_at = now if state == "dropped" else None
        self.con.execute("BEGIN")
        try:
            for surface, pinyin in keys:
                cur = self.con.execute(
                    "UPDATE words SET state = ?, dropped_at = ?, last_evidence = ?"
                    " WHERE surface = ? AND pinyin = ?",
                    (state, dropped_at, now, surface, pinyin),
                )
                n += cur.rowcount
                if cur.rowcount:
                    self._event(now, state, surface, pinyin, None)
            self.con.execute("COMMIT")
        except Exception:
            self._rollback()
            raise
        return n

    def delete(self, keys: Iterable[tuple[str, str]]) -> int:
        n = 0
        self.con.execute("BEGIN")
        try:
            for surface, pinyin in keys:
                cur = self.con.execute(
                    "DELETE FROM words WHERE surface = ? AND pinyin = ?", (surface, pinyin)
                )
                n += cur.rowcount
                if cur.rowcount:
                    self._event(int(time.time()), "purged", surface, pinyin, None)
            self.con.execute("COMMIT")
        except Exception:
            self._rollback()
            raise
        return n

    def add_manual(
        self,
        surface: str,
        pinyin: str,
        syllables: str = "",
        tier: str = "hot",
        source: str = "manual",
    ) -> None:
        now = int(time.time())
        self.con.execute(
            "INSERT INTO words (surface, pinyin, syllables, tier, source, state,"
            " override, born_at, last_evidence, strength)"
            " VALUES (?, ?, ?, ?, ?, 'active', 'confirmed', ?, ?, 1.0)"
            " ON CONFLICT(surface, pinyin) DO UPDATE SET tier = excluded.tier,"
            " source = excluded.source, override = 'confirmed',"
            " last_evidence = excluded.last_evidence",
            (surface, pinyin, syllables, tier, source, now, now),
        )
        self._event(now, source, surface, pinyin, None)

    def _rollback(self) -> None:
        with contextlib.suppress(sqlite3.Error):
            self.con.execute("ROLLBACK")

    # ------------------------------------------------------------------ 查询

    def rows(
        self,
        *,
        state: str | None = None,
        tier: str | None = None,
        source: str | None = None,
        confidence: str | None = None,
        limit: int | None = None,
        order: str = "last_evidence DESC",
        now: int | None = None,
        min_age_days: float = CONFIRM_MIN_AGE_DAYS,
    ) -> list[sqlite3.Row]:
        if order not in _ORDER_WHITELIST:
            raise ValueError(f"不支持的排序：{order}")
        clauses: list[str] = []
        params: list[Any] = []
        if state:
            clauses.append("state = ?")
            params.append(state)
        if tier:
            clauses.append("tier = ?")
            params.append(tier)
        if source:
            clauses.append("source = ?")
            params.append(source)
        sql = "SELECT * FROM words"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += f" ORDER BY {order}"
        result = list(self.con.execute(sql, params))

        if confidence:
            at = int(now if now is not None else time.time())
            result = [
                r
                for r in result
                if effective_confidence(r, at, min_age_days=min_age_days) == confidence
            ]
        if limit is not None and limit >= 0:
            result = result[:limit]
        return result

    def get(self, surface: str, pinyin: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.con.execute(
            "SELECT * FROM words WHERE surface = ? AND pinyin = ?", (surface, pinyin)
        ).fetchone()
        return row

    def find_by_surface(self, surface: str) -> list[sqlite3.Row]:
        return list(self.con.execute("SELECT * FROM words WHERE surface = ?", (surface,)))

    def confidence_counts(
        self, now: int | None = None, min_age_days: float = CONFIRM_MIN_AGE_DAYS
    ) -> dict[str, int]:
        at = int(now if now is not None else time.time())
        out = {k: 0 for k in _CONFIDENCE}
        for row in self.con.execute("SELECT * FROM words"):
            out[effective_confidence(row, at, min_age_days=min_age_days)] += 1
        return out

    def counts(self, now: int | None = None) -> dict[str, int]:
        out = {
            "total": 0,
            "active": 0,
            "dropped": 0,
            "archived": 0,
            "hot": 0,
            "manual": 0,
            "injected": 0,
        }
        for row in self.con.execute("SELECT state, COUNT(*) n FROM words GROUP BY state"):
            out[row["state"]] = row["n"]
            out["total"] += row["n"]
        for row in self.con.execute("SELECT COUNT(*) n FROM words WHERE tier = 'hot'"):
            out["hot"] = row["n"]
        for row in self.con.execute("SELECT source, COUNT(*) n FROM words GROUP BY source"):
            if row["source"] in ("manual", "injected"):
                out[row["source"]] = row["n"]
        out.update(self.confidence_counts(now))
        return out

    def last_runs(self, limit: int = 5) -> list[sqlite3.Row]:
        return list(self.con.execute("SELECT * FROM runs ORDER BY ts DESC LIMIT ?", (limit,)))

    def last_batches(self, limit: int = 5) -> list[sqlite3.Row]:
        return list(self.con.execute("SELECT * FROM batches ORDER BY id DESC LIMIT ?", (limit,)))

    def events(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(self.con.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)))

    def iter_all(self) -> Iterator[sqlite3.Row]:
        yield from self.con.execute("SELECT * FROM words")
