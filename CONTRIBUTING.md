# 贡献指南

## 这个项目最在意的三件事

1. **不侵入**。任何会监听键盘、读剪贴板、截屏、联网、常驻内存的改动都会被拒绝，
   哪怕它能换来更好的功能。见 [ADR 0002](docs/adr/0002-no-keyboard-listeners.md)。
2. **不打扰**。后台路径必须是只读的、毫秒级的、可中断的。
   `tests/test_safety.py` 会守住这条线。
3. **不撒谎**。观测不到的东西就不建模。宁可少一个「热度分数」，
   也不要编一条看起来很精确的曲线。见 [ADR 0004](docs/adr/0004-decay-model.md)。

## 开发流程

```bash
git clone git@github.com:3Asterism/imehot.git
cd imehot
make test        # 必须全绿
make lint
make type
```

运行时不依赖任何第三方包，**请保持这一点**。测试也只用 `unittest`。

## 提交规范

使用 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/)：

```
feat(injector): 注入前自动停止输入法扩展
fix(native): 音节长度串与拼音长度不匹配时回退为原样
docs(adr): 补充衰减模型的选择理由
test(safety): 增加 mypy 严格类型检查
```

- `feat` / `fix` 必须附带测试。
- 改动架构决策时，**新增一条 ADR**，不要偷偷改旧 ADR 的结论（可以追加「后续」小节）。

## 新增功能前的自查

- [ ] 需要新的系统权限吗？如果需要「辅助功能」，基本可以判定方向错了。
- [ ] 后台路径会碰输入法进程吗？如果会，请重新设计。
- [ ] 新增的观测数据是「事实」还是「推断」？如果是推断，注释里要写清楚依据。
- [ ] 失败时会静默出错吗？`imehot doctor` 能发现吗？

## 代码风格

`ruff` + `mypy --strict`。行宽 100。注释写**为什么**，不写**做什么**。
中文注释没问题，本项目的主要读者是中文用户。

## 报告问题

请附上 `imehot doctor` 的输出（它会隐去个人词条）以及 macOS 版本。
**不要**贴出 `lexdb.dat` 的完整内容——那里面是你自己打的词。
