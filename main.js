const { app, BrowserWindow, Menu, dialog, ipcMain } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const fs = require("fs");
const os = require("os");
const net = require("net");
const http = require("http");

const IS_WIN = process.platform === "win32";
let serverProc = null;
let mainWindow = null;
let serverPort = 0;

function pythonCmd() {
  return process.env.SAPPER_UI_PYTHON || (IS_WIN ? "python" : "python3");
}

function serverPyPath() {
  // 打包后 server.py 放在 extraResources（asar 外），python 才读得到
  return app.isPackaged
    ? path.join(process.resourcesPath, "server.py")
    : path.join(__dirname, "server.py");
}

function iconPath() {
  const p = path.join(__dirname, "assets", "icon.png");
  return fs.existsSync(p) ? p : undefined;
}

function sapperBin() {
  if (process.env.SAPPER_UI_BIN) return process.env.SAPPER_UI_BIN;
  const home = os.homedir();
  for (const c of [
    path.join(home, ".local", "bin", "sapper"),
    path.join(home, ".local", "bin", "sapper.py"),
  ]) {
    if (fs.existsSync(c)) return c;
  }
  return null;
}

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.listen(0, "127.0.0.1", () => {
      const port = srv.address().port;
      srv.close(() => resolve(port));
    });
    srv.on("error", reject);
  });
}

function waitReady(port, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const tick = () => {
      const req = http.get({ host: "127.0.0.1", port, path: "/", timeout: 1500 }, (res) => {
        res.resume();
        resolve();
      });
      req.on("error", () => {
        if (Date.now() > deadline) reject(new Error("后端启动超时"));
        else setTimeout(tick, 300);
      });
    };
    tick();
  });
}

async function startBackend() {
  const port = await freePort();
  const args = [serverPyPath(), "--port", String(port)];
  const bin = sapperBin();
  if (bin) args.push("--sapper-bin", bin);
  serverProc = spawn(pythonCmd(), args, {
    cwd: __dirname,
    env: { ...process.env, PYTHONUNBUFFERED: "1" },
    detached: !IS_WIN,
    windowsHide: true,  // Windows 上 python 是控制台程序，不隐藏会弹黑窗
    stdio: ["ignore", "pipe", "pipe"],
  });
  const log = [];
  const onData = (d) => {
    const s = d.toString();
    log.push(s);
    if (log.length > 200) log.shift();
    process.stdout.write("[server] " + s);
  };
  serverProc.stdout.on("data", onData);
  serverProc.stderr.on("data", onData);
  serverProc.on("exit", (code) => {
    serverProc = null;
    if (!app.isQuitting) {
      dialog.showErrorBox(
        "Sapper 后端退出",
        `python 后端意外退出 (code=${code})\n\n${log.join("").slice(-2000)}`
      );
      app.quit();
    }
  });
  await waitReady(port);
  return port;
}

function killBackend() {
  if (!serverProc) return;
  const pid = serverProc.pid;
  serverProc = null;
  try {
    if (IS_WIN) spawn("taskkill", ["/pid", String(pid), "/T", "/F"]);
    else process.kill(-pid, "SIGTERM");
  } catch (e) { /* 进程可能已退出 */ }
}

function createWindow(port) {
  mainWindow = new BrowserWindow({
    width: 1320,
    height: 860,
    minWidth: 1100,
    minHeight: 600,
    backgroundColor: "#0d1117",
    title: "Sapper 控制台",
    icon: iconPath(),
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, "preload.js"),
    },
  });
  mainWindow.loadURL(`http://127.0.0.1:${port}/`);
  mainWindow.webContents.on("before-input-event", (_e, input) => {
    if (input.key === "F12") mainWindow.webContents.toggleDevTools();
  });
  mainWindow.on("closed", () => { mainWindow = null; });
}

function setupMenu() {
  // macOS 不能整棵砍掉菜单（Cmd+Q、复制粘贴都在里面）；其他平台维持无菜单
  if (process.platform === "darwin") {
    const template = [
      { label: app.name, submenu: [
        { role: "about" }, { type: "separator" },
        { role: "hide" }, { role: "hideOthers" }, { role: "unhide" },
        { type: "separator" }, { role: "quit" },
      ]},
      { role: "editMenu" },
      { role: "windowMenu" },
    ];
    Menu.setApplicationMenu(Menu.buildFromTemplate(template));
    const icon = iconPath();
    if (icon && app.dock) {
      try { app.dock.setIcon(icon); } catch (e) { /* 非 dock 环境 */ }
    }
  } else {
    Menu.setApplicationMenu(null);
  }
}

app.whenReady().then(async () => {
  if (!app.requestSingleInstanceLock()) { app.quit(); return; }
  setupMenu();
  ipcMain.handle("pick-env-file", async () => {
    const r = await dialog.showOpenDialog(mainWindow, {
      title: "选择模型 env 配置文件",
      properties: ["openFile", "showHiddenFiles"],
      filters: [
        { name: "env 配置", extensions: ["env", "conf", "cfg", "txt", "sh"] },
        { name: "所有文件", extensions: ["*"] },
      ],
    });
    return r.canceled ? null : r.filePaths[0];
  });
  try {
    serverPort = await startBackend();
    createWindow(serverPort);
  } catch (err) {
    dialog.showErrorBox(
      "启动失败",
      `${(err && err.message) || err}\n\n请确认已安装 Python 3 与 sapper（SAPPER_UI_PYTHON / SAPPER_UI_BIN 可指定路径）。`
    );
    app.quit();
  }
});

app.on("second-instance", () => {
  if (mainWindow) { mainWindow.restore(); mainWindow.focus(); }
});
app.on("activate", () => {
  // macOS 惯例：点 Dock 图标重新开窗（后端常驻，直接复用端口）
  if (process.platform === "darwin" && serverPort
      && (!mainWindow || mainWindow.isDestroyed())) {
    createWindow(serverPort);
  }
});
app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
app.on("before-quit", () => { app.isQuitting = true; killBackend(); });