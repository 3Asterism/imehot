# ADR 0001 — 原生输入法就是「这个词还在用吗」的权威裁判

- 状态：已采纳
- 日期：2026-11-17

## 背景

要做一个「记录我输入法里的热词」的工具，第一反应自然是监听键盘、记录候选、
自己统计词频。但这条路有三个硬伤：

1. **权限重**：`CGEventTap` 需要「辅助功能」授权，等于给自己装了个键盘记录器；
2. **成本高**：常驻进程 + 每个按键一次回调；
3. **不准**：我们只能看到「敲了什么」，看不到「输入法最后记住了什么」。

而 macOS 原生简体拼音输入法（SCIM）**自己就在做这件事**，而且把结果以
可解析的形式落在了磁盘上。

## 实测

`~/Library/Containers/com.apple.inputmethod.SCIM/Data/Library/LanguageModeling/zh_Hans-dynamic.lm/`

| 文件 | 格式 | 内容 | 可用性 |
|---|---|---|---|
| `lexdb.dat` | SQLite | `Words(TokenID, Surface, PinyinReading, PinyinSyllableLengths)` | ✅ 干净、可直接读 |
| `meta.dat` | bplist | `numberOfTokensTyped`、`lastDecay`、`lastPruning`、`adaptationVersion` | ✅ 可直接读 |
| `dynamic.dat` | 私有归档 | 统计模型 | ❌ 已确认不含可映射到 TokenID 的权重 |
| `cache.dat` | 私有归档 | 缓存 | ❌ 无需解析 |

`meta.dat` 解出来的真实内容：

```
numberOfTokensTyped: 18208
lastDecay:           816606348.79   (CF 绝对时 → 2026-11-17)
lastPruning:         0.0
wordsSinceLastDecay: 618
adaptationVersion:   '2.6'
```

也就是说：**原生输入法自己就在学习、自己就有衰减和修剪机制**，
`lexdb.dat` 里躺着 42 条它为用户学到的词（人名 `郝晓晞` `邹宇` `孟豪`、
英文 `community` `guitar`、还有一堆 emoji）。

## 决定

**不重复造轮子。** 我们只做原生输入法做不到的三件事：

1. **可见** —— 把它的学习结果读出来给人看；
2. **可迁移** —— 导出成 TSV / JSON / plist / RIME；
3. **可追溯** —— 记录每个词的出生、死亡、复活时间线。

判断一个词「还在不在用」，以**原生词库里的进出**为准，而不是我们自己的猜测。

## 后果

- 不需要键盘监听，不需要辅助功能权限，只需要「完全磁盘访问权限」读一个文件；
- 每个词能拿到的事实只有两类：**首次观测到** 和 **首次发现消失**；
- 一个持续存在的词，我们无法知道它今天是否被用过 —— 这一点在
  [ADR 0004](0004-decay-model.md) 里被显式地承认为模型限制，而不是用假数据糊过去。
