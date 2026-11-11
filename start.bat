@echo off
rem Sapper 控制台启动器（Windows）
cd /d "%~dp0"
if not exist node_modules\electron (
  echo 首次运行：安装 Electron（走 npmmirror 镜像）…
  call npm install
  if errorlevel 1 (
    echo npm install 失败
    pause
    exit /b 1
  )
)
call npm start --silent