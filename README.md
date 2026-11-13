# sapper-ui 桌面控制台

给 `sapper`（sapper-v2）套一个跨平台桌面窗口：表单录题 → 一键开跑 → 实时日志 / 看板 / **Flag 结果**。
支持 **Linux / Windows / macOS**，适配 sapper_v2 的两种部署方式（**本机直装** / **Docker 容器**）。

- 前端：Electron 窗口 + 本地网页（`index.html`）
- 后端：纯 Python 标准库 HTTP 服务（`server.py`），只监听 127.0.0.1，
  带 Host/Origin 校验（阻断浏览器跨站请求与 DNS rebinding）
- 任务目录：`~/ctf/tasks/<任务名>/.sapper`（与命令行用法完全一致，可混用）

## 依赖

| 依赖 | 说明 |
|---|---|
| Python 3.9+ | 后端只用标准库，无需 pip |
| sapper 或 Docker | 取决于部署模式（见下节）；也可用 `SAPPER_UI_BIN` 指定 |
| Agent 引擎 | direct 模式：默认 `~/ctf/sapper-agent`（直连 DeepSeek）；docker 模式：用容器内置 claude CLI |
| Node.js 18+ | 仅开发/打包时需要（安装包自带 Electron 运行时） |
| 模型配置 | `~/ctf/sapper.env`（后端自动加载），或提前 export 环境变量 |

## 两种部署模式（界面左下「部署模式」可切换）

| | direct（直装） | docker（容器伴生） |
|---|---|---|
| sapper CLI | 本机 PATH / `~/.local/bin/sapper` | 容器内（镜像 `sapper-agent:tmux` 等） |
| 引擎 | 宿主机 sapper-agent 或 claude CLI | 容器内置 claude CLI（界面引擎切换不生效） |
| 任务目录 | 宿主机直接读写 | **宿主机任务目录挂载进容器** `/work/task`，store 不随容器销毁 |
| 命令驱动 | 直接 subprocess | `docker exec <容器> sapper ...` |
| 配置注入 | env 文件加载进环境 | 同一份 env 白名单转成 `docker run -e` |

docker 模式细节：每个任务一个伴生容器 `sapper-ui-<任务名>`，启动 `sleep infinity` 常驻，
UI 通过 `docker exec` 在容器内执行 `setup / auto / graph / view` 等；启动/停止 auto 时会
进入容器 `pkill` 兜底（杀掉 exec 客户端不会终止容器内进程）。容器默认 `--network host`
（Linux；Windows/macOS 上 Docker 的 host 网络语义不同，靶场在本机时改用 `bridge` +
`host.docker.internal`，或按需调整）。镜像可从 sapper-v2 仓库 `docker build -f
docker/Dockerfile.commander -t sapper:latest .` 构建，或 `docker load` 官方 release 的 tar 包。

## 各平台运行

### Linux

```bash
cd ~/ctf/sapper-ui
./start.sh          # 首次会自动 npm install（npmmirror 镜像）
```

### Windows

双击 `start.bat`（需已装 Python、Node）。sapper 直装方法：把 sapper-v2 的
`lib/sapper.py` 放到 PATH 里的某个目录（如重命名为 `sapper.py`），或用环境变量
`SAPPER_UI_BIN` 指向该文件（会自动用 python 执行）。Python 建议安装时勾选
"Add to PATH"，避免用到 Microsoft Store 的占位 `python`。

### macOS

```bash
cd ~/ctf/sapper-ui
./start.sh
```

菜单栏保留标准 App 菜单（Cmd+Q 退出、标准编辑快捷键可用）。

手动方式（三平台通用）：`npm install && npm start`。

## 打包

| 平台 | 本机命令 | 产物 |
|---|---|---|
| Linux | `./build-deb.sh` | `dist/sapper-ui_*_amd64.deb` + 免安装 tar.gz |
| Windows | `npx electron-builder --win zip` | `dist/electron-builder/Sapper Console-1.0.0-win.zip`（免安装，解压运行 `Sapper Console.exe`） |
| macOS | `npx electron-builder --mac zip --x64` / `--arm64` | `dist/electron-builder/Sapper Console-1.0.0[-arm64]-mac.zip`（拖进 Applications） |

**两种「真正的安装包」本机（Linux）打不了**，用 CI 出：

- Windows NSIS 安装包（`*.exe`）：NSIS 在非 Windows 平台必须用 wine 抽取卸载器（NsisTarget 硬性依赖）。
  本机装 wine 后可打：`sudo apt install wine && npm run dist:win`；
- macOS dmg：制作 dmg 依赖 macOS 工具链（hdiutil）。

仓库自带 `.github/workflows/build.yml`：把项目推到 GitHub 后，在 Actions 页手动触发（或推 `v*` 标签），
Windows runner 出 NSIS 安装包、macOS runner 出 x64/arm64 两个 dmg，产物在该次 run 的 Artifacts 里。

说明：

- 安装包自带 Electron 运行时，仍需目标机有 `python3` 和 sapper（direct 模式）或 Docker（docker 模式）。
- `win.signAndEditExecutable: false`（package.json）：Linux 上跳过 wine 的 exe 资源编辑，
  代价是 `Sapper Console.exe` 的属性图标/版本沿用 Electron 默认，不影响安装与使用；CI（Windows 原生）
  构建的 NSIS 安装包本身仍带应用图标。想要完整 exe 图标，装 wine 后删掉该配置行再打。
- macOS 包未签名（签名需要 Apple 开发者账号 + macOS）：首次打开右键 → 打开，
  或 `xattr -cr "/Applications/Sapper Console.app"`；Apple Silicon (M 系列) 用 `-arm64` 包。
- `npm run dist:dir` 只出不打包的目录，用于快速验证 electron-builder 配置。

## 使用流程

1. 左侧填：任务名、靶场地址（每行一个）、题目描述、提示（flag 格式等）
2. 附件题：在「附件」处多选文件（pcap/二进制/压缩包等，单个 ≤256MB），
   创建时自动存入 `任务目录/attachments/` 并登记为图谱 fact（含路径，agent 可直接读取）
3. **① 创建任务**：写入事实图谱（描述→note fact + 文档，靶标→host fact，提示→hint，附件→note fact）；
   docker 模式会先拉起该任务的伴生容器
4. **▶ 开始/继续**：`sapper auto` 无人值守求解，右侧实时滚动日志
   - 时间预算与 step 上限**默认留空 = 不限**（实际仍会在"无新任务 / 连续两轮无产出"时自动停止）
   - 运行中按钮变为「■ 停止」；启动前会自动释放已退出进程遗留的 intent claim
   - docker 模式下停止 = 结束宿主机 exec 进程 + 容器内 `pkill` 兜底，8 秒未退出自动强杀
5. 顶部 **🏁 横幅** 与 **Flag 标签页**：捕获到的 flag 自动出现（每 3 秒刷新），一键复制；
   **📝 生成解题报告**：一键产出 Markdown 报告（题目信息 / Flag / 图谱时间线 / intents / 附件）
   并保存到 `任务目录/writeup.md`，同时登记为图谱 fact。auto 结束时若已捕获 flag 且从未生成过，
   也会**自动**生成一份。报告基于图谱事实自动汇总，可反复重新生成（flag 数增长时自动补登记）
6. 「看板」看 facts/intents/附件摘要，「图谱」看交互式可视化

「自检模式」勾选后不调真模型（零 token），用于验证整条链路（docker 模式下同样生效，
注入 `SAPPER_BIN=true` 到容器）。

## 编辑任务

任务下拉框右侧「编辑」按钮（运行中不可点）：

- **时间预算 / step 上限**：留空即不限，保存后作为该任务的运行配置，下次开始/继续生效
- **目标一句话**：同步改写 `project.json` 与 goal fact（sapper 要求两者一致）
- **靶场地址**：新增的地址登记为 host fact；已有事实不会删除（图谱事实只增不改）
- **题目描述**：修改后写入 `desc.md` 并追加一条 note fact，保留历史
- **追加提示**：写入新 hint

新建任务时的时间/step 也会保存到 `任务目录/ui.json`，之后可在「编辑」里调整。

## 部署 / 模型配置与引擎面板

左下面板从上到下：

1. **部署模式**：direct / docker 切换 + 镜像、网络设置，保存到 `~/.sapper-ui/settings.json`；
   显示 Docker 可用性与镜像探测结果
2. **Agent 引擎**（仅 direct 模式生效）：跟随配置文件 / sapper-agent / claude CLI
3. **env 文件**：模型与密钥配置文件（格式见下），桌面版可用原生文件对话框选择

面板下方是**配置可视化**：sapper 版本与实际命令、模型与引擎分组、超时与限额分组
（`SAPPER_TIMEOUT` / `SAPPER_RETRY_MODEL` / `SAPPER_TOTAL_MINUTES` /
`SAPPER_MINUTES_PER_CHALLENGE` / `SAPPER_MAX_STEPS` / `SAPPER_AGENT_MAX_SECONDS`）、
密钥池 `SAPPER_KEY_POOL`（条数 + base_url，**token 一律打码为 `***后4位`，界面永远拿不到完整密钥**）、
当前 env 文件的键值对（同样打码）、以及诊断告警（找不到 sapper / 模型未设置 /
SAPPER_BIN 含空格 / 镜像缺失等）。

## env 文件格式

```sh
# 整行注释
export ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic"
export ANTHROPIC_AUTH_TOKEN="sk-..."
export SAPPER_MODEL="deepseek-flash"
export SAPPER_TIMEOUT="1200"              # 行内注释也支持（引号会被剥掉）
# export SAPPER_KEY_POOL='[{"base_url":"...","token":"...","model":"..."}]'
```

三级优先：`--env-file` 参数 > `SAPPER_UI_ENV` 环境变量 > 默认 `~/ctf/sapper.env`（不存在则跳过）。
界面里随时指定/切换；改动只影响之后启动的任务。

## 环境变量（可选）

- `SAPPER_UI_PYTHON`：python 解释器路径（默认 linux/mac `python3`，win `python`）
- `SAPPER_UI_BIN`：sapper 可执行文件路径（Windows 上可指向 `sapper.py`，会自动用 python 执行）
- `SAPPER_UI_ENV`：模型 env 文件路径（同 `--env-file`）
- `SAPPER_UI_ENGINE`：默认引擎（同 `--engine`，`default|agent|claude`）
- `SAPPER_UI_AGENT`：sapper-agent 脚本路径（默认 `~/ctf/sapper-agent/sapper_agent.py`）

## 命令行混用

UI 创建的任务就是标准 sapper 项目（docker 模式下任务目录仍在宿主机，容器内命令为
`docker exec sapper-ui-<任务名> sapper ...`）：

```bash
cd ~/ctf/tasks/<任务名>
sapper status / graph / hint "补充提示" / flag 'flag{...}'
```

## 安全说明

后端只监听 `127.0.0.1`，并对每个请求做 Host 回环校验 + Origin 同源校验
（浏览器里其他网页无法跨站调用 `/api/start` 等写操作）；密钥类配置在接口层统一打码。
本程序仅限授权安全测试 / CTF 使用。
