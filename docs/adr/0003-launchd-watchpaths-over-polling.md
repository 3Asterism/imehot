# ADR 0003 — 用 launchd WatchPaths，而不是常驻守护进程或轮询

- 状态：已采纳
- 日期：2026-11-17

## 背景

需要「后台静默」地感知原生输入法词库的变化。三种候选：

| 方案 | 空闲 CPU | 常驻内存 | 启动延迟 | 实现复杂度 |
|---|---|---|---|---|
| A. 常驻进程 + FSEvents | 0 | ~15 MB | 无 | 中（要处理崩溃、重启、单例） |
| B. launchd `StartInterval` 轮询 | 每次唤醒都付进程启动开销 | 0 | 最长一个周期 | 低 |
| C. launchd `WatchPaths` + 兜底 `StartInterval` | 0（内核态 kqueue） | 0 | 毫秒级 | 低 |

## 决定

**方案 C。** 具体做法：

- `WatchPaths` 同时守 `zh_Hans-dynamic.lm/` 目录、`lexdb.dat`、`meta.dat`；
- `ThrottleInterval = 60` 把成串的文件系统事件压成一次运行；
- `StartInterval = 1800` 兜底（防 WatchPaths 因文件被替换而漏事件的已知缺陷）；
- `ProcessType = Background`、`LowPriorityIO = true`、`Nice = 10`，
  让内核把 IO 和 CPU 优先级压到最低。

## 关键优化：先比指纹，再开数据库

每次被唤醒时，进程做的第一件事是 `stat()` 两个文件，把
`mtime(ns) + size` 拼成一个指纹字符串，和上次存下的比对。
**不一致就直接退出**，连 SQLite 都不打开。

实测（M2 MacBook Air）：

```
未变化：53 ms 端到端（其中约 40 ms 是 Python 解释器启动）
有变化：61 ms 端到端，峰值 RSS ≈ 28 MB
空闲：0 CPU / 0 内存（没有进程）
```

按每天 20 次触发估算，总 CPU 时间 < 2 秒/天。

## 后果

- 不需要处理「守护进程挂了怎么办」；
- 顺带获得天然的单例性：launchd 不会并发跑同一个 job；
- 代价是必须保证每次运行足够快，所以指纹短路是**必须**的，不是可选优化。
