"""命令行入口。

设计原则：**每个动作都是一次性进程**。不做守护、不留常驻内存，
需要反复执行的部分交给 launchd。
"""

from __future__ import annotations

import argparse
import json
import plistlib
import re
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path

from . import __version__, decay, exporter, injector, launchd, ledger, native, promote
from .config import DEFAULT_HALF_LIFE_DAYS, Paths, resolve_paths

STATES = ("active", "dropped", "archived", "all")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2


# --------------------------------------------------------------------------- 输出


def _ts(value: int | None) -> str:
    if not value:
        return "-"
    return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M")


def _disp_width(text: str) -> int:
    """终端显示宽度：CJK 全角算 2 列。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int, align: str) -> str:
    gap = max(0, width - _disp_width(text))
    if not gap:
        return text
    return (" " * gap + text) if align == ">" else (text + " " * gap)


def _table(headers: list[str], rows: list[list[str]], aligns: str | None = None) -> str:
    if not rows:
        return "（无数据）"
    ncol = len(headers)
    width = [_disp_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            width[i] = max(width[i], _disp_width(str(cell)))
    align = (aligns or "").ljust(ncol, "<")
    lines = [
        "  ".join(_pad(h, width[i], align[i]) for i, h in enumerate(headers)).rstrip(),
        "  ".join("-" * w for w in width).rstrip(),
    ]
    for row in rows:
        lines.append(
            "  ".join(_pad(str(c), width[i], align[i]) for i, c in enumerate(row)).rstrip()
        )
    return "\n".join(lines)


def _err(msg: str) -> int:
    print(f"imehot: {msg}", file=sys.stderr)
    return EXIT_ERROR


# --------------------------------------------------------------------------- run


def cmd_run(args: argparse.Namespace, paths: Paths) -> int:
    started = time.perf_counter()

    fp = native.fingerprint(paths)
    if fp is None:
        return _err(
            "读不到原生输入法数据。请确认：\n"
            "  1. 系统设置 → 键盘 → 输入法 里已添加「简体拼音」；\n"
            "  2. 系统设置 → 隐私与安全性 → 完全磁盘访问权限 里已勾选本程序。"
        )

    if not args.force:
        try:
            previous = paths.fingerprint.read_text(encoding="utf-8").strip()
        except OSError:
            previous = ""
        if previous == fp:
            return EXIT_OK

    try:
        snap = native.read_snapshot(paths)
    except native.NativeAccessError as exc:
        return _err(str(exc))

    con = ledger.open_ledger(paths.db)
    try:
        lg = ledger.Ledger(con)
        diff = lg.apply(snap)
        elapsed = (time.perf_counter() - started) * 1000
        lg.record_run(elapsed, diff.changed, diff, reason="force" if args.force else "watch")
    finally:
        con.close()

    paths.fingerprint.write_text(fp, encoding="utf-8")
    injector.sweep_temp(paths)

    if not args.quiet:
        flag = "变化" if diff.changed else "无变化"
        extra = ""
        if diff.suspects:
            extra += f"  ⚠{diff.suspects} 疑似错字"
        print(
            f"✓ {flag}  +{diff.added} -{diff.dropped} ↑{diff.revived}  "
            f"共 {diff.total} 词 / {snap.tokens_typed} tokens  {elapsed:.0f}ms{extra}"
        )
        if args.verbose:
            print(f"  指纹 {fp}")
            print(f"  账本 {paths.db}")
            if diff.batch_id:
                print(f"  批次 #{diff.batch_id}")
    return EXIT_OK


# --------------------------------------------------------------------------- status


def cmd_status(args: argparse.Namespace, paths: Paths) -> int:
    print(f"imehot {__version__}   {paths.data_dir}")
    print()

    try:
        meta = native.read_meta_only(paths)
        n_words = len(native.read_snapshot(paths).words)
    except native.NativeAccessError as exc:
        print(f"原生输入法：不可读（{exc}）")
        n_words = 0
        meta = {}

    if meta:
        print("── 原生输入法 ──")
        rows = [
            ["累计上屏 tokens", str(meta.get("numberOfTokensTyped", "?"))],
            ["原生词库条数", str(n_words)],
            ["上次衰减", _ts_native(meta.get("lastDecay"))],
            ["上次修剪", _ts_native(meta.get("lastPruning"))],
            ["自上次衰减累计", str(meta.get("wordsSinceLastDecay", "?"))],
            ["适配版本", str(meta.get("adaptationVersion", "?"))],
        ]
        print(_table(["指标", "值"], rows))
        print()

    if paths.db.exists():
        con = ledger.open_ledger(paths.db)
        try:
            lg = ledger.Ledger(con)
            c = lg.counts()
            print("── 热词账本 ──")
            rows = [
                ["总词条", str(c["total"])],
                ["活跃 (active)", str(c["active"])],
                ["已被原生删除 (dropped)", str(c["dropped"])],
                ["已归档 (archived)", str(c["archived"])],
                ["热词库 (tier=hot)", str(c["hot"])],
                ["已确认 confirmed", str(c["confirmed"])],
                ["冷静期 candidate", str(c["candidate"])],
                ["疑似错字 suspect", str(c["suspect"])],
                ["已否决 rejected", str(c["rejected"])],
                ["手工添加", str(c["manual"])],
                ["注入", str(c["injected"])],
                ["数据库大小", f"{paths.db.stat().st_size / 1024:.1f} KiB"],
            ]
            print(_table(["指标", "值"], rows))
            runs = lg.last_runs(3)
            if runs:
                print()
                print("── 最近运行 ──")
                rrows = [
                    [
                        _ts(r["ts"]),
                        f"{r['duration_ms']:.0f}ms",
                        "变化" if r["changed"] else "无变化",
                        f"+{r['added']} -{r['dropped']} ↑{r['revived']}",
                        str(r["n_words"]),
                        r["reason"] or "",
                    ]
                    for r in runs
                ]
                print(_table(["时间", "耗时", "结果", "差异", "词数", "触发"], rrows))
        finally:
            con.close()
    else:
        print("── 热词账本 ──\n尚未初始化，先运行 `imehot run`。")

    print()
    st = launchd.status(paths)
    print("── 后台代理 ──")
    print(f"{'已加载' if st.loaded else '未安装/未加载'}  {st.detail}")
    if st.installed:
        print(f"plist: {paths.launch_agent}")
        if not paths.lm_dir.is_dir():
            print("⚠ 输入法语言模型目录不存在，代理会空转失败。")
        tail = _log_tail(paths.log, 5)
        if tail:
            print("最近日志：")
            for line in tail:
                print(f"  {line}")
        else:
            print("日志为空（正常）")
    else:
        print("运行 `imehot install` 安装。")
    return EXIT_OK


def _log_tail(path: Path, lines: int) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rows = [ln for ln in text.splitlines() if ln.strip()]
    return rows[-lines:]


def _ts_native(value: object) -> str:
    unix = native.cf_to_unix(value)
    return _ts(int(unix)) if unix else "-"


# --------------------------------------------------------------------------- words


def cmd_words(args: argparse.Namespace, paths: Paths) -> int:
    if not paths.db.exists():
        return _err("账本尚未初始化，先运行 `imehot run`。")
    now = int(time.time())
    con = ledger.open_ledger(paths.db)
    try:
        lg = ledger.Ledger(con)
        rows = lg.rows(
            state=None if args.state == "all" else args.state,
            tier=None if args.tier == "all" else args.tier,
            confidence=None if args.confidence == "all" else args.confidence,
            limit=args.limit,
            order="last_evidence DESC, surface ASC",
        )
        if args.json:
            payload = [
                {
                    "surface": r["surface"],
                    "pinyin": r["pinyin"],
                    "state": r["state"],
                    "tier": r["tier"],
                    "source": r["source"],
                    "confidence": ledger.effective_confidence(r, now),
                    "suspect_of": r["suspect_of"],
                    "born_at": r["born_at"],
                    "score": round(decay.score_row(r, now, args.half_life).value, 4),
                }
                for r in rows
            ]
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return EXIT_OK

        data: list[list[str]] = []
        for r in rows:
            sc = decay.score_row(r, now, args.half_life)
            data.append(
                [
                    decay.label(sc.value),
                    r["surface"],
                    r["pinyin"],
                    f"{sc.value:.2f}",
                    ledger.effective_confidence(r, now),
                    r["state"],
                    r["tier"],
                    _ts(r["born_at"]),
                ]
            )
        print(_table(["热", "词面", "拼音", "分数", "置信", "状态", "层级", "首次观测"], data))
        print(f"\n共 {len(rows)} 条")
    finally:
        con.close()
    return EXIT_OK


# --------------------------------------------------------------------------- promote


def cmd_promote(args: argparse.Namespace, paths: Paths) -> int:
    if not paths.db.exists():
        return _err("账本尚未初始化，先运行 `imehot run`。")
    con = ledger.open_ledger(paths.db)
    try:
        lg = ledger.Ledger(con)
        candidates = lg.rows(state="active")
        picked: list[tuple[str, str]] = []
        rejected: list[tuple[str, str]] = []
        skipped: dict[str, int] = {}

        allowed = {"confirmed", "candidate", "suspect"}
        if args.only_confirmed:
            allowed = {"confirmed"}
        if args.include_suspect:
            allowed.add("suspect")

        for r in candidates:
            if r["tier"] == "hot":
                continue
            conf = ledger.effective_confidence(r, int(time.time()))
            if conf not in allowed:
                skipped[conf] = skipped.get(conf, 0) + 1
                continue
            verdict = promote.judge(
                r["surface"],
                r["pinyin"],
                min_cjk=args.min_cjk,
                allow_english=args.include_english,
                min_ascii_len=args.min_ascii_len,
            )
            if verdict.ok:
                picked.append((r["surface"], r["pinyin"]))
            else:
                rejected.append((r["surface"], verdict.reason))

        print(f"检查 {len(candidates)} 条活跃词，可进热词库 {len(picked)} 条：")
        for s, p in picked:
            print(f"  + {s}\t{p}")
        if rejected:
            print(f"\n被质检挡住 {len(rejected)} 条：")
            for s, why in rejected[: args.show_rejected]:
                print(f"  - {s}\t{why}")
        if skipped:
            detail = "  ".join(f"{k}={v}" for k, v in sorted(skipped.items()))
            print(f"\n未达置信度门槛而跳过：{detail}")
            print(
                "  （suspect 用 `imehot review` 裁决；"
                "candidate 等冷静期结束，或加 --only-confirmed 反向筛选）"
            )

        if not args.apply:
            print("\n这是预演。确认无误后加 --apply 才写入热词库。")
            return EXIT_OK

        n = lg.set_tier(picked, "hot")
        print(f"\n✓ 已写入热词库 {n} 条")
        return EXIT_OK
    finally:
        con.close()


# --------------------------------------------------------------------------- review


def cmd_review(args: argparse.Namespace, paths: Paths) -> int:
    """列出疑似错字，等人裁决。"""
    if not paths.db.exists():
        return _err("账本尚未初始化，先运行 `imehot run`。")
    con = ledger.open_ledger(paths.db)
    try:
        lg = ledger.Ledger(con)
        suspects = lg.rows(confidence="suspect", order="born_at DESC")
        pending = lg.rows(confidence="candidate", order="born_at DESC")

        if not suspects and not pending:
            print("没有待裁决的词。")
            return EXIT_OK

        if suspects:
            print(f"── 疑似错字 {len(suspects)} 条（同一批输入里被后来的词取代）──")
            data = [
                [r["surface"], r["pinyin"], r["suspect_of"] or "-", _ts(r["born_at"])]
                for r in suspects
            ]
            print(_table(["词面", "拼音", "疑似应为", "首次观测"], data))
            print()
            print("  确认它是真词：imehot confirm <词面>")
            print("  确认它是错字：imehot reject <词面>")

        if pending:
            print(f"\n── 冷静期 {len(pending)} 条（持续存在后会自动转正）──")
            data = [
                [r["surface"], r["pinyin"], str(r["seen_runs"]), _ts(r["born_at"])]
                for r in pending[: args.limit]
            ]
            print(_table(["词面", "拼音", "已观测", "首次观测"], data))
        return EXIT_OK
    finally:
        con.close()


def cmd_confirm(args: argparse.Namespace, paths: Paths) -> int:
    return _set_override(args, paths, "confirmed", "确认")


def cmd_reject(args: argparse.Namespace, paths: Paths) -> int:
    code = _set_override(args, paths, "rejected", "否决")
    if code == EXIT_OK and args.archive:
        con = ledger.open_ledger(paths.db)
        try:
            lg = ledger.Ledger(con)
            keys = [(r["surface"], r["pinyin"]) for r in lg.find_by_surface(args.surface)]
            if keys:
                lg.set_state(keys, "archived")
        finally:
            con.close()
    return code


def _set_override(args: argparse.Namespace, paths: Paths, override: str | None, verb: str) -> int:
    """记录用户的显式判断。confidence 本身是派生视图，不直接写。"""
    if not paths.db.exists():
        return _err("账本尚未初始化，先运行 `imehot run`。")
    con = ledger.open_ledger(paths.db)
    try:
        lg = ledger.Ledger(con)
        rows = lg.find_by_surface(args.surface)
        if not rows:
            return _err(f"账本里没有「{args.surface}」")
        if len(rows) > 1 and not args.all:
            listing = ", ".join(f"{r['surface']}/{r['pinyin']}" for r in rows)
            return _err(f"「{args.surface}」对应多条记录：{listing}；用 --all 全部处理")
        keys = [(r["surface"], r["pinyin"]) for r in rows]
        n = lg.set_override(keys, override)
        print(f"✓ 已{verb} {n} 条：{args.surface}")
        return EXIT_OK
    finally:
        con.close()


# --------------------------------------------------------------------------- prune


def cmd_prune(args: argparse.Namespace, paths: Paths) -> int:
    if not paths.db.exists():
        return _err("账本尚未初始化，先运行 `imehot run`。")
    if args.hard and not args.experimental:
        return _err("--hard 会改写原生词库，必须同时给出 --experimental。")

    now = int(time.time())
    con = ledger.open_ledger(paths.db)
    try:
        lg = ledger.Ledger(con)
        targets: list[sqlite3.Row] = []
        for r in lg.rows(state="dropped"):
            sc = decay.score_row(r, now, args.half_life)
            stale = sc.idle_days >= args.older_than
            cold = args.min_score > 0 and sc.value <= args.min_score
            if stale or cold:
                targets.append(r)

        if not targets:
            print(f"没有需要清理的词（已删除且闲置 ≥{args.older_than} 天的词为 0）")
            return EXIT_OK

        keys = [(r["surface"], r["pinyin"]) for r in targets]
        print(f"{'将' if args.dry_run else '已'}清理 {len(targets)} 条：")
        for r in targets[: args.show]:
            sc = decay.score_row(r, now, args.half_life)
            print(
                f"  {decay.label(sc.value)} {r['surface']}"
                f"\t{r['pinyin']}\t闲置 {sc.idle_days:.0f} 天"
            )
        if len(targets) > args.show:
            print(f"  … 另 {len(targets) - args.show} 条")

        if args.dry_run:
            return EXIT_OK

        if args.hard:
            # 账本里的 dropped 是**过去**的状态，原生词库现在可能已经把它学回来了。
            # 删之前必须重新读一次现状，否则会把用户刚重新用起来的词删掉。
            try:
                live = {(w.surface, w.pinyin) for w in native.read_snapshot(paths).words}
            except native.NativeAccessError as exc:
                return _err(f"无法读取当前原生词库，拒绝执行 --hard：{exc}")

            # 只有「仍在原生词库」的词才需要从原生删除；
            # 已经不在的词本来就没东西可删（早期版本会对它们空转一次 replace）。
            to_remove = [k for k in keys if k in live]
            if to_remove and not args.force:
                detail = "、".join(f"{s}/{p}" for s, p in to_remove[:5])
                return _err(
                    f"有 {len(to_remove)} 个词现在仍在原生词库里（{detail}…）。\n"
                    "  它们可能刚被你重新用起来。加 --force 才会删掉它们。"
                )
            if to_remove:
                if injector.ime_running():
                    if not args.restart_ime:
                        return _err("--hard 需要先停掉输入法扩展，请加 --restart-ime。")
                    if not injector.stop_ime():
                        return _err("无法停止输入法扩展。")
                    print("  已停止输入法扩展")
                removed, bak = injector.remove(paths, to_remove, dry_run=False)
                print(f"  原生词库删除 {removed} 行，备份 {bak}")
                if injector.ime_running() and not injector.wait_ime_back():
                    print("  ⚠ 输入法尚未自动恢复：切一下输入源，或注销重登。")
            else:
                print("  这些词已不在原生词库中，无需重写原生文件")

        # 账本侧的归档/删除始终针对**全部**目标，不受 to_remove 过滤影响
        if args.purge:
            n = lg.delete(keys)
            print(f"✓ 从账本彻底删除 {n} 条")
        else:
            n = lg.set_state(keys, "archived")
            print(f"✓ 归档 {n} 条（记录保留，可追溯）")
        return EXIT_OK
    finally:
        con.close()


# --------------------------------------------------------------------------- export


def cmd_export(args: argparse.Namespace, paths: Paths) -> int:
    if not paths.db.exists():
        return _err("账本尚未初始化，先运行 `imehot run`。")
    now = int(time.time())
    con = ledger.open_ledger(paths.db)
    try:
        lg = ledger.Ledger(con)
        rows = lg.rows(
            state=None if args.state == "all" else args.state,
            tier=None if args.tier == "all" else args.tier,
            confidence=None if args.confidence == "all" else args.confidence,
            order="last_evidence DESC, surface ASC",
        )
        if args.confidence != "all":
            excluded = [
                r
                for r in lg.rows(
                    state=None if args.state == "all" else args.state,
                    tier=None if args.tier == "all" else args.tier,
                )
                if ledger.effective_confidence(r, now) != args.confidence
            ]
            if excluded:
                buckets: dict[str, int] = {}
                for r in excluded:
                    key = ledger.effective_confidence(r, now)
                    buckets[key] = buckets.get(key, 0) + 1
                detail = " ".join(f"{k}={v}" for k, v in sorted(buckets.items()))
                print(
                    f"已排除其他置信度的词条（{detail}）。用 --confidence all 包含全部。",
                    file=sys.stderr,
                )
        out = Path(args.output).expanduser() if args.output else None
        n = exporter.export(rows, args.format, now, out, args.half_life)
        if out:
            print(f"✓ 导出 {n} 条 → {out}", file=sys.stderr)
        return EXIT_OK
    finally:
        con.close()


# --------------------------------------------------------------------------- inject


def cmd_inject(args: argparse.Namespace, paths: Paths) -> int:
    if not args.experimental:
        return _err("注入会改写原生词库，必须显式给出 --experimental。")
    src = Path(args.file).expanduser()
    if not src.exists():
        return _err(f"找不到文件 {src}")

    wanted = _load_pairs(src)
    if not wanted:
        return _err("输入里没有可用的 (词面, 拼音) 对。")

    result = injector.plan(paths, wanted)
    print(f"计划注入 {len(result.planned)} 条，跳过 {len(result.skipped)} 条")
    for item in result.planned[: args.show]:
        print(f"  + {item.surface}\t{item.pinyin}\tlen={item.syllables}\tid={item.token_id}")
    for surface, why in result.skipped[: args.show]:
        print(f"  - {surface}\t{why}")

    if args.dry_run:
        injector.apply_plan(paths, result, dry_run=True)
        print("\n(dry-run，未落盘)")
        return EXIT_OK

    # 关键：运行中的输入法会用自己的内存状态整体回写 lexdb，把注入内容冲掉。
    # 所以写入前必须让它先退出，由 launchd 在下次需要时按需重启。
    was_running = injector.ime_running()
    if was_running:
        if not args.restart_ime:
            return _err(
                "输入法扩展正在运行，直接写入会在几秒内被它覆写。\n"
                "  加 --restart-ime 让 imehot 先停掉它再写入（launchd 会按需自动拉起）。"
            )
        if not injector.stop_ime():
            return _err("无法停止输入法扩展。")
        print("已停止输入法扩展（会丢掉正在组字的内容；launchd 会在需要时拉起）")

    result = injector.apply_plan(paths, result, dry_run=False)
    if not result.applied:
        print("无需写入。")
        return EXIT_OK

    con = ledger.open_ledger(paths.db)
    try:
        lg = ledger.Ledger(con)
        for item in result.planned:
            # 一次性写对 source，不要先写 manual 再 UPDATE —— 两条 autocommit
            # 语句之间被中断，这行就会永久卡在 'manual'，每轮都被当成新词。
            lg.add_manual(item.surface, item.reading, item.syllables, tier="hot", source="injected")
    finally:
        con.close()

    print(f"\n✓ 已注入 {len(result.planned)} 条")
    for b in result.backups:
        print(f"  备份 {b}")
    print("  提示：重启输入法（或注销）后生效；输入法下次落盘时可能覆盖未使用的词。")
    if not injector.wait_ime_back():
        print("  ⚠ 输入法尚未自动恢复：切一下输入源，或注销重登。")
    return EXIT_OK


def _load_pairs(path: Path) -> list[tuple[str, str]]:
    """从 plist 或 TSV 读取 ``(词面, 拼音)``。"""
    raw = path.read_bytes()
    pairs: list[tuple[str, str]] = []
    if raw.lstrip().startswith(b"<?xml") or raw.startswith(b"bplist"):
        try:
            data = plistlib.loads(raw)
        except Exception:
            data = None
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    phrase = item.get("phrase") or item.get("with") or ""
                    shortcut = item.get("shortcut") or item.get("replace") or ""
                    if phrase and shortcut:
                        pairs.append((str(phrase), str(shortcut)))
            return pairs

    text = raw.decode("utf-8", "replace")
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = re.split(r"\t+", line.rstrip("\n"))
        if len(parts) >= 2 and parts[0] not in ("surface", "phrase"):
            pairs.append((parts[0].strip(), parts[1].strip()))
    return pairs


# --------------------------------------------------------------------------- decay


def cmd_decay(args: argparse.Namespace, paths: Paths) -> int:
    if not args.experimental:
        return _err("改写 meta.dat 属于实验性操作，必须显式给出 --experimental。")

    # fail closed：输入法在跑的时候改 meta.dat 会被它整体回写覆盖，
    # 而且它一旦真的执行修剪，删的是 lexdb 里的词 —— 不能"先写了再说"。
    if not args.dry_run and injector.ime_running():
        if not args.restart_ime:
            return _err(
                "输入法正在运行，改写 meta.dat 会被它覆盖。\n"
                "  加 --restart-ime 让 imehot 先停掉它（会丢掉正在组字的内容）。"
            )
        if not injector.stop_ime():
            return _err("无法停止输入法扩展。")
        print("已停止输入法扩展")

    try:
        changed, backups = injector.force_decay(paths, dry_run=args.dry_run, days=args.days)
    except (OSError, native.NativeAccessError) as exc:
        return _err(str(exc))

    print("meta.dat 字段变更：")
    for key, (before, after) in changed.items():
        print(f"  {key}: {before} → {after}")
    if args.dry_run:
        print("\n(dry-run，未落盘)")
        return EXIT_OK

    for b in backups:
        print(f"  备份 {b}")
    print("\n✓ 已原子写入并回验")
    print(
        "  ⚠ 下一次原生落盘会执行一次衰减/修剪，**可能删除 lexdb 里的词**。\n"
        "    lexdb 已一并备份（见上），如需回滚请从 backups/ 取。"
    )
    if injector.ime_running() and not injector.wait_ime_back():
        print("  ⚠ 输入法尚未自动恢复：切一下输入源，或注销重登。")
    return EXIT_OK


# --------------------------------------------------------------------------- install


def cmd_install(args: argparse.Namespace, paths: Paths) -> int:
    paths.ensure()

    # WatchPaths 指向不存在的路径时 launchd 无法建立监视，只会每 30 分钟空转一次，
    # 用户在 status 里看到的却是"已加载"。宁可在安装时直接拒绝。
    if not paths.lm_dir.is_dir():
        return _err(
            f"找不到输入法语言模型目录：{paths.lm_dir}\n"
            "  请先在 系统设置 → 键盘 → 输入法 里添加「简体拼音」。"
        )

    interpreter = args.interpreter
    if interpreter:
        version = launchd.python_version(interpreter)
        if not version or version < launchd.MIN_PYTHON:
            return _err(
                f"{interpreter} 不可用或版本低于 {launchd.MIN_PYTHON[0]}.{launchd.MIN_PYTHON[1]}"
            )
    else:
        try:
            interpreter, version = launchd.pick_interpreter()
        except RuntimeError as exc:
            return _err(str(exc))
        print(f"使用解释器：{interpreter}  ({version[0]}.{version[1]})")

    try:
        path = launchd.install(
            paths,
            throttle=args.throttle,
            start_interval=args.interval,
            interpreter=interpreter,
        )
    except RuntimeError as exc:
        return _err(str(exc))
    print(f"✓ 已安装 launchd 代理：{path}")
    print(f"  监听：{paths.lm_dir}")
    print(f"  节流：{args.throttle}s   兜底轮询：{args.interval}s（命中指纹即秒退）")
    print("  登录自启：已在 ~/Library/LaunchAgents，登录时由 launchd 自动加载并立即跑一次")
    print()
    print("  ⚠ 还需要一步手动授权，否则后台任务读不到词库（不会弹窗，只会静默失败）：")
    print("     系统设置 → 隐私与安全性 → 完全磁盘访问权限 → + → ⌘⇧G 粘贴：")
    print(f"     {interpreter}")
    print("     授权后注销重登（或重启）生效。")
    st = launchd.status(paths)
    print(f"  状态：{st.detail}")
    return EXIT_OK


def cmd_uninstall(args: argparse.Namespace, paths: Paths) -> int:
    removed = launchd.uninstall(paths)
    print("✓ 已卸载 launchd 代理" if removed else "代理本就不存在")

    if not args.purge:
        if args.purge_backups:
            print("  提示：--purge-backups 需要同时给 --purge")
        return EXIT_OK

    ok, why = paths.purge_is_safe()
    if not ok:
        return _err(f"拒绝清理数据目录：{why}")

    import shutil

    if not paths.data_dir.exists():
        return EXIT_OK

    deletable = ["state.db", "state.db-wal", "state.db-shm", "fingerprint", "imehot.log"]
    targets: list[Path] = []
    for name in deletable:
        p = paths.data_dir / name
        if p.exists():
            targets.append(p)
    for pattern in ("lexdb-*.dat", "lexdb-work-*.dat", "meta.dat.tmp.*"):
        targets.extend(paths.data_dir.glob(pattern))
    if args.purge_backups and paths.backups.is_dir():
        targets.append(paths.backups)
    if paths.exports.is_dir():
        targets.append(paths.exports)

    if args.dry_run:
        for p in targets:
            print(f"  (dry-run) 将删除 {p}")
        return EXIT_OK

    kept_backups = False
    for p in targets:
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)
    if paths.backups.is_dir():
        kept_backups = True

    print(f"✓ 已清理账本与临时文件：{paths.data_dir}")
    if kept_backups:
        print(f"  保留了备份目录 {paths.backups}（里面是原生词库副本）")
        print("  确认不再需要回滚后，再执行：imehot uninstall --purge --purge-backups")
    return EXIT_OK


# --------------------------------------------------------------------------- doctor


def cmd_restore(args: argparse.Namespace, paths: Paths) -> int:
    infos = injector.list_backups(paths)
    if args.list or not infos:
        if not infos:
            print("没有任何备份。")
            return EXIT_OK
        data = [
            [
                _ts(int(i.path.stat().st_mtime)),
                i.kind,
                f"{i.size / 1024:.0f} KiB",
                "✓" if i.valid else "✗",
                i.detail,
                i.path.name,
            ]
            for i in infos
        ]
        print(_table(["时间", "类型", "大小", "可用", "内容", "文件"], data))
        if args.list:
            return EXIT_OK
        return _err("没有可用的备份（或用 --list 查看全部）")

    usable = [i for i in infos if i.valid]
    if not usable:
        return _err("备份都存在但都无法通过验证，拒绝恢复。")

    chosen = usable[0]
    if args.from_file:
        match = [i for i in infos if i.path.name == args.from_file]
        if not match:
            return _err(f"找不到备份 {args.from_file}")
        chosen = match[0]
    if not args.experimental:
        return _err(
            f"将用 {chosen.path.name}（{chosen.detail}）覆盖原生词库。\n"
            "  这是实验性操作，请加 --experimental 确认。"
        )

    if not args.dry_run and injector.ime_running():
        if not injector.stop_ime():
            return _err("无法停止输入法扩展。")
        print("已停止输入法扩展")

    try:
        target = injector.restore(paths, chosen.path, dry_run=args.dry_run)
    except (OSError, native.NativeAccessError) as exc:
        return _err(str(exc))

    if args.dry_run:
        print(f"(dry-run) 将用 {chosen.path.name} 覆盖 {target}")
        return EXIT_OK

    print(f"✓ 已从 {chosen.path.name} 恢复到 {target}")
    print("  恢复前的当前文件也做了一份备份，见 imehot restore --list")
    if not injector.wait_ime_back():
        print("  ⚠ 输入法尚未自动恢复：切一下输入源，或注销重登。")
    return EXIT_OK


def cmd_clean(args: argparse.Namespace, paths: Paths) -> int:
    """清理临时副本与旧备份 —— 这个工具本身不该占地方。"""
    if not paths.data_dir.exists():
        print("数据目录不存在，无需清理。")
        return EXIT_OK

    before = _dir_size(paths.data_dir)

    temps = injector.sweep_temp(paths, max_age_seconds=0 if args.all_temp else 3600)
    print(f"临时副本：清理 {temps} 个")

    if args.backups and paths.backups.is_dir():
        cutoff = time.time() - args.keep_days * 86_400
        kept = 0
        doomed: list[tuple[Path, int]] = []
        for item in sorted(paths.backups.iterdir()):
            try:
                stat = item.stat()
            except OSError:
                continue
            if item.is_file() and stat.st_mtime >= cutoff:
                kept += 1
            elif item.is_file():
                doomed.append((item, stat.st_size))

        freed = sum(size for _, size in doomed)
        if args.dry_run:
            for item, _ in doomed:
                print(f"  (dry-run) 将删除 {item.name}")
        else:
            for item, _ in doomed:
                item.unlink(missing_ok=True)
        verb = "将删除" if args.dry_run else "删除"
        print(
            f"备份：保留 {kept} 个（{args.keep_days:g} 天内），"
            f"{verb} {len(doomed)} 个，释放 {freed / 1024:.0f} KiB"
        )

    after = _dir_size(paths.data_dir)
    print(f"\n数据目录 {before / 1024:.0f} KiB → {after / 1024:.0f} KiB")
    return EXIT_OK


def _dir_size(root: Path) -> int:
    total = 0
    for p in root.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def cmd_doctor(args: argparse.Namespace, paths: Paths) -> int:
    checks: list[tuple[str, bool, str]] = []

    cur = ".".join(map(str, sys.version_info[:2]))
    checks.append(
        (
            f"Python ≥ {launchd.MIN_PYTHON[0]}.{launchd.MIN_PYTHON[1]}",
            sys.version_info[:2] >= launchd.MIN_PYTHON,
            f"当前进程 {cur} ({sys.executable})",
        )
    )

    try:
        exe, version = launchd.pick_interpreter()
        checks.append(("后台代理可用解释器", True, f"{exe} ({version[0]}.{version[1]})"))
    except RuntimeError as exc:
        checks.append(("后台代理可用解释器", False, str(exc).splitlines()[0]))

    checks.append(("原生语言模型目录", paths.lm_dir.is_dir(), str(paths.lm_dir)))
    fp = native.fingerprint(paths)
    checks.append(("lexdb.dat / meta.dat 可读", fp is not None, fp or "读取失败"))

    meta_ok, meta_detail = False, ""
    if fp:
        try:
            meta = native.read_meta_only(paths)
            meta_ok = True
            meta_detail = f"tokens={meta.get('numberOfTokensTyped')}"
        except native.NativeAccessError as exc:
            meta_detail = str(exc)
    checks.append(("meta.dat 可解析", meta_ok, meta_detail))

    lex_ok, lex_detail = False, ""
    if fp:
        try:
            snap = native.read_snapshot(paths)
            lex_ok = True
            lex_detail = f"{len(snap.words)} 词"
        except native.NativeAccessError as exc:
            lex_detail = str(exc).splitlines()[0]
    checks.append(("lexdb.dat 可解析", lex_ok, lex_detail))

    # 单独测"能否真的读出内容"：stat 成功但 read 被 TCC 拦，是很常见的状态，
    # 而它恰恰意味着后台代理会静默失效。
    read_ok, read_detail = False, ""
    try:
        with paths.lexdb.open("rb") as fh:
            n = len(fh.read(64))
        read_ok = n > 0
        read_detail = f"读到 {n} 字节"
    except PermissionError:
        read_detail = f"被 TCC 拦截；需把 {sys.executable} 加入完全磁盘访问权限"
    except OSError as exc:
        read_detail = str(exc)
    checks.append(("词库内容可读（TCC）", read_ok, read_detail))

    writable = True
    try:
        paths.ensure()
        probe = paths.data_dir / ".probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        writable = False
        lex_detail = str(exc)
    checks.append(("数据目录可写", writable, str(paths.data_dir)))

    st = launchd.status(paths)
    checks.append(("launchd 代理", st.loaded, st.detail))

    if st.installed:
        try:
            payload = plistlib.loads(paths.launch_agent.read_bytes())
            agent_py = str(payload["ProgramArguments"][0])
        except (OSError, KeyError, ValueError, plistlib.InvalidFileException):
            agent_py = ""
        ver = launchd.python_version(agent_py) if agent_py else None
        checks.append(
            (
                "代理固定的解释器仍可用",
                ver is not None and ver >= launchd.MIN_PYTHON,
                f"{agent_py or '无法读取 plist'} → {'.'.join(map(str, ver)) if ver else '不可用'}",
            )
        )

    print(_table(["检查项", "", "详情"], [[n, "✓" if ok else "✗", d] for n, ok, d in checks]))
    failed = [n for n, ok, _ in checks if not ok]
    print()
    if failed:
        print(f"未通过：{', '.join(failed)}")
        if any("可读" in n or "可解析" in n for n in failed):
            print(
                "\n若提示权限问题：系统设置 → 隐私与安全性 → 完全磁盘访问权限，"
                "把你的终端/Python 解释器加进去。"
            )
        return EXIT_ERROR
    print("全部通过。")
    return EXIT_OK


# --------------------------------------------------------------------------- 解析器


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="imehot",
        description="低侵入、零常驻的热词保管员：观测 macOS 原生拼音输入法的学习结果。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="数据从不离开本机。",
    )
    p.add_argument("--version", action="version", version=f"imehot {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name: str, help_: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help_, description=help_)

    r = add("run", "采样一次原生状态并写入账本（launchd 调用的就是它）")
    r.add_argument("--quiet", action="store_true", help="静默，不输出")
    r.add_argument("--force", action="store_true", help="忽略指纹，强制重采样")
    r.add_argument("-v", "--verbose", action="store_true", help="输出耗时与指纹细节")
    r.set_defaults(func=cmd_run)

    s = add("status", "总览：原生状态 + 账本统计 + 代理状态")
    s.set_defaults(func=cmd_status)

    w = add("words", "列出词条")
    w.add_argument("-s", "--state", default="active", choices=STATES)
    w.add_argument("-t", "--tier", default="all", choices=["ledger", "hot", "all"])
    w.add_argument(
        "-c",
        "--confidence",
        default="all",
        choices=["suspect", "candidate", "confirmed", "rejected", "all"],
    )
    w.add_argument("-n", "--limit", type=int, default=50)
    w.add_argument("--half-life", type=float, default=DEFAULT_HALF_LIFE_DAYS)
    w.add_argument("--json", action="store_true")
    w.set_defaults(func=cmd_words)

    pr = add("promote", "把质检合格且已确认的词提升进热词库（默认只预演）")
    pr.add_argument("--min-cjk", type=int, default=2)
    pr.add_argument("--min-ascii-len", type=int, default=3)
    pr.add_argument("--include-english", action="store_true", help="同时收录英文词")
    pr.add_argument("--only-confirmed", action="store_true", help="只处理已过冷静期的词")
    pr.add_argument("--include-suspect", action="store_true", help="包含疑似错字（不推荐）")
    pr.add_argument("--apply", action="store_true", help="真正写入热词库")
    pr.add_argument("--show-rejected", type=int, default=10)
    pr.set_defaults(func=cmd_promote)

    rv = add("review", "裁决疑似错字与冷静期词条")
    rv.add_argument("--limit", type=int, default=15)
    rv.set_defaults(func=cmd_review)

    for name, func, help_ in (
        ("confirm", cmd_confirm, "把词标为已确认（它是真词）"),
        ("reject", cmd_reject, "把词标为已否决（它是错字）"),
    ):
        cp = add(name, help_)
        cp.add_argument("surface", help="词面")
        cp.add_argument("--all", action="store_true", help="同词面多条记录时全部处理")
        if name == "reject":
            cp.add_argument(
                "--no-archive", dest="archive", action="store_false", help="只否决，不归档"
            )
            cp.set_defaults(archive=True)
        cp.set_defaults(func=func)

    pn = add("prune", "清理被原生删除且长期闲置的词")
    pn.add_argument("--older-than", type=float, default=180.0, help="闲置天数阈值（默认 180）")
    pn.add_argument("--min-score", type=float, default=0.0, help="分数低于此值也清理（0=关闭）")
    pn.add_argument("--half-life", type=float, default=DEFAULT_HALF_LIFE_DAYS)
    pn.add_argument("--purge", action="store_true", help="从账本彻底删除，而非归档")
    pn.add_argument("--hard", action="store_true", help="同时从原生词库删除（实验性）")
    pn.add_argument("--restart-ime", action="store_true", help="--hard 前先停掉输入法扩展")
    pn.add_argument("--force", action="store_true", help="即使该词仍存在于原生词库也删除")
    pn.add_argument("--experimental", action="store_true")
    pn.add_argument("--dry-run", action="store_true")
    pn.add_argument("--show", type=int, default=20)
    pn.set_defaults(func=cmd_prune)

    ex = add("export", "导出为 tsv / json / plist / rime")
    ex.add_argument("-f", "--format", default="tsv", choices=list(exporter.FORMATS))
    ex.add_argument("-o", "--output")
    ex.add_argument("-s", "--state", default="active", choices=STATES)
    ex.add_argument("-t", "--tier", default="all", choices=["ledger", "hot", "all"])
    ex.add_argument(
        "-c",
        "--confidence",
        default="all",
        choices=["suspect", "candidate", "confirmed", "rejected", "all"],
        help="按置信度过滤；默认不排除任何词（confidence 是派生视图，见 ADR 0005）",
    )
    ex.add_argument("--half-life", type=float, default=DEFAULT_HALF_LIFE_DAYS)
    ex.set_defaults(func=cmd_export)

    inj = add("inject", "把词写回原生词库（实验性）")
    inj.add_argument("file", help="plist 或 TSV（词面<TAB>空格分隔拼音）")
    inj.add_argument("--experimental", action="store_true")
    inj.add_argument("--dry-run", action="store_true")
    inj.add_argument(
        "--restart-ime",
        action="store_true",
        help="写入前先停掉输入法扩展（否则会被它覆写）",
    )
    inj.add_argument("--show", type=int, default=20)
    inj.set_defaults(func=cmd_inject)

    dc = add("decay", "请求原生输入法执行一次衰减/修剪（实验性）")
    dc.add_argument("--experimental", action="store_true")
    dc.add_argument("--dry-run", action="store_true")
    dc.add_argument("--restart-ime", action="store_true", help="写入前先停掉输入法扩展")
    dc.add_argument("--days", type=int, default=400, help="把衰减指针往回拨多少天（默认 400）")
    dc.set_defaults(func=cmd_decay)

    ins = add("install", "安装 launchd 用户代理")
    ins.add_argument("--throttle", type=int, default=launchd.DEFAULT_THROTTLE_SECONDS)
    ins.add_argument("--interval", type=int, default=launchd.DEFAULT_START_INTERVAL)
    ins.add_argument("--interpreter", help="指定 Python 解释器路径")
    ins.set_defaults(func=cmd_install)

    un = add("uninstall", "卸载 launchd 用户代理")
    un.add_argument("--purge", action="store_true", help="同时清理账本与日志（默认保留备份）")
    un.add_argument(
        "--purge-backups",
        action="store_true",
        help="连 backups/ 里的原生词库备份一起删（不可恢复）",
    )
    un.add_argument("--dry-run", action="store_true", help="只看会删什么")
    un.set_defaults(func=cmd_uninstall)

    dr = add("doctor", "自检：权限、格式、代理状态")
    dr.set_defaults(func=cmd_doctor)

    cl = add("clean", "清理临时副本与旧备份")
    cl.add_argument("--backups", action="store_true", help="同时清理旧备份")
    cl.add_argument("--keep-days", type=float, default=30.0, help="备份保留天数（默认 30）")
    cl.add_argument("--all-temp", action="store_true", help="连一小时内的临时文件也删")
    cl.add_argument("--dry-run", action="store_true")
    cl.set_defaults(func=cmd_clean)

    rs = add("restore", "从 backups/ 恢复原生词库（实验性）")
    rs.add_argument("--list", action="store_true", help="只列出所有备份及可用性")
    rs.add_argument("--from", dest="from_file", help="指定备份文件名（默认用最新的可用备份）")
    rs.add_argument("--experimental", action="store_true")
    rs.add_argument("--dry-run", action="store_true")
    rs.set_defaults(func=cmd_restore)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = resolve_paths()
    try:
        return int(args.func(args, paths))
    except KeyboardInterrupt:
        return 130
    except native.NativeAccessError as exc:
        return _err(str(exc))
    except PermissionError as exc:
        return _err(f"权限不足：{exc}")
    except BrokenPipeError:
        return EXIT_OK
    except OSError as exc:
        return _err(f"文件操作失败：{exc}")
    except Exception as exc:
        # 后台代理的崩溃只会写进 imehot.log，如果连 traceback 都没有，
        # 用户永远不知道为什么它不工作。这里兜底并给出类型名。
        return _err(f"内部错误（{type(exc).__name__}）：{exc}")
