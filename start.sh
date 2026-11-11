#!/usr/bin/env bash
# Sapper 控制台启动器（Linux/macOS）
cd "$(dirname "$0")" || exit 1
export PATH="$HOME/.local/bin:$PATH"
if [ ! -d node_modules/electron ]; then
  echo "首次运行：安装 Electron（走 npmmirror 镜像）…"
  npm install || { echo "npm install 失败"; read -r -p "按回车退出"; exit 1; }
fi
exec npm start --silent