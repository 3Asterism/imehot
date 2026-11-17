# ADR 0004 — 衰减模型：只存事实，按时现算

- 状态：已采纳
- 日期：2026-11-17

## 参考了谁

| 实现 | 机制 | 出处 |
|---|---|---|
| **librime** | `UserDbValue{commits c, dee d, tick}`，`d_new = d0 + d_old · exp((t_old − t_new)/200)` | [librime](https://deepwiki.com/sbxlm/librime/4.2-user-dictionary-and-adaptation) |
| **青简 Qingjian** | 选择次数随时间冷却：**90 天不打，次数约减半**；冷却只影响排序，记录永存 | [qingjian habits](https://qingjian.app/docs/personalize/habits) |
| **WindInput 清风** | 三层：24h 草稿层（用过才跃迁）→ 5000 条临时词库 → 永久用户词库 | [WindInput 词库学习机制](https://blog.gitcode.com/5ff75b96dd6f59c0f1760954bec68710.html) |

## 决定

**账本只存原始事实，分数在查询时现算。**

`words` 表里没有一个叫 `score` 的列。存的是：

```
state         active | dropped | archived
born_at       首次观测到的时刻
dropped_at    首次发现消失的时刻
revivals      复活次数
strength      基础权重（目前 = 1 + revivals）
```

查询时：

```python
score = strength · 0.5 ** (idle_days / HALF_LIFE_DAYS)   # 默认 90 天
```

### 为什么事后算而不是入库时算

衰减参数一定会被调（90 天合不合适？要不要 30 天？）。
预计算意味着**调参就要重写历史数据**，而原始事实一旦被覆盖就不可再生。
事后算则只改一个常数。

### 为什么 active 的词不衰减

这是本项目最重要的诚实性约定。

原生输入法保留一个词，说明它至少不是「废词」。但我们**无法观测**一个持续存在的
词今天到底有没有被用过 —— `lexdb.dat` 里没有使用计数，`dynamic.dat` 里的权重
也映射不到 TokenID（见 ADR 0001）。

给 active 词编一个衰减曲线，会让「热度」看起来精确，实际是伪造的。
所以：

- `active` → `score = strength`，标注「原生词库仍在，不衰减」；
- `dropped` → 从原生删除那一刻开始，才按半衰期衰减；
- `archived` → 0。

### 三档清理

| 档位 | 动作 | 风险 | 命令 |
|---|---|---|---|
| L0 软归档 | 从导出口径中移出，记录保留 | 零 | `imehot prune` |
| L1 原生修剪 | 把 `meta.dat` 的 `lastDecay` / `lastPruning` 往回拨，让输入法用**自己的模型**清理 | **高** | `imehot decay --experimental --restart-ime` |
| L2 硬删除 | 直接从 `lexdb.dat` 删行 | 高（会先核对是否仍是 live 词） | `imehot prune --hard --experimental --restart-ime` |

默认只做 L0。真正「很久不用」的判断交给 L1 —— 因为那才是原生输入法自己的判断，
我们只是替用户按下它的开关。

### 修订（2026-11-17）：L1 的风险原来被低估了

初版把 L1 标成"中（有备份）"。这是错的，两个原因：

1. **只备份了 `meta.dat`。** 但 L1 的后果是**原生输入法删除 `lexdb.dat` 里的词**，
   而 `meta.dat` 只是它的"时钟"。备份时钟不备份被删的东西，等于没备份。
   现在 `force_decay` 会同时备份 `meta.dat` 和 `lexdb.dat`。
2. **改动幅度比文档说的大。** 初版说"把 `lastDecay` 往回拨"，实际同时把
   `lastPruning` 往回拨 400 天，并把 `wordsSinceLastDecay` 拉到 100 万，
   这会触发一次**衰减 + 修剪**，不是一次温和的排序调整。

现在 L1 标注为**高风险**，并且 `decay` 在输入法运行时会 fail closed（要求 `--restart-ime`）。
