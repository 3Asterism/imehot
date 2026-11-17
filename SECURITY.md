# 安全说明

## 威胁模型

imehot 读取的是 macOS 原生输入法的**学习结果**——也就是你用过哪些词。
这属于个人数据，虽然敏感度低于按键记录，但仍应按个人数据对待。

### 本项目不做的事（且由冒烟测试检查）

| 行为 | 状态 | 守卫测试 |
|---|---|---|
| `CGEventTap` / `IOHIDManager` 键盘监听 | 永不 | `test_no_snooping_apis` |
| 读取候选窗口 / 截屏 / OCR | 永不 | `test_no_snooping_apis` |
| 读剪贴板 | 永不 | `test_no_snooping_apis` |
| 任何网络请求 | 永不 | `test_no_network` |
| 后台常驻进程 | 从不 | `test_launchd_is_not_a_daemon` |
| 后台路径停输入法 | 从不 | `test_cmd_run_never_touches_ime_process` |

**这些是 denylist 冒烟测试，不是形式化证明**：它挡得住随手引入的写法，
挡不住刻意规避（`ctypes` 动态调用、`getattr` 拼字符串、新增子包）。
真正的保证来自代码量小 + 无第三方依赖 + 人工审查。

### 数据落在哪

```
~/Library/Application Support/IMEHot/
├── state.db        SQLite（WAL），词条与时间线
├── fingerprint     上次采样的文件指纹
├── backups/        写回操作前的原生文件备份
└── imehot.log      launchd 的标准输出
```

全部在用户主目录内，未加密（依赖 FileVault 保护整盘）。
`imehot uninstall --purge` 会整体删除。

### 需要的权限

只需要**完全磁盘访问权限**（`~/Library/Containers/` 里其他 App 的容器受 TCC 保护）。
不需要辅助功能、不需要屏幕录制、不需要网络。

## 写入路径的风险

`inject` / `prune --hard` / `decay` / `restore` 会改写原生输入法文件。防护措施：

1. 写入前完整备份到 `backups/`（`mkstemp` 保证不重名，并 `fsync`）；
2. `lexdb.dat` 在**临时副本**上修改，`PRAGMA integrity_check` 通过才替换；
   `meta.dat` 则是序列化后**回读校验**再替换（它是 plist，没有 `integrity_check` 这回事）；
3. 用 `os.replace` 原子替换 inode，正在运行的程序持有的旧 fd 不受影响；
4. 输入法运行中时 `inject` / `prune --hard` / `decay` **拒绝执行**，需显式 `--restart-ime`；
5. `restore` 在覆盖前会把"当前文件"再备份一次，所以恢复本身也可回滚。

### 必须知道的边界

- **`decay` 会让原生输入法执行衰减和修剪，它可能删除 `lexdb.dat` 里的词。**
  我们备份了 `meta.dat` 和 `lexdb.dat`，但"删掉哪些词"只有原生知道。
- **停输入法会丢掉正在组字的内容。** 已经上屏到应用里的文字是安全的，
  但还没上屏的拼音和候选会消失。没有"检测你正在打字"的能力。
- **`uninstall --purge` 默认保留 `backups/`**；要删备份必须再加 `--purge-backups`。
  删除前会校验数据目录的归属标记，拒绝 `$HOME`、`/`、`~/Documents` 等危险目标。
- **没有自动化手段能回滚原生输入法自己的内部状态**（`dynamic.dat` 的权重我们不动也不读）。
  备份只覆盖 `lexdb.dat` 和 `meta.dat`。

## 报告漏洞

请通过 GitHub Security Advisories 私信，**不要**开公开 issue。
涉及以下方向的问题会被优先处理：

- 绕过上述任一「永不」约束的代码路径；
- 让后台代理获得写权限的配置错误；
- 备份/恢复流程中的缺陷可能导致原生词库损坏。
