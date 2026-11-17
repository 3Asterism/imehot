# imehot

> 低侵入、零常驻的「热词保管员」——观测 macOS 原生拼音输入法自己的学习结果，
> 维护一份可查询、可导出、可衰减的账本。

**不监听键盘。不联网。不常驻内存。不改输入法界面。**

---

## 它解决什么问题

同一个词，你每天都在打，但输入法就是不给你。

比如我（作者）经常要打 **回测**，输入 `huice` 得到的是：

```
1 回撤    2 回车    3 回车键    4 会车    5 汇册    6 会
```

**回测 根本不在候选里。** 因为它是行业黑话，不在系统词典里；
而原生输入法要「学到」它，你得先逐字选「回」再选「测」打出来一次。

问题是：**原生输入法到底记住什么了？我看不到、导不出、也管不了。**
它甚至会把学到的东西悄悄衰减掉，而你完全不知道发生过。

`imehot` 补的就是这一层：

| 能力 | 原生输入法 | imehot |
|---|---|---|
| 学习你的用词 | ✅ | （复用它的结果） |
| 看见学到了什么 | ❌ | ✅ |
| 导出 / 迁移到别的输入法 | ❌ | ✅ |
| 追溯每个词的生死时间线 | ❌ | ✅ |
| 明确控制清理 | ❌ | ✅ |

---

## 它**不做**什么

这一节比上面那节更重要。

| | imehot |
|---|---|
| 监听键盘事件（`CGEventTap` / `IOHIDManager`） | ❌ 永不 |
| 读取输入法候选窗口 / 截屏 / OCR | ❌ 永不 |
| 读写剪贴板 | ❌ 永不 |
| 任何网络请求 | ❌ 永不 |
| 常驻后台进程 | ❌ 从不 |
| 打断你打字 | ❌ 后台路径不会 |

`tests/test_safety.py` 会对源码做文本与 AST 断言，挡住明显的越界写法。
**但它是 denylist 冒烟测试，不是形式化证明** —— 详见下面「不会打断输入法」一节的第 4 点。

---

## 不会打断输入法

这是最容易被怀疑的点，所以单独列出来。**先说清楚哪些是测试保证的，哪些是设计约定。**

### 1. 后台代理对原生数据只读

launchd 唯一会执行的命令是：

```
python3 -m imehot run --quiet
```

它做的事：`copyfile(lexdb.dat → 临时目录)` → 只读打开副本 → 比对 → 写自己的数据库 → 退出。
**对 `~/Library/Containers/...` 下的文件只读**，不发信号、不加文件锁、不碰输入法进程。
（它当然会写自己的 `state.db` 和临时副本，那不叫"纯只读"。）

守它的测试：`tests/test_safety.py::DaemonIsReadOnlyTest`
- plist 的 `ProgramArguments` 末尾必须是 `-m imehot run --quiet`，且不含 `inject`/`prune`/`decay`；
- plist 不允许出现 `KeepAlive`（否则就不是一次性进程了）；
- `cmd_run()` 的 AST 里不允许出现 `stop_ime`/`ime_pids`/`kill`/`subprocess`/`signal`，
  同时检查 `Name` 和 `Attribute`（所以 `from .injector import stop_ime` 也会被抓到）。

### 2. 中断只发生在一次性命令里

| 命令 | 是否会停输入法 | 频率 |
|---|---|---|
| `run`（后台） | 不会 | 每次词库变化 |
| `status` / `words` / `export` / `promote` / `prune`（默认）/ `review` | 不会 | 手动 |
| `inject` | 需要 `--restart-ime`，否则**直接拒绝执行** | 一次性 |
| `prune --hard` | 需要 `--restart-ime` | 一次性 |
| `decay` | 需要 `--restart-ime`，否则**直接拒绝执行** | 一次性 |
| `restore` | 需要 `--restart-ime` 之外还会自行停一次 | 一次性 |

不带 `--restart-ime` 时，`inject` / `prune --hard` / `decay` 会**报错退出**而不是悄悄写入 ——
因为写了也会被输入法冲掉（见下方实测）。

### 3. 杀掉输入法会丢"正在组字"的内容

**这一条是真实代价，不是零成本。**

`SCIM_Extension` 被杀时，已经上屏到应用里的文字是安全的（那在应用的文本存储里），
但**还没上屏的拼音和候选会消失**。代码里没有"检测你正在打字"的能力，
所以唯一的防护是：这些命令必须显式加 `--restart-ime`，而且只在一次性设置时用。

写完之后程序会等最多 8 秒看输入法是否被 launchd 拉起；没起来会提示
"切一下输入源，或注销重登"，**不会**假装它一定恢复。

### 4. 这些测试是 denylist，不是形式化证明

`tests/test_safety.py` 挡住的是"顺手加一行 `os.kill`"这类事故，
挡不住刻意规避（`ctypes` 动态调用、`getattr` 拼字符串、子包里的新文件）。
真正的保证来自：代码量小（约 2600 行）+ 无第三方依赖 + 人工审查。

关于"不监听键盘、不联网"的守卫同样如此：它是**冒烟测试**，不是证明。
见 [ADR 0002](docs/adr/0002-no-keyboard-listeners.md) 的修订说明。

---

## 三条实测结论（架构的地基）

这些都不是推测，是在 macOS 27.2.1 / M2 MacBook Air 上跑出来的。

### ① 原生输入法的学习结果，是可读的

`~/Library/Containers/com.apple.inputmethod.SCIM/Data/Library/LanguageModeling/zh_Hans-dynamic.lm/`

| 文件 | 格式 | 内容 | 用不用 |
|---|---|---|---|
| `lexdb.dat` | **SQLite** | `Words(TokenID, Surface, PinyinReading, PinyinSyllableLengths)` | ✅ 读 |
| `meta.dat` | **bplist** | 累计上屏量、上次衰减/修剪时间 | ✅ 读 |
| `dynamic.dat` | 私有归档 | 统计模型 | ❌ 不解析 |
| `cache.dat` | 私有归档 | 缓存 | ❌ 不解析 |

`meta.dat` 解出来的样子：

```python
{
  'numberOfTokensTyped': 18208,
  'lastDecay':           816606348.79,   # CF 绝对时 → 2026-11-17
  'lastPruning':         0.0,
  'wordsSinceLastDecay': 618,
  'adaptationVersion':   '2.6',
}
```

**原生输入法自己就有衰减和修剪机制**，`lastDecay` / `lastPruning` / `wordsSinceLastDecay`
就是它的记账指针。`lexdb.dat` 里躺着 42 条它为我学到的词 —— 人名、英文、还有一堆 emoji。

> `dynamic.dat` 已确认**不含**可映射到 TokenID 的权重（头部第 4 个 uint32 恰好等于文件长度，
> TokenID 在其中几乎不出现），所以不去逆向它。见 [ADR 0001](docs/adr/0001-native-ime-as-source-of-truth.md)。

### ② 直接写 `lexdb.dat` 会被输入法在几秒内冲掉

实测时间线：

```
22:36:39  注入 回测 → lexdb.dat 43 行
22:36:43  输入法整体回写 lexdb.dat → 42 行，回测消失（dynamic.dat 同时长了 6.7 KB）
```

原因不是"lexdb 是派生的"，而是**运行中的输入法用内存状态覆盖了整份文件**。

反证：先 `SIGKILL` 输入法再注入，它重启后**没有重建** lexdb，注入的行完好保留。

所以写入有一条硬规则：**必须先让输入法退出**。这条规则已经内建到 `inject` 里。

### ③ `SIGTERM` 杀不掉它

```
killall SCIM_Extension      → 无效（PID 不变）
killall -9 SCIM_Extension   → 有效
```

`injector.stop_ime()` 因此用 `SIGKILL`，并轮询确认进程真的退出后再写入。

---

## 架构

```
              ┌──────────────────────────────────────────────┐
   原生输入法  │ lexdb.dat   SQLite：词面 + 拼音 + 音节长度    │
   （它的活）  │ meta.dat    bplist：累计量 / 上次衰减时刻      │
              │ dynamic.dat 私有归档（不解析）                │
              └───────────────┬──────────────────────────────┘
                              │  launchd WatchPaths（内核 kqueue，空闲 0 开销）
                              ▼
              ┌──────────────────────────────────────────────┐
   imehot run │ ① stat 两文件 → 拼指纹 → 与上次相同就退出     │  ← 绝大多数触发止步于此
   (一次性)   │ ② 复制 lexdb 到临时目录，只读打开              │
              │ ③ 与账本 diff：新增 / 消失 / 复活              │
              │ ④ 写账本（WAL）→ 退出，内存归零                │
              └───────────────┬──────────────────────────────┘
                              ▼
              ┌──────────────────────────────────────────────┐
   账本       │ words    born_at / dropped_at / revivals     │
   state.db   │ events   完整审计时间线                       │
              │ runs     每次有变化的运行的耗时与差异         │
              └───────────────┬──────────────────────────────┘
                              ▼
                 TSV / JSON / plist / RIME 用户码表
```

---

## 性能预算（实测，非估算）

M2 MacBook Air / macOS 27.2.1，同一台机器上跑 20 次取中位数：

| 场景 | Python 3.14 | Python 3.12 | 峰值内存 |
|---|---|---|---|
| 某一瞬间没有进程 | — | — | **0** |
| 被触发但文件没变（快速路径） | **52 ms** | 60 ms | ≈ 12 MB，瞬时 |
| 被触发且有变化 | ≈ 60 ms | ≈ 68 ms | ≈ 28 MB，瞬时 |
| `imehot status` | < 100 ms | < 100 ms | ≈ 30 MB |

**全天 CPU 的诚实算法**：`StartInterval = 1800` 意味着每天固定 48 次兜底唤醒，
即使一次都不变化也是 `48 × 52 ms ≈ 2.5 秒/天`；再叠加词库真正变化时的运行，
**实际约 2.5–4 秒/天**。

所以准确说法是「**空闲时没有进程**」而不是「全天 0 CPU」——
后者是常驻守护进程做不到的，前者才是这个设计真正的收益：
后台程序最贵的不是 CPU，是那几十 MB 你永远不知道它在干什么的内存。

这就是为什么选择 `WatchPaths` + 一次性进程。
详见 [ADR 0003](docs/adr/0003-launchd-watchpaths-over-polling.md)。

---

## 词是怎么"变旧"的（衰减模型）

参考了三家开源实现：

| 实现 | 机制 |
|---|---|
| **librime** | `d_new = d0 + d_old · exp((t_old − t_new)/200)`，指数遗忘 |
| **青简 Qingjian** | 90 天不打的词次数减半；**冷却只影响排序，记录永存** |
| **WindInput 清风** | 三层：24h 草稿层 → 5000 条临时库 → 永久用户词库 |

imehot 的取舍：**只存原始事实，分数查询时现算。**

```
score = strength · 0.5 ** (idle_days / 90)
```

账本里**没有** `score` 列。这样调整半衰期不用重写历史数据。

### 一个必须说清楚的限制

`lexdb.dat` 里**没有**每个词的使用次数和最后使用时间。

这意味着：**一个持续存在于原生词库里的词，我们无法知道它今天有没有被用过。**
给这种词编一条衰减曲线，会让"热度"看起来很精确，实际是伪造的。所以：

- `active` 的词 → 不衰减（原生还留着你，说明不是废词）
- `dropped` 的词 → **从原生删除那一刻**开始按半衰期衰减
- `archived` → 不计分

而"该不该删"这件事，最权威的判断在原生输入法自己手里。
所以我们提供三档清理，默认只做最轻的那档：

| 档位 | 动作 | 风险 | 命令 |
|---|---|---|---|
| **L0 软归档** | 从导出口径移出，记录保留可回溯 | 零 | `imehot prune` |
| **L1 原生修剪** | 把 `meta.dat` 的 `lastDecay`/`lastPruning` 往回拨 400 天，让输入法用**自己的模型**清理 | **高**：原生会**删除 `lexdb.dat` 里的词**。两者都会先备份，但删除只能手工回滚 | `imehot decay --experimental --restart-ime` |
| **L2 硬删除** | 直接从 `lexdb.dat` 删行 | 高：有备份；且会先核对词是否仍在原生词库，在的话默认拒绝 | `imehot prune --hard --experimental --restart-ime` |

默认只做 L0。真正「很久不用」的判断交给 L1 —— 因为那才是原生输入法自己的判断，
我们只是替用户按下它的开关。

### 任何写回都能回滚

```bash
imehot restore --list                      # 列出所有备份，含完整性校验和行数
imehot restore --experimental --dry-run    # 预演：用最新可用备份恢复
imehot restore --experimental              # 真恢复
```

恢复流程：验证备份（SQLite `integrity_check` / plist 可解析）→ 停输入法 →
**把"恢复前的现状"再备份一次** → 原子替换 → 恢复后再次验证。
所以恢复本身也是可回滚的。

备份用 `mkstemp` 生成唯一文件名，同一秒内连续两次写回不会互相覆盖。

---

## 环境要求

| 项目 | 要求 | 说明 |
|---|---|---|
| 系统 | macOS 13+ | 在 **macOS 27.2.1 (26B131)** 上实测 |
| 芯片 | Apple Silicon / Intel | 在 **M2 MacBook Air** 上实测；代码纯 Python，无原生扩展 |
| 输入法 | 已启用「简体拼音」 | 系统设置 → 键盘 → 输入法 |
| Python | **≥ 3.9** | 已在 **3.9.6 / 3.12.9 / 3.14.10** 上跑通全部测试 |
| 第三方包 | **无** | 只用标准库，`pip install` 一步都不需要 |
| 权限 | **完全磁盘访问权限** | 只有一个用途：读输入法词库，见下 |

> **Python 怎么选？** 装了 Python 3.14 就用 3.14（启动比 3.12 快约 8 ms，对本项目
> 的"被唤醒即秒退"路径有意义）。没有的话系统自带的 3.9 也能跑，不用装任何东西。
> `bin/imehot` 会自动按 `$IMEHOT_PYTHON` → 自装的 3.14 → `$PYTHON` → `python3`
> → `/usr/bin/python3` 的顺序挑第一个满足版本要求的。

> **为什么必须是「完全磁盘访问权限」？**
> 词库在 `~/Library/Containers/com.apple.inputmethod.SCIM/…`，属于沙盒容器，
> 从 macOS 10.15 起受 TCC 保护。注意一个坑：**从终端手动跑时，是终端替你持有权限；
> 后台代理跑时，是解释器自己**，两者不能互相顶替。所以如果你只要手动跑，
> 给终端授权就行；如果要开机自启，得给解释器授权（见下文）。

## 下载与启动

```bash
# 1. 取代码
git clone git@github.com:3Asterism/imehot.git
cd imehot

# 2. 装到 PATH（软链到 ~/.local/bin，不动任何系统文件）
./scripts/install.sh

# 3. 自检 —— 它会逐项告诉你缺什么
imehot doctor
```

`doctor` 会检查：Python 版本、可用解释器、输入法目录、**词库内容是否真的读得出来（TCC）**、
`meta.dat` 能否解析、数据目录可写、launchd 代理状态、代理固定的解释器是否还有效。

### 先手动跑一次

```bash
imehot run -v      # 采样一次
imehot status      # 看原生学到了什么、账本里有什么
imehot words -s all
```

## 开机自启

```bash
imehot install
```

安装后会做三件事：

1. 把代码复制到 `~/Library/Application Support/IMEHot/lib`（**不是**指向 `~/Documents`，
   原因见下方踩坑记录）；
2. 写 `~/Library/LaunchAgents/io.github.3asterism.imehot.plist`；
3. `launchctl bootstrap` 立即加载。

`RunAtLoad` 为真，所以**登录时 launchd 会自动加载并立刻跑一次**；之后由
`WatchPaths`（词库文件变动）+ `StartInterval`（30 分钟兜底）驱动。

### 还差一步手动授权

后台代理第一次跑会**静默失败**，因为 TCC 不会给后台进程弹授权框。授权一次即可：

> 系统设置 → 隐私与安全性 → **完全磁盘访问权限** → 点 `+` →
> 按 `⌘⇧G` 粘贴 `~/Library/Application Support/IMEHot` 下 plist 里记录的解释器路径
> （`imehot install` 会直接把它打印出来）→ 允许 → **注销重登**

装完 `imehot install` 会提示这条。没授权时 `imehot doctor` 的
「词库内容可读（TCC）」一行会明确标红。

### 验证它在工作

```bash
launchctl print gui/$(id -u)/io.github.3asterism.imehot | grep -E "state|runs|last exit"
imehot status        # 「最近运行」里应该出现新的 watch 记录
tail -f ~/Library/Application\ Support/IMEHot/imehot.log
```

正常时进程会在几十毫秒内退出，`last exit code = 0`，日志为空。

### 关掉自启

```bash
imehot uninstall              # 停止并删除代理与部署的代码
imehot uninstall --purge      # 再清掉账本与日志（**默认保留 backups/**）
imehot uninstall --purge --purge-backups   # 连原生词库备份一起删（不可恢复）
```

### 踩坑记录：为什么不能直接指向 `~/Documents`

第一版把 launchd 的 `PYTHONPATH` 直接指向仓库的 `src/`。结果进程启动后**永不退出**
（`last exit code = (never exited)`，`sample` 显示卡在
`Py_InitializeFromConfig → PyImport_ImportModule`）——因为 `~/Documents` 是 TCC 保护目录，
没有交互界面的后台进程读不到，也不会弹窗，只会一直等。

现在的做法是安装时把代码复制到 `~/Library/Application Support/IMEHot/lib`
（不在 TCC 名单里）。代价是**改了源码要重新 `imehot install`** 才会生效。

另一个相关发现：TCC 拦截词库读取时返回的是
`PermissionError: [Errno 1] Operation not permitted`，不是 `EACCES`，
而且**没有任何弹窗**。这也是为什么 `doctor` 里要单独测"读 64 字节"，
而不是只测文件是否存在。

## 命令一览

```bash
imehot run [-v] [--force]        # 采样一次（launchd 调用的就是它）
imehot status                    # 原生状态 + 账本统计 + 代理状态
imehot words [-s …] [-t …] [-c suspect|candidate|confirmed|rejected|all] [--json]
imehot review                    # 裁决疑似错字与冷静期词条
imehot confirm <词面>            # 「它是真词」
imehot reject  <词面>            # 「它是错字」，并归档
imehot promote [--apply]         # 质检后进热词库（默认只预演）
imehot export -f tsv|json|plist|rime [-o FILE] [-c confirmed|all]
imehot prune [--older-than 180] [--purge] [--hard] [--dry-run]
imehot inject FILE --experimental --restart-ime   # 写回原生词库
imehot decay --experimental [--restart-ime]       # 请求原生衰减
imehot install | uninstall [--purge] [--purge-backups] | doctor
imehot restore [--list] [--from FILE] --experimental
imehot clean [--backups] [--keep-days 30] [--all-temp] [--dry-run]
```

### 典型流程

```bash
# 1. 先看看原生学到了什么
imehot words -s all

# 2. 看看有没有需要裁决的疑似错字
imehot review

# 3. 质检 + 冷静期都过了，才写进热词库
imehot promote            # 预演，只列不写
imehot promote --apply    # 确认无误再落盘

# 4. 导出成原生「编辑自定义短语」能直接拖入的 plist
imehot export -f plist -o ~/Desktop/热词.plist

# 5. 或者导出给 RIME / 雾凇
imehot export -f rime -o ~/Desktop/imehot.userdb.txt
```

---

## 错字不会变成热词

这是最容易搞砸的地方：你打「说思路」打成「所思路」，`backspace` 删掉重打，
这个错字会不会被记成你的常用词？

**会进原生词库。** 苹果自己的专利 [US9703394](https://patentimages.storage.googleapis.com/42/2b/ee/c59862eef21491/US9703394.pdf) 说得很清楚：

> 用户拒绝了 "Gattaca" 改打 "Inception"，键盘模块会**降低** "Gattaca" 的概率，
> 但**不会完全移除**，只是递减其似然值。拒绝的方式包括多次敲退格键。

也就是说错字留在 `lexdb.dat` 里，只在另一个我们读不到的文件里被降权。
所以必须在 imehot 这一层认出来。

### 怎么认

`TokenID` 是单调递增的——**它就是输入法学习这些词的先后顺序**。
一次采样里新增的词构成一个批次，批内 TokenID 相邻的两个词，
就是输入法在极短时间内先后学到的，正好对应「先选错、再改对」。

判据：**同一批次 + TokenID 差 ≤3 + 拼音模糊音归一化后高度相似** → 前者标记为疑似错字。

模糊音归一化（`zh/z`、`ch/c`、`sh/s`、`n/l`、`in/ing`、`an/ang`、`en/eng`）之后：

```
所思路  suo si lu
所示录  suo shi lu   → sh→s    归一化后全是 suo si lu
说思路  shuo si lu   → sh→s
```

三个词归一化后完全相同，所以这个方法是有效的，不需要任何外部词典。

### 但不许自动删

真实同音词（`会`/`汇`、`回车`/`回测`）也会被算成一对。所以规则**只打问号**。

**置信度不是账本里的一列，而是查询时现算的派生视图。** 账本只存原始事实
（出生时刻、复活次数、是否成对被怀疑、以及你的显式判断），换算规则随时可改，
历史数据不用重写 —— 这和「衰减分数不预计算」是同一个理由。

| 置信度 | 怎么算出来的 | 默认进热词库吗 |
|---|---|---|
| `rejected` | 你显式否决 | 否 |
| `confirmed` | 你显式确认 / 复活过 / 出生已超过冷静期（2 天） | 是 |
| `candidate` | 新词，还在冷静期内 | 是（`promote` 默认包含） |
| `suspect` | 成对被怀疑是错字 | **否**，需 `imehot review` 裁决 |

`export` 默认**不按置信度过滤**（`--confidence all`），导出内容里带
`confidence` 和 `suspect_of` 两列，你可以自己复核。
理由很直接：在阈值还没用真实数据校准之前，不该让一个启发式悄悄决定你导出什么。

**四道防线**：纠正对检测（主动怀疑）→ 冷静期（按**时间**算，2 天）
→ `review` 决策队列 → `reject` 黑名单。

另外，一个词如果**被原生删掉之后又学回来**，说明你确实又用了它——
这种情况直接转正，也是 `suspect` 唯一的自动例外。

首批导入（一次新增超过 12 个词）**不做**判定：那是历史词一起进来，
TokenID 相邻只代表"很久以前先后学到"，不是同一次输入的先错后对。

设计记录见 [ADR 0005](docs/adr/0005-typo-filtering.md)。

## 隐私

- 数据全部在本机，`~/Library/Application Support/IMEHot/`（约 120 KiB）。
- 没有网络代码（`tests/test_safety.py::test_no_network` 断言源码里不出现 `urllib` / `requests` / `socket.`）。
- 不记录按键，所以也不存在"按键记录被泄漏"这回事 —— **它从来没被采集过**。
- `imehot uninstall --purge` 清掉账本与日志；`backups/`（原生词库备份）默认保留，
  要删必须再加 `--purge-backups`。删除前会校验数据目录的归属标记，拒绝删 `$HOME`、`/` 等危险路径。

## 它自己不占地方

这个工具本身应该小到没有存在感，所以每个动作都要能被看见、被清理：

```bash
imehot clean                       # 清掉异常退出残留的临时副本（默认只清 1 小时前的）
imehot clean --all-temp            # 连刚才的都清
imehot clean --backups --keep-days 7 --dry-run   # 先看会删什么
imehot clean --backups --keep-days 7             # 再真删
```

磁盘占用参考（实测）：

| 项目 | 大小 |
|---|---|
| 账本 `state.db` | 56 KiB（42 词） |
| 部署的代码 `lib/` | 约 200 KiB |
| 每次写回前的备份 | 各 8 KiB |
| 临时副本 | 只在写回瞬间存在，正常结束即删除 |
| 常驻内存 | **0**（没有进程） |

如果后台代理反复异常退出，可能会留下 `lexdb-work-*.dat`；`imehot clean` 就是为这个准备的，
而且 `imehot run` 每次真正干活后都会顺手扫一遍。

---

## 已知限制

1. **不能观测单词的实时使用。** 见上文"衰减模型"。
2. **`lexdb.dat` 是私有 schema。** macOS 升级可能改结构，届时 `imehot doctor` 会报错而不是静默出错。
3. **注入需要停一次输入法。** 这是原生实现决定的，不是我们的选择。
4. **注入的词可能被冲掉。** 如果写入后输入法又从旧内存状态回写了，词会丢 ——
   备份在 `backups/`，用 `imehot restore --list` 查看、`imehot restore --experimental` 回滚。
5. **`decay` 会让原生删除词。** 这是"让输入法自己清理"的固有代价，不是 bug；
   它可能删掉你觉得有用的词，所以默认不做，且必须显式 `--experimental`。
6. **只支持简体拼音。** 五笔、双拼等有自己的 `.lm` 目录，尚未适配。

---

## 开发

```bash
make test      # 110 个单元测试，零第三方依赖
make lint      # ruff
make type      # mypy --strict
make doctor    # 真机自检
```

设计决策记录在 [`docs/adr/`](docs/adr/)：

- [0001 原生输入法就是权威裁判](docs/adr/0001-native-ime-as-source-of-truth.md)
- [0002 不做键盘监听](docs/adr/0002-no-keyboard-listeners.md)
- [0003 launchd WatchPaths 而非轮询](docs/adr/0003-launchd-watchpaths-over-polling.md)
- [0004 衰减模型](docs/adr/0004-decay-model.md)
- [0005 错字过滤](docs/adr/0005-typo-filtering.md)

## License

MIT
