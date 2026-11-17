# ADR 0005 — 错字不应该被记成常用词

- 状态：**实验性（0.2.0 实现，默认不启用过滤）**
- 日期：2026-11-17

## 问题

用户把「说思路」打成「所思路」「所示录」，backspace 删掉重打。
这类词如果被当成热词收进词库，就是噪音。

## 关键发现：苹果的做法是「降权」，不是「不学」

苹果自己的专利 [US9703394](https://patentimages.storage.googleapis.com/42/2b/ee/c59862eef21491/US9703394.pdf) 写得很明白：

> The user may reject the suggestion "Gattaca" and type "Inception" instead.
> In response to the user's rejection, keyboard module may **reduce the probability**
> for "Gattaca", but **not remove it entirely**, for example by **decrementing the likelihood value**.

> the one or more indications of user input may include multiple selections of the
> **backspace key** that result in the deletion of characters

推论：**错字确实会进入 `lexdb.dat`**，只是在 `dynamic.dat` 里被降了权。
而 `dynamic.dat` 我们读不了（ADR 0001），所以我们看到的是一个**没有权重的错字**。

这就是为什么必须在 imehot 这一层做过滤。

## 参考实现

| 来源 | 做法 |
|---|---|
| [青简 Qingjian](https://qingjian.app/docs/personalize/habits) | 「选错了马上删掉重打、改选别的，这个词也不会留下」 |
| [librime](https://rimeinn.github.io/plugin/lua/Objects.html) | 提供 `delete_notifier`，删除即反学习 |
| [腾讯专利 CN103049458A](https://patents.google.com/patent/CN103049458A/zh) | 以「输入序列」为单位比较前后两次输入；**不要求有删除动作**，**不要求编码相同**（「你好」nh 与「年号」可识别，「什么」与「神恶魔」也可识别） |
| [ime-lexicon](https://skillsmp.com/zh/creators/taxueseek/ime-lexicon/skill-sub-skills-optimize) | 有明显笔误的自动改；**吃不准的进「待确认」，不写回输入法** |

## 我们的约束与可用的信号

拿不到按键、拿不到时间戳、拿不到权重。但有一样东西是免费的：

**`TokenID` 单调递增 = 学习先后顺序。**

一次 harvest 里新增的词构成一个「批次」，批内 TokenID 相邻的两个词，
就是输入法在极短时间内先后学到的两个词 —— 这正好对应腾讯专利里的「前后两次输入」。

于是判据变成：

1. 同一批次内，TokenID 差 ≤ K（默认 3）
2. 两个词的**拼音相似度**超过阈值
3. → TokenID 小的那个是「先被选中的」，也就是**疑似错字**

### 拼音相似度怎么算

参考 [CN102122298A「中文相似性匹配方法」](https://patentimages.storage.googleapis.com/b2/df/b6/d1147126cc7504/CN102122298A.pdf)，
它把相似度拆成编辑距离 + 键盘指法 + 声母相似 + 韵母相似 + 模糊音 + 字形。

我们只需要其中的拼音部分，因为这是**拼音输入法**：

- **模糊音归一化**：`zh/z`、`ch/c`、`sh/s`、`n/l`、`in/ing`、`an/ang`、`en/eng`
- 归一化后再算编辑距离，除以较长者长度得到相似度

验证一下用户的真实案例：

```
所思路  suo si lu   → 归一化  suo si lu
说思路  shuo si lu  → 归一化  suo si lu   ← sh→s 模糊音，完全一致
所示录  suo shi lu  → 归一化  suo si lu   ← sh→s 模糊音，完全一致
```

三个词归一化后**完全相同**。这说明只靠模糊音归一化就能把这类错字抓出来，
而且不需要任何外部词典。

风险：`会` vs `汇`、`在` vs `再` 这类真实同音词也会被判成一对。
所以规则**只打问号，不删除**。

## 决定

### 四级置信度

| 级别 | 含义 | 自动进热词库 |
|---|---|---|
| `suspect` | 在纠正对里是「前者」 | 否，需人工确认 |
| `candidate` | 新观察到，尚未冷静 | 否 |
| `confirmed` | 跨多个批次持续存在，或人工确认 | 是 |
| `rejected` | 人工否定 | 永久排除 |

### 四道防线

1. **纠正对检测**（主动怀疑）：同批次 + TokenID 相邻 + 拼音模糊音归一化后高度相似。
2. **冷静期**（被动兜底）：新词必须跨 ≥N 个批次 / ≥D 天仍然存在，才转 `confirmed`。
   错字即使进了词库，也不会被反复选中，最终会被原生衰减掉。
3. **待确认队列**（学 ime-lexicon）：所有 `suspect` 进一个可读的清单，
   默认不写回、不导出，等人裁决。
4. **黑名单**：`imehot reject <词>` 一次记一辈子。

### 垃圾词规则（补充到 `promote.judge`）

来自 ime-lexicon 的 profile 规则，简化为适合中文拼音的三条：

- 以「的了」「的吧」等虚词收尾的短语
- 长度 ≥ 16 的「句子」（那是整句上屏，不是词）
- 拼音与词面完全相同的纯 ASCII（已经在过滤）

## 后果

- 账本会多两个状态列（`confidence`、`batch_id`），需要 schema 迁移。
- `promote` 的默认行为要改：从「自动写入热词库」改为「只推荐，不自动转正」。
- 需要一个新的 `imehot review` 命令来裁决 `suspect`。

## 尚未验证

用户报告的错字发生在输入法最近一次落盘之后，目前还没有进入 `lexdb.dat`。
**需要等下一次落盘才能确认苹果是否真的把错字学了进去。**
如果没学，第 1 道防线仍然保留（成本低、误伤可控），但优先级可以降低。

## 修订（2026-11-17）：置信度不能落库，也不能默认过滤

初版把 `confidence` 写成账本的一列，并让 `export` / `promote` 默认只看
`confirmed`。代码审查指出这是**方向性错误**，两个理由都成立：

**1. 它是推断，不是事实。** 「这个像不像错字」由 TokenID 相邻性 + 拼音相似度
推导出来，是启发式判断。把它存成列、并让它决定数据去留，等于把猜测当观测 ——
和 ADR 0004 拒绝预计算 decay 的理由自相矛盾（那边说"原始事实不可再生"，
这边却在把推导结果沉淀成历史）。

**2. 冷静期会卡死。** 早期实现只在词库变化时才推进 `seen_runs`，于是
"词库这几天没变化"就等于"冷静期永不结束"。实测新装用户默认导出是**空的**。

### 改成什么

- 账本只存原始事实：`born_at`、`revivals`、`suspect_of`（成对被怀疑的关系）、
  `batch_id`（哪一批学到的）、以及 `override`（**用户显式判断**，这是事实）。
- `confidence` 由 `ledger.effective_confidence(row, now)` 在**查询时现算**：
  1. 有 `override` → 用它；
  2. `suspect_of` 非空 → `suspect`，除非复活过；
  3. 复活过 → `confirmed`；
  4. 出生超过冷静期（默认 2 天，按**时间**算）→ `confirmed`；
  5. 否则 `candidate`。
- 换算规则改了不用迁移数据，历史记录不动。

### 默认行为也改了

- `export` 默认 `--confidence all`：**不因为一个未验证的启发式而悄悄少给词**。
  导出内容里新增 `confidence` / `suspect_of` 两列，用户自己能看到标记。
- `promote` 默认处理 `confirmed + candidate`，跳过 `suspect`（除非 `--include-suspect`）。
- 首批导入（一次新增超过 12 个词）**不做**纠正对判定 —— 那是历史词一起进来，
  它们的 TokenID 相邻只代表"很久以前先后学到"，不是同一次输入的先错后对。

在这些验证完成前，本项目**不宣称**"错字一定不会进热词库"，只宣称
"错字会被标出来，且你可以一眼看到"。等真实数据校准阈值后再提高默认强度。
