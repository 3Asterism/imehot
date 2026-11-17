# imehot — 开发与运维快捷命令
#
#   make test    跑单元测试（零第三方依赖）
#   make lint    ruff 静态检查
#   make type    mypy 严格类型检查
#   make doctor  真机自检

PY      ?= $(shell if [ -x "$$HOME/.local/python/3.14/bin/python3.14" ]; then echo "$$HOME/.local/python/3.14/bin/python3.14"; else echo python3; fi)
export PYTHONPATH := src

# 把 ResourceWarning 变成错误：未关闭的 sqlite 连接/文件一律让测试挂掉，
# 而不是等到 GC 时机不确定时才在某个无关的 traceback 里冒出来。
WARN := -W error::ResourceWarning

.PHONY: help test lint type fmt check doctor run status install uninstall export clean setup-py

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

test: ## 跑全部单元测试
	$(PY) $(WARN) -m unittest discover -s tests -p "test_*.py"

lint: ## ruff 静态检查
	ruff check src tests

type: ## mypy 严格模式
	mypy src

fmt: ## ruff 格式化
	ruff format src tests

check: test lint type ## 提交前全量检查

doctor: ## 真机自检（权限 / 格式 / 代理）
	$(PY) -m imehot doctor

run: ## 立刻采样一次
	$(PY) -m imehot run -v

status: ## 总览
	$(PY) -m imehot status

install: ## 安装 launchd 代理
	$(PY) -m imehot install

uninstall: ## 卸载 launchd 代理
	$(PY) -m imehot uninstall

export: ## 导出热词 plist 到桌面
	$(PY) -m imehot export -f plist -o $$HOME/Desktop/imehot-热词.plist

clean: ## 清理构建产物
	rm -rf build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
