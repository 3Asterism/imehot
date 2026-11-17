#!/bin/sh
# 把 bin/imehot 软链到 PATH 上（默认 ~/.local/bin），不改动任何系统文件。
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
target="${1:-$HOME/.local/bin}"

chmod +x "$root/bin/imehot"

mkdir -p "$target"
ln -sfn "$root/bin/imehot" "$target/imehot"

echo "✓ 已链接 $target/imehot -> $root/bin/imehot"
case ":$PATH:" in
  *":$target:"*) ;;
  *) echo "  提示：$target 不在 PATH 里，请把它加进去。" ;;
esac

echo
echo "下一步："
echo "  1) 系统设置 → 隐私与安全性 → 完全磁盘访问权限，加入你的终端/Python"
echo "  2) imehot doctor      # 自检"
echo "  3) imehot run -v      # 首次采样"
echo "  4) imehot install     # 安装后台代理"
