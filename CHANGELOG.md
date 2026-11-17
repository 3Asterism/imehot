# Changelog

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [0.2.0] - 2026-11-17

主题：**错字不应该变成常用词**。见 [ADR 0005](docs/adr/0005-typo-filtering.md)。

### Added

- `similarity` 模块：拼音模糊音归一化（`zh/z`、`ch/c`、`sh/s`、`n/l`、`in/ing`、
  `an/ang`、`eng/en`）+ Levenshtein，用于识别「先选错、再改对」的词对。
  参考 CN102122298A 的相似度分解，只取拼音输入法需要的部分。
- 账本新增**置信度**四态：`suspect` / `candidate` / `confirmed` / `rejected`。
- 账本新增 `batches` 表：每次采样新增的词构成一个批次，记录上屏 token 的进度。
- **纠正对检测**：同一批次内 TokenID 相邻（≤3）且读音高度相似的词对，
  把先学到的那个标为 `suspect`，并记录它「疑似应为」哪个词。
- **冷静期**：新词出生满 2 天（按**时间**算，不按采样次数）自动转正；
  被原生删掉又学回来的词直接转正。**`suspect` 不能靠冷静期转正**，只能人工确认或复活。
- 新命令：`imehot review`（裁决队列）、`imehot confirm`、`imehot reject`。
- 新命令：`imehot restore [--list] [--from FILE]` —— 从 `backups/` 回滚原生词库，
  恢复前会把"当前文件"再备份一次。
- 新命令：`imehot clean [--backups] [--keep-days N] [--all-temp]`。
- `promote` 默认改为**只预演**，必须 `--apply` 才写入；且默认只处理 `confirmed`。
- `export` 默认只导出 `confirmed`，排除时会打印桶统计。
- 垃圾词规则补齐（来自 ime-lexicon）：长度 >16 视为整句、以「的了/的吧/的是/…」
  收尾视为半句话。

### Changed

- **破坏性**：`promote --dry-run` 改为默认行为，`--apply` 才是写入。
- **破坏性**：`uninstall --purge` 默认**保留** `backups/`，删备份需再加 `--purge-backups`。
- schema 版本 1 → 2，老数据自动迁移并标记为 `confirmed`（不因新功能而消失）。
- `bin/imehot` 自动挑选满足版本要求的解释器（优先自装的 3.14），不再依赖 PATH。
- 后台代理改为把代码部署到 `~/Library/Application Support/IMEHot/lib`，
  不再直接指向仓库的 `src/`（后者在 TCC 保护的 `~/Documents` 下，会让进程卡死）。

### Fixed

- 旧库迁移时 `CREATE INDEX ... ON words(confidence)` 会在 `ALTER TABLE` 之前执行，
  导致 `no such column: confidence`。索引创建移到迁移之后。
- **`decay` 只备份 `meta.dat`**，而它的后果是原生删除 `lexdb.dat` 里的词 ——
  现在两者都备份。
- **`decay` 在输入法运行时直接写入并"提示可能覆盖"**，现在是 fail closed，必须 `--restart-ime`。
- **`uninstall --purge` 可能 `rmtree` 任意目录**（若 `IMEHOT_DATA_DIR` 指向 `$HOME`）。
  现在校验归属标记并拒绝危险路径。
- **`prune --hard` 可能删掉刚重新学会的 live 词**（它按过期账本删除）。
  现在删除前重新读一次原生词库，遇到 live 词默认拒绝，需 `--force`。
- **`stop_ime` 用 `pgrep -f`**，会匹配任何命令行里含 `SCIM_Extension` 的进程。
  改为 `pgrep -x` + 可执行文件路径二次校验。
- **`stop_ime` 只停不确认恢复**。新增 `wait_ime_back`，写完后等待并在未恢复时明确提示。
- **备份文件名按秒拼**，同一秒两次写回会覆盖前一份原始备份。改用 `mkstemp` 并 `fsync`。
- `force_decay` 的临时文件原先写在输入法容器目录里，崩溃会留下垃圾；改到数据目录。
- `apply_plan` / `remove` 在 `os.replace` 失败时残留临时副本；现在统一清理。
- `sweep_temp` 的 glob `lexdb-??????.dat` 匹配不到 `mkstemp` 的随机名，改为 `lexdb-*.dat`。
- `native.fingerprint` 把所有 `OSError` 压成 `None`；现在 TCC 拒绝会给出可操作提示。
- 代理在 launchd 下被 TCC 静默拒绝时难以诊断；`doctor` 新增「词库内容可读（TCC）」检查，
  `status` 新增日志尾部显示。
- 测试里 `test_decay.make_row` 泄漏 sqlite 连接；CI 现在用 `-W error::ResourceWarning`。

### Security

- 守卫测试从"只检查属性名"升级为同时检查 AST `Name` 与 `Attribute`，
  覆盖 `from x import stop_ime` 这类写法；扫描范围改为递归整个包；
  新增 `KeepAlive` 缺失断言与"代码不得位于 TCC 保护目录"断言。
- 明确记录：这些是 **denylist 冒烟测试，不是形式化证明**。
- `state.db` 权限 0600、数据目录 0700（之前是 0644/0755，多用户 Mac 上别人能读）。

### Design revision（代码审查后）

初版把 `confidence` 写成账本的一列，并让 `export` / `promote` 默认只看 `confirmed`。
审查判定这是方向性错误：**置信度是启发式推断，不是观测事实**，把它落库并让默认
导出依赖它，与「不编造观测不到的事实」冲突；而且冷静期依赖"词库发生变化"，
导致新装用户默认导出为空。

现在：

- 账本只存原始事实（`born_at` / `revivals` / `suspect_of` / `batch_id` / 用户 `override`）；
- 置信度由 `ledger.effective_confidence()` **查询时现算**，改阈值不用迁移数据；
- `export` 默认 `--confidence all`，并新增 `confidence` / `suspect_of` 两列便于复核；
- `promote` 默认包含 `candidate`，只跳过 `suspect`；
- 首批导入（一次新增 >12 词）不做纠正对判定。

## [0.1.0] - 2026-11-17

首个可用版本。目标不是功能齐全，而是把「不侵入」这件事用代码钉死。

### Added

- `native` 模块：只读解析原生简体拼音输入法的 `lexdb.dat`（SQLite）与
  `meta.dat`（bplist），刻意不解析私有的 `dynamic.dat`。
- `ledger` 模块：SQLite 账本，记录每个词的 `born_at` / `dropped_at` / `revivals`，
  外加 `events` 审计时间线与 `runs` 运行日志。
- `decay` 模块：半衰期指数衰减（默认 90 天），**只存事实、查询时现算**。
- `promote` 模块：可解释的词条质检，过滤 emoji / 单字 / 纯数字 / 短英文。
- `exporter` 模块：TSV / JSON / plist（原生「编辑自定义短语」可直接拖入）/ RIME 码表。
- `launchd` 模块：`WatchPaths` + `ThrottleInterval` + 兜底 `StartInterval`，
  `ProcessType=Background`、`LowPriorityIO`、`Nice 10`。
- `injector` 模块（实验性）：向 `lexdb.dat` 注入/删除词条、改写 `meta.dat`
  请求原生衰减。全部走备份 + 临时副本 + `integrity_check` + `os.replace` 原子替换。
- 指纹短路：`stat(mtime_ns, size)` 没变就直接退出，连 SQLite 都不打开。
- `doctor` 自检命令。

### Verified on real hardware

- macOS 27.2.1 (26B131) / Apple M2 / Python 3.12.9。
- 未变化触发 53 ms，有变化 61 ms，空闲 0 CPU / 0 内存。
- 原生输入法在运行时会用自己的内存状态整体回写 `lexdb.dat`，
  使外部写入在数秒内失效 —— 因此 `inject` 要求 `--restart-ime`。
- `SCIM_Extension` 忽略 `SIGTERM`，必须 `SIGKILL`。

### Security

- 无键盘监听、无剪贴板、无网络、无常驻进程。
- `tests/test_safety.py` 以文本 + AST 断言把上述约束固化进 CI。

[Unreleased]: https://github.com/3Asterism/imehot/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/3Asterism/imehot/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/3Asterism/imehot/releases/tag/v0.1.0
