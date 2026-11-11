#!/usr/bin/env bash
# 构建 Linux x64 安装包（deb）与免安装绿色包（tar.gz）
# 用法：./build-deb.sh
set -euo pipefail
cd "$(dirname "$0")"

NAME="sapper-ui"
VERSION="$(python3 -c 'import json;print(json.load(open("package.json"))["version"])')"
ARCH="$(dpkg --print-architecture)"
ROOT="build/deb-root"
OUT="dist"
DEB="$OUT/${NAME}_${VERSION}_${ARCH}.deb"
PORTABLE="$OUT/${NAME}-${VERSION}-linux-${ARCH}"

rm -rf "$ROOT" "$PORTABLE"
mkdir -p "$OUT"

if [ ! -x node_modules/electron/dist/electron ]; then
  echo "缺少 Electron 运行时，请先执行 npm install" >&2
  exit 1
fi

mkdir -p "$ROOT/opt/$NAME/app" \
         "$ROOT/usr/bin" \
         "$ROOT/usr/share/applications" \
         "$ROOT/usr/share/icons/hicolor/256x256/apps" \
         "$ROOT/DEBIAN"

# 1. Electron 运行时（含 chrome-sandbox；安装后由 root 持有并 setuid）
cp -a node_modules/electron/dist/. "$ROOT/opt/$NAME/"
chmod 4755 "$ROOT/opt/$NAME/chrome-sandbox"

# 2. 应用代码
cp main.js preload.js server.py index.html package.json "$ROOT/opt/$NAME/app/"

# 3. 图标（512x512：electron-builder 的 NSIS/dmg 也复用这张）
mkdir -p assets
if [ ! -f assets/icon.png ]; then
  convert -size 512x512 xc:none \
    -fill '#1f6feb' -draw 'roundrectangle 32,32 480,480 104,104' \
    -fill white -gravity center -font DejaVu-Sans-Bold -pointsize 300 \
    -annotate 0 'S' assets/icon.png
fi
mkdir -p "$ROOT/usr/share/icons/hicolor/512x512/apps"
cp assets/icon.png "$ROOT/usr/share/icons/hicolor/512x512/apps/$NAME.png"

# 4. 启动器：把 ~/.local/bin 放进 PATH，sapper 才能被找到
cat > "$ROOT/usr/bin/$NAME" <<'LAUNCHER'
#!/bin/sh
export PATH="$HOME/.local/bin:$PATH"
exec /opt/sapper-ui/electron /opt/sapper-ui/app "$@"
LAUNCHER
chmod 755 "$ROOT/usr/bin/$NAME"

# 5. 桌面入口
cat > "$ROOT/usr/share/applications/$NAME.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Sapper 控制台
Name[en]=Sapper Console
Comment=CTF fact-graph 桌面控制台
Exec=$NAME
Icon=$NAME
Terminal=false
Categories=Development;Security;
StartupWMClass=$NAME
DESKTOP

# 6. deb 元数据
INSTALLED_SIZE="$(du -sk "$ROOT" | cut -f1)"
cat > "$ROOT/DEBIAN/control" <<CONTROL
Package: $NAME
Version: $VERSION
Section: utils
Priority: optional
Architecture: $ARCH
Installed-Size: $INSTALLED_SIZE
Maintainer: sapper-ui <local@sapper-ui>
Depends: python3, libgtk-3-0t64 | libgtk-3-0, libnss3, libgbm1, libasound2t64 | libasound2, libxss1, libxtst6, libatspi2.0-0t64 | libatspi2.0-0, libdrm2, libxkbcommon0, libxcomposite1, libxdamage1, libxrandr2, libsecret-1-0, libnotify4, xdg-utils
Description: Sapper CTF 桌面控制台
 给 sapper-cli 套的本地桌面界面：录题、附件、模型/引擎切换、
 一键运行、日志/看板/Flag/图谱可视化。
 运行前需自备：python3、sapper CLI（~/.local/bin/sapper 或 PATH），
 以及模型配置（默认 ~/ctf/sapper.env，可在界面里另选）。
CONTROL

dpkg-deb --build --root-owner-group "$ROOT" "$DEB"

# 7. 免安装包：解压后 ./run.sh 即可运行（无 setuid，使用 --no-sandbox）
mkdir -p "$PORTABLE"
cp -a "$ROOT/opt/$NAME/." "$PORTABLE/"
cp assets/icon.png "$PORTABLE/icon.png"
cat > "$PORTABLE/run.sh" <<'RUN'
#!/bin/sh
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$PATH"
export SAPPER_UI_PYTHON="${SAPPER_UI_PYTHON:-python3}"
# 绿色包无法安装 root setuid 沙箱，退化为 --no-sandbox
exec ./electron app --no-sandbox "$@"
RUN
chmod 755 "$PORTABLE/run.sh"
tar -czf "$OUT/${NAME}-${VERSION}-linux-${ARCH}.tar.gz" -C "$OUT" "$(basename "$PORTABLE")"

echo
echo "已生成："
ls -lh "$DEB" "$OUT/${NAME}-${VERSION}-linux-${ARCH}.tar.gz" | awk '{print "  "$5"\t"$9}'
echo "安装：sudo dpkg -i $(basename "$DEB")  （缺依赖时 sudo apt -f install）"
echo "免安装：tar xzf ${NAME}-${VERSION}-linux-${ARCH}.tar.gz && ./${NAME}-${VERSION}-linux-${ARCH}/run.sh"
