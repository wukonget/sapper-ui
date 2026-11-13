#!/usr/bin/env python3
"""sapper-ui — 本地 Web 控制台：表单录入题目/靶标，一键启动 sapper auto。

零第三方依赖（纯 Python 标准库）。只监听 127.0.0.1，仅供授权安全测试使用。
支持 sapper_v2 两种部署方式：direct（本机直装 CLI）/ docker（容器伴生，
宿主机任务目录挂载进容器，sapper 命令经 docker exec 驱动）。

用法:
    python3 server.py [--port 8788] [--tasks-dir ~/ctf/tasks]

浏览器打开 http://127.0.0.1:8788
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

BASE_DIR = Path(__file__).resolve().parent
INDEX_HTML = (BASE_DIR / "index.html").read_text(encoding="utf-8")

# ---------- 平台层 ----------
IS_POSIX = os.name == "posix"
IS_WIN = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000 if IS_WIN else 0  # Windows 子进程不弹控制台窗口
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

# task name -> {"proc": Popen|None, "log": deque[str], "lock": Lock}
RUNNING = {}
LOCK = threading.Lock()

# sapper 启动命令，main() 里按 --sapper-bin / SAPPER_UI_BIN 解析
SAPPER_CMD = ["sapper"]

# 当前加载的模型 env 文件路径（空=未加载文件）
CURRENT_ENV_FILE = ""
MAX_UPLOAD = 256 * 1024 * 1024  # 单个附件上限 256MB

# Agent 引擎：default=沿用 env 里的 SAPPER_BIN；agent=内置 sapper-agent；claude=claude CLI
ENGINE_MODE = "default"
AGENT_PY = Path(os.environ.get("SAPPER_UI_AGENT")
                or (Path.home() / "ctf" / "sapper-agent" / "sapper_agent.py"))
ENGINES = {
    "default": "跟随配置文件（env 中的 SAPPER_BIN）",
    "agent": "sapper-agent（内置 Python 引擎，直连 DeepSeek）",
    "claude": "claude CLI（Anthropic 兼容接口）",
}
UNLIMITED_STEPS = 1000000  # sapper 要求 --max-steps >= 1，"不限"用大值透传

# ---------- 部署设置（direct / docker） ----------
# SAPPER_UI_SETTINGS 可覆盖设置目录（测试/多实例隔离用）
SETTINGS_DIR = Path(os.environ.get("SAPPER_UI_SETTINGS") or (Path.home() / ".sapper-ui"))
SETTINGS_FILE = SETTINGS_DIR / "settings.json"
# 注入容器的环境变量白名单前缀（SAPPER_UI_* 是 UI 自身配置，不注入）
ENV_INJECT_PREFIXES = ("SAPPER_", "ANTHROPIC_", "OPENAI_", "DEEPSEEK_")
ENV_INJECT_SKIP = ("SAPPER_UI_",)
DEPLOY = {
    "mode": "direct",            # direct=本机 CLI；docker=容器伴生
    "image": "sapper-agent:tmux",
    "network": "host" if IS_POSIX else "bridge",
}
_CONTAINER_OK = {"name": None, "ts": 0.0}      # 容器存活检查的短缓存
_DOCKER_STATE = {"ok": None, "ts": 0.0, "images": {}}  # docker/镜像探测缓存
_SAPPER_VER = {"text": None, "ts": 0.0}


def load_settings() -> None:
    """从 ~/.sapper-ui/settings.json 读取部署设置（缺省则自动探测）。"""
    global DEPLOY
    data = {}
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        pass
    if not isinstance(data, dict):
        data = {}
    DEPLOY["mode"] = data.get("mode") if data.get("mode") in ("direct", "docker") else None
    DEPLOY["image"] = str(data.get("image") or DEPLOY["image"])
    DEPLOY["network"] = str(data.get("network") or DEPLOY["network"])
    if DEPLOY["mode"] is None:
        DEPLOY["mode"] = detect_deploy_mode()
    save_settings()


def save_settings() -> None:
    try:
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {k: DEPLOY[k] for k in ("mode", "image", "network")},
            ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, SETTINGS_FILE)
    except OSError as exc:
        print(f"[sapper-ui] 保存部署设置失败: {exc}")


def detect_deploy_mode() -> str:
    """没显式配置时自动探测：本机有 sapper CLI → direct；否则 docker 可用且有镜像 → docker。"""
    bin_env = os.environ.get("SAPPER_UI_BIN")
    if shutil.which("sapper") or (bin_env and Path(bin_env).expanduser().exists()):
        return "direct"
    if docker_ok() and image_ok(DEPLOY["image"]):
        return "docker"
    return "direct"


# ---------- docker 后端 ----------
def _docker(args: list[str], timeout: int = 60) -> tuple[int, str]:
    cmd = ["docker"] + args
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           text=True, encoding="utf-8", errors="replace",
                           creationflags=CREATE_NO_WINDOW)
    except FileNotFoundError:
        return 127, "找不到 docker 命令 —— 请确认 Docker 已安装并在 PATH 中"
    except subprocess.TimeoutExpired:
        return 124, f"docker {args[0]} 超时（{timeout}s）"
    out = (p.stdout or "") + (p.stderr or "")
    return p.returncode, out.strip()


def docker_ok() -> bool:
    now = time.monotonic()
    if _DOCKER_STATE["ok"] is None or now - _DOCKER_STATE["ts"] > 60:
        code, out = _docker(["version", "--format", "{{.Server.Version}}"], timeout=10)
        _DOCKER_STATE["ok"] = code == 0
        _DOCKER_STATE["ts"] = now
        if code != 0:
            _DOCKER_STATE["images"] = {}
    return bool(_DOCKER_STATE["ok"])


def image_ok(image: str) -> bool:
    now = time.monotonic()
    cached = _DOCKER_STATE["images"].get(image)
    if cached is not None and now - _DOCKER_STATE["ts"] <= 60:
        return cached
    code, _ = _docker(["image", "inspect", image], timeout=15)
    _DOCKER_STATE["images"][image] = code == 0
    _DOCKER_STATE["ts"] = now
    return code == 0


def container_name(name: str) -> str:
    """任务名可能是中文，docker 容器名只允许 [a-zA-Z0-9][a-zA-Z0-9_.-]。

    非法字符替换为 -，并追加任务名的 sha1 前 8 位保证唯一。
    """
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", name)
    if not safe or not re.match(r"[A-Za-z0-9]", safe[0]):
        safe = "t" + safe
    return f"sapper-ui-{safe}-{hashlib.sha1(name.encode('utf-8')).hexdigest()[:8]}"[:128]


def container_status(name: str) -> str:
    """返回 none / running / exited。仅在 docker 模式下被调用。"""
    code, out = _docker(["inspect", "-f", "{{.State.Running}}",
                         container_name(name)], timeout=15)
    if code != 0:
        return "none"
    return "running" if out.strip() == "true" else "exited"


def env_whitelist() -> dict:
    """收集要注入容器的环境变量（parse_env_file 已加载进 os.environ）。"""
    out = {}
    for k, v in os.environ.items():
        if v is None:
            continue
        if k.startswith(ENV_INJECT_SKIP):
            continue
        if k.startswith(ENV_INJECT_PREFIXES):
            out[k] = v
    out.setdefault("PYTHONUNBUFFERED", "1")  # 容器内 sapper 日志实时滚动
    return out


def ensure_container(tdir: Path, name: str) -> tuple[bool, str]:
    """确保任务的伴生容器在运行；不存在则创建（挂载任务目录、注入白名单 env）。"""
    cn = container_name(name)
    now = time.monotonic()
    if _CONTAINER_OK["name"] == cn and now - _CONTAINER_OK["ts"] < 10:
        return True, ""
    code, out = _docker(["inspect", "-f", "{{.State.Running}}", cn], timeout=15)
    if code == 0:
        if out.strip() == "true":
            _CONTAINER_OK.update(name=cn, ts=now)
            return True, ""
        code, out = _docker(["start", cn])
        if code == 0:
            _CONTAINER_OK.update(name=cn, ts=now)
        return code == 0, f"容器已停止且启动失败: {out}"
    if code == 127:
        return False, out
    mount_src = str(tdir).replace("\\", "/")  # Windows 的 -v 需要 C:/... 形式
    cmd = ["run", "-d", "--name", cn,
           "-w", "/work/task", "-v", f"{mount_src}:/work/task",
           "--network", DEPLOY["network"]]
    for k, v in env_whitelist().items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [DEPLOY["image"], "sleep", "infinity"]
    code, out = _docker(cmd, timeout=120)
    if code == 0:
        _CONTAINER_OK.update(name=cn, ts=time.monotonic())
        return True, ""
    hint = "" if image_ok(DEPLOY["image"]) else \
        f"（镜像 {DEPLOY['image']} 不存在：可在 sapper-v2 仓库 docker build，或 docker load 发布的 tar 包）"
    return False, f"创建容器失败: {out} {hint}"


def sapper_argv(tdir: Path, name: str, args: list[str]) -> list[str]:
    """按部署模式构造完整命令行。docker 模式下任务目录固定挂载在 /work/task。"""
    if DEPLOY["mode"] != "docker":
        return SAPPER_CMD + ["--store", str(tdir / ".sapper")] + args
    return ["docker", "exec", "-w", "/work/task", container_name(name),
            "sapper", "--store", "/work/task/.sapper"] + args


def engine_bin() -> str:
    """返回本次启动要注入的 SAPPER_BIN；空串表示不改环境（default）。

    sapper 内部按空白拆分 SAPPER_BIN，路径不能含空格；Windows 下尝试
    8.3 短路径消除，消除不了会在诊断里告警。
    """
    if ENGINE_MODE == "agent":
        py, agent = str(sys.executable), str(AGENT_PY)
        if IS_WIN:
            py, agent = win_short_path(py), win_short_path(agent)
        return f"{py} {agent}"
    if ENGINE_MODE == "claude":
        return "claude"
    return ""


def win_short_path(path: str) -> str:
    """Windows 下把含空格的长路径转成 8.3 短路径；失败则原样返回。"""
    if not IS_WIN or " " not in path:
        return path
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.kernel32.GetShortPathNameW(path, buf, 260):
            return buf.value
    except Exception:
        pass
    return path


def platform_warnings() -> list[str]:
    """SAPPER_BIN 是「命令前缀 + 参数」，多 token 属正常；只有单个路径
    组件内部带空格才会被 sapper 的空白拆分破坏。"""
    warns = []
    if IS_WIN and re.search(r"\s", sys.executable) \
            and win_short_path(sys.executable) == sys.executable:
        warns.append(f"python 解释器路径含空格且无法转 8.3 短路径: {sys.executable}，"
                     "sapper-agent 引擎可能无法启动")
    if re.search(r"\s", str(AGENT_PY)):
        warns.append(f"sapper-agent 路径含空格，sapper 无法解析: {AGENT_PY}")
    return warns


def pid_alive(pid: int) -> bool:
    """探测进程是否存活。Windows 的 os.kill(pid,0) 是 TerminateProcess 语义，
    绝不能用来探测 —— 改走 tasklist。"""
    if IS_POSIX:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except (PermissionError, OSError):
            return True
    try:
        p = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                           capture_output=True, timeout=10, text=True,
                           encoding="utf-8", errors="replace",
                           creationflags=CREATE_NO_WINDOW)
        return str(pid) in (p.stdout or "")
    except Exception:
        return True  # 探测失败保守视为存活


def kill_tree(proc: subprocess.Popen, force: bool = False) -> None:
    """终止进程及整棵子树。POSIX 用进程组；Windows 用 taskkill /T（能带上孙进程）。"""
    if IS_POSIX:
        try:
            sig = signal.SIGKILL if force else signal.SIGTERM
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError):
            pass
        return
    args = ["taskkill", "/PID", str(proc.pid), "/T"]
    if force:
        args.append("/F")
    try:
        subprocess.run(args, capture_output=True, timeout=15,
                       creationflags=CREATE_NO_WINDOW)
    except Exception:
        try:
            proc.kill()
        except OSError:
            pass


def diagnostics() -> list[str]:
    """配置面板的诊断告警。"""
    warns = platform_warnings()
    if DEPLOY["mode"] == "docker":
        if not docker_ok():
            warns.append("Docker 不可用（未安装或守护进程未启动）")
        elif not image_ok(DEPLOY["image"]):
            warns.append(f"镜像 {DEPLOY['image']} 不存在，需先构建或 load")
        warns.append("docker 模式：引擎使用容器内置 claude CLI，界面引擎切换不生效")
    else:
        bin_path = SAPPER_CMD[-1] if SAPPER_CMD[0] == sys.executable else SAPPER_CMD[0]
        if not (shutil.which(bin_path) or Path(bin_path).exists()):
            warns.append(f"找不到 sapper 命令 ({bin_path}) —— 任务操作会失败")
    if not os.environ.get("SAPPER_MODEL"):
        warns.append("SAPPER_MODEL 未设置（模型可能无法启动）")
    if os.environ.get("ANTHROPIC_BASE_URL") and not (
            os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")):
        warns.append("设置了 ANTHROPIC_BASE_URL 但没有 AUTH_TOKEN")
    if ENGINE_MODE == "agent" and not AGENT_PY.is_file():
        warns.append(f"引擎选了 sapper-agent 但文件不存在: {AGENT_PY}")
    return warns


def sapper_version() -> str:
    now = time.monotonic()
    if _SAPPER_VER["text"] is None and now - _SAPPER_VER["ts"] > 300:
        try:
            p = subprocess.run(SAPPER_CMD + ["--version"], capture_output=True,
                               timeout=10, text=True, encoding="utf-8",
                               errors="replace", creationflags=CREATE_NO_WINDOW)
            _SAPPER_VER["text"] = ((p.stdout or p.stderr or "").strip()
                                   or ("(退出码 %d)" % p.returncode))
        except Exception as exc:
            _SAPPER_VER["text"] = f"(查询失败: {exc})"
        _SAPPER_VER["ts"] = now
    return _SAPPER_VER["text"] or "未知"


# ---------- env 文件解析 ----------
def _strip_inline_comment(val: str) -> str:
    """去掉成对引号之外的行内注释（# 须在引号外且前面是空白，与 shell 语义一致）。"""
    in_s = in_d = False
    for i, ch in enumerate(val):
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        elif ch == "#" and not in_s and not in_d and (i == 0 or val[i - 1] in " \t"):
            return val[:i].rstrip()
    return val


def parse_env_file(path: Path) -> dict:
    """解析 env 文件（KEY=VALUE / export KEY=VALUE，# 注释，去首尾引号）。

    行内注释（VALUE 后的 "# 说明"）会被去掉，且引号内的 # 不受影响。
    返回有序 dict，不改动 os.environ（纯函数，docker 注入也复用）。
    """
    out: dict[str, str] = {}
    if not path.exists():
        return out
    pat = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
    # utf-8-sig 兼容 Windows 编辑器写入的 BOM
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        m = pat.match(line)
        if not m:
            continue
        key, val = m.group(1), _strip_inline_comment(m.group(2).strip())
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        out[key] = val
    return out


def load_env_file(path: Path, override: bool = False) -> None:
    """把 env 文件的值加载进 os.environ。

    默认 setdefault：已显式 export 的真实环境变量优先；override=True 用于
    运行时在界面里切换配置文件，新文件的值覆盖旧值。
    """
    for key, val in parse_env_file(path).items():
        if override:
            os.environ[key] = val
        else:
            os.environ.setdefault(key, val)


# ---------- 基础工具 ----------
def task_dir(tasks_root: Path, name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_\-一-龥]{1,64}", name):
        raise ValueError("任务名只能包含中英文、数字、- 和 _，最长 64 字符")
    return tasks_root / name


def run_sapper(tdir: Path, args: list[str], timeout: int = 120,
               merge_stderr: bool = True) -> tuple[int, str]:
    """按部署模式执行 sapper 子命令（不经过 shell）。"""
    cmd = sapper_argv(tdir, tdir.name, args)
    try:
        p = subprocess.run(cmd, cwd=str(tdir), capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, env=os.environ.copy(),
                           creationflags=CREATE_NO_WINDOW)
        out = (p.stdout or "") + ((p.stderr or "") if merge_stderr else "")
        return p.returncode, out.strip()
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s: {' '.join(cmd)}"
    except FileNotFoundError:
        if DEPLOY["mode"] == "docker":
            return 127, "找不到 docker 命令 —— 请确认 Docker 已安装并在 PATH 中"
        return 127, f"找不到 sapper 命令 ({SAPPER_CMD[0]}) —— 请确认已安装并在 PATH 中"


def append_log(name: str, line: str) -> None:
    with LOCK:
        RUNNING.setdefault(name, {"proc": None, "log": deque(maxlen=4000), "lock": threading.Lock()})
        RUNNING[name]["log"].append(line)


def start_auto(name: str, tdir: Path, minutes: int | None, max_steps: int | None,
               skip_permissions: bool, engine_override: str | None) -> None:
    # 不传 --minutes = 无时间上限；max-steps 不支持 0，用大值表示不限
    steps = max_steps or UNLIMITED_STEPS
    args = ["auto"]
    if minutes:
        args += ["--minutes", str(minutes)]
    args += ["--max-steps", str(steps), "--max-explore", str(steps)]
    if skip_permissions:
        args.append("--skip-permissions")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"  # sapper 是 python：管道输出改行缓冲，日志才能实时滚动
    docker_mode = DEPLOY["mode"] == "docker"
    if docker_mode:
        ok, msg = ensure_container(tdir, name)
        if not ok:
            raise RuntimeError(msg)
        # 引擎是容器内置 claude CLI；仅自检模式的 SAPPER_BIN 需要传进容器
        if engine_override:
            env["SAPPER_BIN"] = engine_override
        else:
            env.pop("SAPPER_BIN", None)
    elif engine_override:
        env["SAPPER_BIN"] = engine_override  # 自检模式: 不调真模型
    else:
        bin_ = engine_bin()
        if bin_:
            env["SAPPER_BIN"] = bin_

    def reader(proc, handle):
        try:
            for raw in iter(handle.readline, ""):
                append_log(name, raw.rstrip("\n"))
        except Exception as exc:  # 解码等异常不能让日志线程悄悄死掉
            append_log(name, f"[ui] 日志读取异常: {exc}")
        finally:
            code = proc.wait()
            append_log(name, f"[ui] auto 进程退出 (exit={code})")
            with LOCK:
                entry = RUNNING.get(name)
                if entry:
                    entry["proc"] = None  # 释放句柄；/api/* 用 None 判定为已结束
            maybe_autoreport(name, tdir)  # 解出 flag 时自动落盘解题报告

    with LOCK:
        entry = RUNNING.setdefault(name, {"proc": None, "log": deque(maxlen=4000), "lock": threading.Lock()})
        if entry["proc"] and entry["proc"].poll() is None:
            raise RuntimeError("该任务已在运行中")
        proc = subprocess.Popen(
            sapper_argv(tdir, name, args),
            cwd=str(tdir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            env=env,
            start_new_session=IS_POSIX,
            creationflags=CREATE_NO_WINDOW,
        )
        entry["proc"] = proc
    threading.Thread(target=reader, args=(proc, proc.stdout), daemon=True).start()
    used = ("容器内置 claude CLI" if docker_mode
            else (env.get("SAPPER_BIN") or "(未设置)"))
    time_txt = f"{minutes} 分钟" if minutes else "不限"
    step_txt = str(max_steps) if max_steps else "不限"
    append_log(name, f"[ui] 已启动: sapper auto（时间 {time_txt}，step {step_txt}）")
    append_log(name, f"[ui] 引擎: {used}")


def stop_auto(name: str) -> bool:
    with LOCK:
        entry = RUNNING.get(name)
        proc = entry["proc"] if entry else None
    if not proc or proc.poll() is not None:
        return False
    if DEPLOY["mode"] == "docker":
        # 杀掉 exec 客户端不会终止容器内进程，必须进容器补一刀
        cn = container_name(name)
        _docker(["exec", cn, "pkill", "-f", "sapper"], timeout=30)
    kill_tree(proc, force=False)
    append_log(name, "[ui] 已发送停止信号")

    def escalate():
        time.sleep(8)
        if proc.poll() is None:
            append_log(name, "[ui] 进程未响应 TERM，强制结束")
            if DEPLOY["mode"] == "docker":
                _docker(["exec", container_name(name),
                         "pkill", "-9", "-f", "sapper"], timeout=30)
            kill_tree(proc, force=True)

    threading.Thread(target=escalate, daemon=True).start()
    return True


def worker_alive(worker: str) -> bool:
    """worker 名形如 deepseek-flash-122308 / agent-1234，尾部是启动它的 PID。

    拿不到 PID 或无权探测时保守视为仍存活，避免抢别人的任务。
    """
    m = re.search(r"-(\d+)$", worker or "")
    if not m:
        return True
    return pid_alive(int(m.group(1)))


def release_stale_claims(tdir: Path) -> list[str]:
    """释放 worker 进程已退出的 intent claim，返回释放列表。"""
    code, out = run_sapper(tdir, ["graph", "--format", "json"], timeout=30)
    if code != 0:
        return []
    try:
        g = json.loads(out)
    except Exception:
        return []
    released = []
    for it in g.get("intents", []):
        worker = it.get("worker")
        iid = it.get("id", "")
        if not worker or it.get("to") is not None or worker_alive(worker):
            continue
        rc, _ = run_sapper(tdir, ["release", iid, "--worker", worker], timeout=30)
        if rc == 0:
            released.append(f"{iid}（{worker}）")
    return released


def parse_limit(value) -> int | None:
    """把表单里的时间/步数转成正整数；空、0、非法值都视为不限。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        n = int(text)
    except ValueError:
        return None
    return n if n > 0 else None


def ui_state_file(tdir: Path) -> Path:
    return tdir / "ui.json"


def load_ui_state(tdir: Path) -> dict:
    try:
        data = json.loads(ui_state_file(tdir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_ui_state(tdir: Path, data: dict) -> None:
    path = ui_state_file(tdir)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def task_graph(tdir: Path) -> dict:
    code, out = run_sapper(tdir, ["graph", "--format", "json"], timeout=30)
    if code != 0:
        return {}
    try:
        return json.loads(out)
    except ValueError:
        return {}


def graph_targets(graph: dict) -> list[str]:
    targets = []
    for f in graph.get("facts", []):
        if f.get("kind") != "host":
            continue
        addr = (f.get("data") or {}).get("addr")
        if addr and addr not in targets:
            targets.append(addr)
    return targets


def task_info(tdir: Path) -> dict:
    """汇总任务当前配置：目标、靶标、描述、提示、运行限制。"""
    project = {}
    try:
        project = json.loads((tdir / ".sapper" / "project.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    ui = load_ui_state(tdir)
    graph = task_graph(tdir)
    desc_path = tdir / "desc.md"
    current_desc = ui.get("desc")
    if current_desc is None and desc_path.is_file():
        current_desc = desc_path.read_text(encoding="utf-8")
    return {
        "name": tdir.name,
        "goal": project.get("goal") or ui.get("goal") or "",
        "status": project.get("status") or "",
        "targets": ui.get("targets") or graph_targets(graph),
        "desc": current_desc or "",
        "hints": [h.get("content", "") for h in graph.get("hints", [])],
        "minutes": ui.get("minutes"),
        "max_steps": ui.get("max_steps"),
    }


def update_goal(tdir: Path, goal: str) -> None:
    """同步修改 project.json 与 goal fact（sapper 校验要求两者一致）。"""
    targets = [
        (tdir / ".sapper" / "project.json", "goal"),
        (tdir / ".sapper" / "facts" / "goal.json", "description"),
    ]
    for path, key in targets:
        data = json.loads(path.read_text(encoding="utf-8"))
        data[key] = goal
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)


# ---------- 配置可视化 ----------
SECRET_PAT = re.compile(r"(token|key|secret|password)", re.I)


def mask_value(val: str) -> str:
    """密钥类值只留末 4 位；服务端永不向浏览器回传完整密钥。"""
    val = val or ""
    if len(val) <= 8:
        return "***"
    return f"***{val[-4:]}"


def _cfg_env_items(keys: list[str]) -> list[dict]:
    items = []
    for k in keys:
        v = os.environ.get(k, "")
        items.append({
            "key": k,
            "value": ("（未设置）" if not v
                      else mask_value(v) if SECRET_PAT.search(k) else v),
            "secret": bool(v) and bool(SECRET_PAT.search(k)),
            "set": bool(v),
        })
    return items


def deploy_info() -> dict:
    return {
        "mode": DEPLOY["mode"],
        "image": DEPLOY["image"],
        "network": DEPLOY["network"],
        "docker_ok": docker_ok(),
        "image_ok": image_ok(DEPLOY["image"]) if DEPLOY["mode"] == "docker" else None,
        "settings_file": str(SETTINGS_FILE),
    }


def _config_info() -> dict:
    pool_raw = os.environ.get("SAPPER_KEY_POOL", "")
    pool = []
    if pool_raw:
        try:
            arr = json.loads(pool_raw)
            pool = [{"base_url": e.get("base_url", ""),
                     "model": e.get("model", ""),
                     "token": mask_value(e.get("token", ""))}
                    for e in arr if isinstance(e, dict)]
        except ValueError:
            pool = [{"error": "SAPPER_KEY_POOL 不是合法 JSON"}]
    return {
        # 兼容旧版前端的扁平字段
        "env_file": CURRENT_ENV_FILE,
        "model": os.environ.get("SAPPER_MODEL", ""),
        "base_url": os.environ.get("ANTHROPIC_BASE_URL", ""),
        "engine": ENGINE_MODE,
        "engine_bin": engine_bin(),
        "env_bin": os.environ.get("SAPPER_BIN", ""),
        "agent_py": str(AGENT_PY),
        "agent_exists": AGENT_PY.is_file(),
        "engines": ENGINES,
        # 分组可视化
        "deploy": deploy_info(),
        "groups": {
            "model": _cfg_env_items(["SAPPER_MODEL", "ANTHROPIC_BASE_URL",
                                     "ANTHROPIC_AUTH_TOKEN"]),
            "limits": _cfg_env_items(["SAPPER_TIMEOUT", "SAPPER_RETRY_MODEL",
                                      "SAPPER_TOTAL_MINUTES",
                                      "SAPPER_MINUTES_PER_CHALLENGE",
                                      "SAPPER_MAX_STEPS",
                                      "SAPPER_AGENT_MAX_SECONDS"]),
        },
        "pool": pool,
        "env_file_pairs": [
            {"key": k, "value": mask_value(v) if SECRET_PAT.search(k) else v,
             "secret": bool(SECRET_PAT.search(k))}
            for k, v in (parse_env_file(Path(CURRENT_ENV_FILE)).items()
                         if CURRENT_ENV_FILE else [])
        ],
        "diagnostics": diagnostics(),
        "sapper_version": sapper_version(),
        "sapper_cmd": " ".join(SAPPER_CMD),
    }


# ---------- 解题报告 ----------
FLAG_STATUS_ZH = {"recorded": "已记录", "submitted": "已提交 ✓", "rejected": "提交被拒"}
INTENT_MARK = {"done": "✅", "claimed": "🔧", "open": "☐"}


def extract_flags(graph: dict) -> list[dict]:
    out = []
    for f in (graph or {}).get("facts", []):
        if f.get("kind") != "flag":
            continue
        d = f.get("data") or {}
        out.append({
            "id": f.get("id", ""),
            "value": d.get("value", ""),
            "status": d.get("status", "recorded"),
            "challenge": d.get("challenge", ""),
            "created_at": f.get("created_at", ""),
        })
    return out


def _md_esc(s: str) -> str:
    """表格单元格转义：竖线会破坏 Markdown 表格。"""
    return (s or "").replace("|", "\\|").replace("\r", "")


def _fmt_time(ts: str) -> str:
    return (ts or "").replace("T", " ").replace("Z", " UTC") or "—"


def generate_report(tdir: Path) -> dict:
    """汇总任务信息 + 图谱，产出 Markdown 解题报告（不落盘）。"""
    info = task_info(tdir)
    g = task_graph(tdir)
    flags = extract_flags(g)
    facts = [f for f in g.get("facts", []) if f.get("id") != "goal"]
    facts.sort(key=lambda f: f.get("created_at") or "")
    intents = g.get("intents", [])
    name = tdir.name
    now = time.strftime("%Y-%m-%d %H:%M:%S")

    L: list[str] = []
    L.append(f"# {name} · 解题报告\n")
    L.append(f"> 由 sapper-ui 自动生成于 {now} · "
             f"数据来源：任务图谱（{len(facts)} facts / {len(intents)} intents）\n")

    L.append("## 基本信息\n")
    L.append("| 项目 | 内容 |")
    L.append("|---|---|")
    L.append(f"| 目标 | {_md_esc(info.get('goal')) or '—'} |")
    targets = info.get("targets") or []
    L.append(f"| 靶标 | {_md_esc('、'.join(targets)) or '—'} |")
    L.append(f"| 模型 | {_md_esc(os.environ.get('SAPPER_MODEL') or '(未设置)')} |")
    minutes = info.get("minutes")
    max_steps = info.get("max_steps")
    L.append(f"| 运行限制 | 时间 {minutes if minutes else '不限'} 分钟 / "
             f"step {max_steps if max_steps else '不限'} |")
    L.append("")

    L.append(f"## Flag（{len(flags)}）\n")
    if flags:
        for f in flags:
            st = FLAG_STATUS_ZH.get(f["status"], f["status"])
            L.append(f"- `{_md_esc(f['value'])}` — {st}"
                     f"（{f['id']}，{_fmt_time(f['created_at'])}）")
    else:
        L.append("（尚未捕获 flag）")
    L.append("")

    L.append("## 题目描述\n")
    L.append(((info.get("desc") or "").strip() or "（未填写）") + "\n")

    hints = info.get("hints") or []
    L.append("## 提示\n")
    for h in hints:
        L.append(f"- {_md_esc(h)}")
    if not hints:
        L.append("（无）")
    L.append("")

    L.append("## 解题时间线\n")
    if facts:
        L.append("| # | 时间 | 类型 | 记录 |")
        L.append("|---|---|---|---|")
        for i, f in enumerate(facts, 1):
            title = (f.get("title") or f.get("description") or "")[:60]
            L.append(f"| {i} | {_fmt_time(f.get('created_at'))} | "
                     f"{f.get('kind', 'note')} | {_md_esc(title)} |")
        L.append("")
        L.append("### 详细记录\n")
        for f in facts:
            title = f.get("title") or "(untitled)"
            L.append(f"**[{f.get('kind', 'note')}] {_md_esc(title)}**"
                     f"（{_fmt_time(f.get('created_at'))}）\n")
            desc = (f.get("description") or "").strip()
            if desc and desc != title:
                L.append(_md_esc(desc) + "\n")
            data = f.get("data") or {}
            if data:
                dims = "、".join(f"{k}={v}" for k, v in data.items())
                L.append(f"- 维度：{_md_esc(dims)}\n")
    else:
        L.append("（图谱为空）\n")

    L.append("## 任务 intents\n")
    if intents:
        for it in intents:
            st = it.get("status", "open")
            L.append(f"- {INTENT_MARK.get(st, '·')} **[{st}]** "
                     f"{_md_esc(it.get('title') or '')} — "
                     f"{_md_esc((it.get('description') or '')[:120])}")
    else:
        L.append("（无）")
    L.append("")

    attach_dir = tdir / "attachments"
    files = sorted(p for p in attach_dir.iterdir() if p.is_file()) \
        if attach_dir.is_dir() else []
    L.append("## 附件\n")
    if files:
        for p in files:
            L.append(f"- {p.name}（{p.stat().st_size} 字节）")
    else:
        L.append("（无）")
    L.append("")
    L.append("---\n*本报告由 sapper-ui 基于任务图谱自动生成；"
             "交互式图谱见界面「图谱」页或 `sapper view`。*")

    return {
        "markdown": "\n".join(L),
        "flag_count": len(flags),
        "flags": flags,
        "path": str(tdir / "writeup.md"),
        "facts": len(facts),
        "intents": len(intents),
    }


def save_report(tdir: Path) -> dict:
    """生成并写入 writeup.md；flag 数有增长时把报告登记为图谱 fact（幂等）。"""
    rep = generate_report(tdir)
    if rep["flag_count"] == 0:
        raise ValueError("还没有捕获 flag，无法生成解题报告")
    path = tdir / "writeup.md"
    path.write_text(rep["markdown"], encoding="utf-8")
    ui = load_ui_state(tdir)
    if (ui.get("writeup") or {}).get("flags") != rep["flag_count"]:
        code, out = run_sapper(tdir, [
            "fact", f"解题报告（{rep['flag_count']} 个 flag）",
            "-k", "note", "-t", "解题报告", "--doc-file", str(path),
        ])
        if code == 0:
            ui["writeup"] = {"flags": rep["flag_count"],
                             "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            save_ui_state(tdir, ui)
        else:
            rep["fact_warning"] = f"报告已保存，登记图谱失败: {out[:200]}"
    rep["saved"] = True
    return rep


def maybe_autoreport(name: str, tdir: Path) -> None:
    """auto 进程退出后调用：已有 flag 且还没写过报告时自动生成。"""
    try:
        rep = generate_report(tdir)
        if rep["flag_count"] and not (tdir / "writeup.md").exists():
            r = save_report(tdir)
            append_log(name, f"[ui] 检测到 {r['flag_count']} 个 flag，"
                             f"已自动生成解题报告: {r['path']}")
    except Exception as exc:
        append_log(name, f"[ui] 自动生成解题报告失败: {exc}")


def safe_filename(raw: str) -> str:
    name = Path(raw.replace("\\", "/")).name.strip()
    # Windows 保留字符 + 控制字符一并去掉
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    name = name.rstrip(". ")
    if (not name or name in (".", "..")
            or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", name)):
        raise ValueError("非法文件名")
    return name[:128]


def resolve_sapper_cmd(cli_bin: str | None) -> list[str]:
    """确定 sapper 启动命令：--sapper-bin > SAPPER_UI_BIN > PATH 里的 sapper。

    Windows 上 sapper 通常是 sapper.py / sapper.bat，需要显式用 python 解释器执行。
    """
    global SAPPER_CMD
    bin_path = cli_bin or os.environ.get("SAPPER_UI_BIN") or "sapper"
    if bin_path != "sapper":
        bin_path = str(Path(bin_path).expanduser())
    if bin_path.endswith(".py"):
        SAPPER_CMD = [sys.executable, bin_path]
    else:
        if bin_path == "sapper" and IS_WIN and not shutil.which("sapper"):
            found = shutil.which("sapper.py") or shutil.which("sapper.bat")
            if found:
                bin_path = found
        if bin_path.endswith(".py"):
            SAPPER_CMD = [sys.executable, bin_path]
        else:
            SAPPER_CMD = [bin_path]
    return SAPPER_CMD


class Handler(BaseHTTPRequestHandler):
    server_version = "sapper-ui/1.1"

    def log_message(self, fmt, *args):  # 安静一点
        pass

    # ---------- 安全与工具 ----------
    def _guard(self) -> bool:
        """DNS rebinding / 跨站防护：Host 必须是回环地址；带 Origin 时必须同源。

        curl 等无 Origin 的客户端放行；浏览器发起的跨站请求必带 Origin，会被 403。
        """
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        if host not in LOOPBACK_HOSTS:
            self._json({"error": f"非法 Host: {host}（只允许回环访问）"}, 403)
            return False
        origin = self.headers.get("Origin")
        if origin:
            ohost = (urlparse(origin).netloc or "").rsplit(":", 1)[0].strip("[]").lower()
            if ohost not in LOOPBACK_HOSTS:
                self._json({"error": "跨站请求被拒绝（Origin 校验失败）"}, 403)
                return False
        return True

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text, code=200, ctype="text/html; charset=utf-8"):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 10 * 1024 * 1024:
            raise ValueError("请求体超过 10MB 上限")
        return json.loads(self.rfile.read(n) or b"{}")

    # ---------- GET ----------
    def do_GET(self):
        try:
            if not self._guard():
                return
            self._route_get()
        except Exception as exc:
            try:
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            except Exception:
                pass

    def _route_get(self):
        path = urlparse(self.path).path
        q = parse_qs(urlparse(self.path).query)
        tasks_root = Path(self.server.tasks_root)

        if path == "/" or path == "/index.html":
            return self._html(INDEX_HTML)
        if path == "/api/tasks":
            items = []
            if tasks_root.exists():
                for d in sorted(tasks_root.iterdir()):
                    if not d.is_dir():
                        continue
                    with LOCK:
                        entry = RUNNING.get(d.name)
                        proc = entry["proc"] if entry else None
                        running = bool(proc and proc.poll() is None)
                    items.append({"name": d.name, "running": running})
            return self._json({"tasks": items})

        if path == "/api/config":
            return self._json(_config_info())

        if path == "/api/deploy":
            return self._json(deploy_info())

        m = re.fullmatch(r"/api/task/([^/]+)", path)
        if m:
            tdir = task_dir(tasks_root, unquote(m.group(1)))
            if not (tdir / ".sapper").exists():
                return self._json({"error": "任务不存在"}, 404)
            return self._json(task_info(tdir))

        m = re.fullmatch(r"/api/running/([^/]+)", path)
        if m:
            name = unquote(m.group(1))
            with LOCK:
                entry = RUNNING.get(name)
                proc = entry["proc"] if entry else None
                running = bool(proc and proc.poll() is None)
            resp = {"running": running}
            if DEPLOY["mode"] == "docker":
                resp["container"] = container_status(name)
            return self._json(resp)

        m = re.fullmatch(r"/api/log/([^/]+)", path)
        if m:
            name = unquote(m.group(1))
            with LOCK:
                entry = RUNNING.get(name)
                proc = entry["proc"] if entry else None
                running = bool(proc and proc.poll() is None)
                text = "\n".join(entry["log"]) if entry else ""
            return self._json({"running": running, "log": text})

        m = re.fullmatch(r"/api/status/([^/]+)", path)
        if m:
            tdir = task_dir(tasks_root, unquote(m.group(1)))
            if not tdir.exists():
                return self._json({"error": "任务不存在"}, 404)
            _, status = run_sapper(tdir, ["status"])
            _, graph = run_sapper(tdir, ["graph", "--format", "json"], timeout=30)
            gj = None
            try:
                gj = json.loads(graph)
            except Exception:
                pass
            attach_dir = tdir / "attachments"
            attachments = (
                [{"name": p.name, "size": p.stat().st_size}
                 for p in sorted(attach_dir.iterdir()) if p.is_file()]
                if attach_dir.is_dir() else []
            )
            return self._json({"status": status, "graph": gj, "graph_raw": graph,
                               "attachments": attachments})

        m = re.fullmatch(r"/api/flags/([^/]+)", path)
        if m:
            tdir = task_dir(tasks_root, unquote(m.group(1)))
            if not (tdir / ".sapper").exists():
                return self._json({"flags": []})
            _, graph = run_sapper(tdir, ["graph", "--format", "json"], timeout=30)
            try:
                flags = extract_flags(json.loads(graph))
            except Exception:
                flags = []
            return self._json({"flags": flags})

        m = re.fullmatch(r"/api/report/([^/]+)", path)
        if m:
            tdir = task_dir(tasks_root, unquote(m.group(1)))
            if not (tdir / ".sapper").exists():
                return self._json({"error": "任务不存在"}, 404)
            return self._json(generate_report(tdir))

        m = re.fullmatch(r"/view/([^/]+)", path)
        if m:
            name = unquote(m.group(1))
            tdir = task_dir(tasks_root, name)
            if not tdir.exists():
                return self._html("<h1>任务不存在</h1>", 404)
            # --print 只输出 HTML，不会像 --out 那样调用 xdg-open 打开浏览器
            code, html = run_sapper(tdir, ["view", "--print"], timeout=60, merge_stderr=False)
            if code == 0 and "<html" in html.lower():
                return self._html(html)
            return self._html("<h1>图谱尚未生成（还没有 .sapper）</h1>", 404)

        return self._html("<h1>404</h1>", 404)

    # ---------- POST ----------
    def do_POST(self):
        try:
            if not self._guard():
                return
            self._route_post()
        except Exception as exc:
            try:
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            except Exception:
                pass

    def _route_post(self):
        global CURRENT_ENV_FILE, ENGINE_MODE, DEPLOY
        path = urlparse(self.path).path
        tasks_root = Path(self.server.tasks_root)

        if path == "/api/create":
            try:
                b = self._body()
                name = str(b.get("name") or "").strip()
                tdir = task_dir(tasks_root, name)
                if tdir.exists():
                    return self._json({"error": f"任务目录已存在: {tdir}"}, 409)
                tdir.mkdir(parents=True)
                if DEPLOY["mode"] == "docker":
                    ok, msg = ensure_container(tdir, name)
                    if not ok:
                        tdir.rmdir()  # 留着空目录会让重试撞 409
                        return self._json({"error": msg}, 500)
                goal = (b.get("goal") or "解出本题并捕获 flag").strip() or "解出本题并捕获 flag"
                desc = (b.get("desc") or "").strip()
                targets = [t.strip() for t in re.split(r"[\n,;]+", b.get("targets") or "") if t.strip()]
                hint = (b.get("hint") or "").strip()

                logs = []
                code, out = run_sapper(tdir, ["setup", "--goal", goal, "--agents"])
                logs.append(f"$ sapper setup --goal \"{goal}\" --agents\n{out}")
                if code != 0:
                    return self._json({"error": "setup 失败", "log": logs}, 500)

                if desc:
                    (tdir / "desc.md").write_text(desc, encoding="utf-8")
                    summary = re.sub(r"\s+", " ", desc)[:200]
                    code, out = run_sapper(tdir, [
                        "fact", summary, "-k", "note", "-t", "题目描述",
                        "--doc-file", str(tdir / "desc.md"), "-d", f"challenge={name}",
                    ])
                    logs.append(f"$ sapper fact <题目描述 note>\n{out}")
                for t in targets:
                    code, out = run_sapper(tdir, [
                        "fact", f"靶标 {t}", "-k", "host", "-t", "靶标", "-d", f"addr={t}",
                    ])
                    logs.append(f"$ sapper fact <靶标 {t}>\n{out}")
                if hint:
                    code, out = run_sapper(tdir, ["hint", hint, "--creator", "ui"])
                    logs.append(f"$ sapper hint\n{out}")
                save_ui_state(tdir, {
                    "goal": goal, "desc": desc, "targets": targets, "hint": hint,
                    "minutes": parse_limit(b.get("minutes")),
                    "max_steps": parse_limit(b.get("max_steps")),
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                })
                return self._json({"ok": True, "log": "\n\n".join(logs)})
            except Exception as exc:
                return self._json({"error": str(exc)}, 500)

        if path == "/api/upload":
            try:
                q = parse_qs(urlparse(self.path).query)
                name = unquote(q.get("task", [""])[0])
                tdir = task_dir(tasks_root, name)
                if not (tdir / ".sapper").exists():
                    return self._json({"error": "任务不存在，请先创建"}, 400)
                fname = safe_filename(unquote(q.get("filename", [""])[0]))
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0:
                    return self._json({"error": "空文件"}, 400)
                if length > MAX_UPLOAD:
                    return self._json({"error": f"附件超过上限 {MAX_UPLOAD // (1024*1024)}MB"}, 413)
                attach_dir = tdir / "attachments"
                attach_dir.mkdir(exist_ok=True)
                stem, suffix = Path(fname).stem, Path(fname).suffix
                dest = attach_dir / fname
                i = 1
                # "xb" 独占创建，并发上传同名文件不会互相覆盖
                while True:
                    try:
                        f = open(dest, "xb")
                        break
                    except FileExistsError:
                        dest = attach_dir / f"{stem}-{i}{suffix}"
                        i += 1
                with f:
                    remaining = length
                    while remaining > 0:
                        chunk = self.rfile.read(min(1 << 20, remaining))
                        if not chunk:
                            break
                        f.write(chunk)
                        remaining -= len(chunk)
                if remaining:
                    dest.unlink(missing_ok=True)
                    return self._json({"error": "上传中断，文件不完整"}, 400)
                code, out = run_sapper(tdir, [
                    "fact", f"附件 {dest.name}（{length} 字节），路径 {dest}",
                    "-k", "note", "-t", "附件",
                    "-d", f"path={dest}", "-d", f"rel=attachments/{dest.name}",
                    "-d", f"size={length}",
                ])
                if code != 0:
                    return self._json({"error": f"附件已保存但登记图谱失败: {out}"}, 500)
                return self._json({"ok": True, "name": dest.name, "size": length})
            except Exception as exc:
                return self._json({"error": str(exc)}, 500)

        if path == "/api/config":
            try:
                b = self._body()
                if "engine" in b:
                    eng = str(b.get("engine") or "default").strip() or "default"
                    if eng not in ENGINES:
                        return self._json({"error": f"未知引擎: {eng}"}, 400)
                    if eng == "agent" and not AGENT_PY.is_file():
                        return self._json({"error": f"找不到 sapper-agent: {AGENT_PY}"}, 400)
                    ENGINE_MODE = eng
                if "env_file" in b:
                    p = str(b.get("env_file") or "").strip()
                    if p:
                        fp = Path(p).expanduser()
                        if not fp.is_file():
                            return self._json({"error": f"文件不存在: {fp}"}, 400)
                        load_env_file(fp, override=True)
                        CURRENT_ENV_FILE = str(fp)
                    else:
                        CURRENT_ENV_FILE = ""
                return self._json(_config_info())
            except Exception as exc:
                return self._json({"error": str(exc)}, 500)

        if path == "/api/deploy":
            try:
                b = self._body()
                mode = str(b.get("mode") or "").strip()
                if mode:
                    if mode not in ("direct", "docker"):
                        return self._json({"error": f"未知部署模式: {mode}"}, 400)
                    if mode == "docker" and not docker_ok():
                        return self._json({"error": "Docker 不可用（未安装或守护进程未启动）"}, 400)
                    DEPLOY["mode"] = mode
                    _CONTAINER_OK.update(name=None, ts=0.0)
                if "image" in b:
                    img = str(b.get("image") or "").strip()
                    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./:-]{0,127}", img):
                        return self._json({"error": f"镜像名不合法: {img}"}, 400)
                    DEPLOY["image"] = img
                if "network" in b:
                    net = str(b.get("network") or "").strip()
                    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", net):
                        return self._json({"error": f"网络名不合法: {net}"}, 400)
                    DEPLOY["network"] = net
                save_settings()
                return self._json(deploy_info())
            except Exception as exc:
                return self._json({"error": str(exc)}, 500)

        if path == "/api/edit":
            try:
                b = self._body()
                name = str(b.get("name") or "")
                tdir = task_dir(tasks_root, name)
                if not (tdir / ".sapper").exists():
                    return self._json({"error": "任务不存在"}, 404)
                with LOCK:
                    entry = RUNNING.get(name)
                    if entry and entry["proc"] and entry["proc"].poll() is None:
                        return self._json({"error": "任务正在运行，请先停止再编辑"}, 409)
                ui = load_ui_state(tdir)
                before = task_info(tdir)
                msgs = []

                goal = str(b.get("goal") or "").strip()
                if goal and goal != before["goal"]:
                    update_goal(tdir, goal)
                    ui["goal"] = goal
                    msgs.append("目标已更新")

                if b.get("targets") is not None:
                    wanted = []
                    for t in re.split(r"[\n,;]+", str(b.get("targets") or "")):
                        t = t.strip()
                        if t and t not in wanted:
                            wanted.append(t)
                    existing = graph_targets(task_graph(tdir))
                    for t in wanted:
                        if t in existing:
                            continue
                        code, out = run_sapper(tdir, [
                            "fact", f"靶标 {t}", "-k", "host", "-t", "靶标", "-d", f"addr={t}",
                        ])
                        if code != 0:
                            return self._json({"error": f"登记靶标失败: {out}"}, 500)
                        existing.append(t)
                        msgs.append(f"已新增靶标 {t}")
                    ui["targets"] = existing

                desc = b.get("desc")
                if desc is not None:
                    desc = str(desc).strip()
                    if desc and desc != (before["desc"] or "").strip():
                        (tdir / "desc.md").write_text(desc, encoding="utf-8")
                        summary = re.sub(r"\s+", " ", desc)[:200]
                        code, out = run_sapper(tdir, [
                            "fact", summary, "-k", "note", "-t", "题目描述（更新）",
                            "--doc-file", str(tdir / "desc.md"), "-d", f"challenge={name}",
                        ])
                        if code != 0:
                            return self._json({"error": f"登记题目描述失败: {out}"}, 500)
                        ui["desc"] = desc
                        msgs.append("题目描述已追加记录")

                hint = str(b.get("hint") or "").strip()
                if hint:
                    code, out = run_sapper(tdir, ["hint", hint, "--creator", "ui"])
                    if code != 0:
                        return self._json({"error": f"添加提示失败: {out}"}, 500)
                    msgs.append("提示已追加")

                if "minutes" in b or "max_steps" in b:
                    ui["minutes"] = parse_limit(b.get("minutes"))
                    ui["max_steps"] = parse_limit(b.get("max_steps"))
                    msgs.append("运行限制已保存")
                ui["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                save_ui_state(tdir, ui)
                return self._json({"ok": True, "messages": msgs, "task": task_info(tdir)})
            except Exception as exc:
                return self._json({"error": str(exc)}, 500)

        if path == "/api/start":
            try:
                b = self._body()
                name = str(b.get("name") or "")
                tdir = task_dir(tasks_root, name)
                if not (tdir / ".sapper").exists():
                    return self._json({"error": "该任务还没有图谱，请先创建"}, 400)
                with LOCK:
                    entry = RUNNING.get(name)
                    if entry and entry["proc"] and entry["proc"].poll() is None:
                        return self._json({"error": "该任务已在运行中"}, 409)
                released = release_stale_claims(tdir)
                for r in released:
                    append_log(name, f"[ui] 已释放残留 claim: {r}")
                ui = load_ui_state(tdir)
                minutes = parse_limit(b["minutes"]) if "minutes" in b else parse_limit(ui.get("minutes"))
                max_steps = parse_limit(b["max_steps"]) if "max_steps" in b else parse_limit(ui.get("max_steps"))
                # 自检引擎按平台取默认值（旧字段 engine_override 仍兼容）
                engine_override = (b.get("engine_override") or "").strip() or None
                if b.get("selftest"):
                    engine_override = "true" if IS_POSIX else "cmd /c exit 0"
                start_auto(
                    name, tdir,
                    minutes=minutes,
                    max_steps=max_steps,
                    skip_permissions=bool(b.get("skip_permissions", True)),
                    engine_override=engine_override,
                )
                return self._json({"ok": True, "released": released})
            except Exception as exc:
                return self._json({"error": str(exc)}, 500)

        if path == "/api/report":
            try:
                b = self._body()
                name = str(b.get("name") or "")
                tdir = task_dir(tasks_root, name)
                if not (tdir / ".sapper").exists():
                    return self._json({"error": "任务不存在"}, 404)
                rep = save_report(tdir)
                return self._json(rep)
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)
            except Exception as exc:
                return self._json({"error": str(exc)}, 500)

        if path == "/api/stop":
            try:
                b = self._body()
                ok = stop_auto(str(b.get("name") or ""))
                return self._json({"ok": ok})
            except Exception as exc:
                return self._json({"error": str(exc)}, 500)

        return self._json({"error": "unknown endpoint"}, 404)


def main():
    global CURRENT_ENV_FILE, ENGINE_MODE
    ap = argparse.ArgumentParser(description="sapper-ui 本地控制台")
    ap.add_argument("--port", type=int, default=8788)  # 8765 是 sapper view --serve 的默认端口，避开
    ap.add_argument("--tasks-dir", default=str(Path.home() / "ctf" / "tasks"))
    ap.add_argument("--sapper-bin", default=None, help="sapper 可执行文件路径（默认取 PATH）")
    ap.add_argument("--env-file", default=None,
                    help="模型 env 配置文件路径（默认 ~/ctf/sapper.env，不存在则跳过）")
    ap.add_argument("--engine", choices=list(ENGINES), default=os.environ.get("SAPPER_UI_ENGINE"),
                    help="Agent 引擎（界面里也能切换）")
    args = ap.parse_args()
    if args.engine:
        ENGINE_MODE = args.engine
    resolve_sapper_cmd(args.sapper_bin)
    print(f"[sapper-ui] sapper 命令: {' '.join(SAPPER_CMD)}")

    load_settings()
    print(f"[sapper-ui] 部署模式: {DEPLOY['mode']}"
          + (f"（镜像 {DEPLOY['image']}，网络 {DEPLOY['network']}）"
             if DEPLOY["mode"] == "docker" else ""))

    env_arg = (args.env_file or os.environ.get("SAPPER_UI_ENV")
               or str(Path.home() / "ctf" / "sapper.env"))
    fp = Path(env_arg).expanduser()
    if fp.is_file():
        load_env_file(fp)
        CURRENT_ENV_FILE = str(fp)
    else:
        print(f"[sapper-ui] 未加载 env 文件（{fp} 不存在）；"
              "可用 --env-file / SAPPER_UI_ENV / 界面「模型配置」指定")
    model = os.environ.get("SAPPER_MODEL", "")
    base = os.environ.get("ANTHROPIC_BASE_URL", "")
    print(f"[sapper-ui] 模型配置: SAPPER_MODEL={model or '(未设置!)'}  BASE_URL={base or '(未设置!)'}")
    if DEPLOY["mode"] == "docker":
        print("[sapper-ui] docker 模式: 引擎为容器内置 claude CLI，宿主机 SAPPER_BIN/引擎切换不生效")
    else:
        print(f"[sapper-ui] 引擎: {ENGINES[ENGINE_MODE]}  "
              f"env SAPPER_BIN={os.environ.get('SAPPER_BIN') or '(未设置)'}")
    if ENGINE_MODE == "agent" and not AGENT_PY.is_file():
        print(f"[sapper-ui] 警告: 找不到 sapper-agent ({AGENT_PY})，启动任务会失败")
    for w in diagnostics():
        print(f"[sapper-ui] 警告: {w}")
    tasks_root = Path(args.tasks_dir).expanduser().resolve()
    tasks_root.mkdir(parents=True, exist_ok=True)

    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    httpd.tasks_root = tasks_root
    print(f"[sapper-ui] http://127.0.0.1:{args.port}   任务目录: {tasks_root}")
    print("[sapper-ui] 仅限授权目标使用。Ctrl+C 退出（运行中的 auto 会一并终止）。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[sapper-ui] bye")


if __name__ == "__main__":
    main()
