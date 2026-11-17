#!/usr/bin/env python3
"""Token 管理台（离线比赛用，纯 Python 标准库）

围绕「单平台单模型 + N 条 key」设计：
  - 档位（pi 引擎 provider）：~/.pi/agent/models.json，key 实际存放处
  - 当前模型：~/ctf/sapper.env 的 SAPPER_MODEL=档位名/模型名
  - key 池：sapper.env 的 SAPPER_KEY_POOL（sapper 侧 failover，额度/欠费自动轮换）
  - 测试：对每条 key 发最小 chat completion（max_tokens=4），按错误分类展示

只监听 127.0.0.1。改配置前自动备份 sapper.env。
"""
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, unquote

ENV_PATH = Path(os.environ.get("TOKEN_CONSOLE_ENV") or Path.home() / "ctf" / "sapper.env")
PI_PATH = Path(os.environ.get("TOKEN_CONSOLE_PI") or Path.home() / ".pi" / "agent" / "models.json")
HOST = "127.0.0.1"
PORT = int(os.environ.get("TOKEN_CONSOLE_PORT") or "8790")

BUDGET_KEYS = ["SAPPER_TOTAL_MINUTES", "SAPPER_MINUTES_PER_CHALLENGE", "SAPPER_MAX_STEPS",
               "SAPPER_RETRY_MODEL", "SAPPER_TIMEOUT", "SAPPER_AGENT_MAX_SECONDS"]
TEST_TIMEOUT = 20          # 单 key 冒烟超时（秒）
TEST_MAX_TOKENS = 4        # 冒烟尽量少花 token

TLS_CTX = ssl.create_default_context()
TLS_CTX.check_hostname = False
TLS_CTX.verify_mode = ssl.CERT_NONE   # 赛场内网自签证书也能测


# ---------------------------------------------------------------- sapper.env 读写
def env_read() -> str:
    return ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.is_file() else ""


def env_get(text: str, key: str) -> str:
    m = re.search(rf"^export {re.escape(key)}=(.*)$", text, flags=re.M)
    if not m:
        return ""
    v = m.group(1).strip()
    v = re.sub(r"\s+#.*$", "", v)          # 去行尾注释
    return v.strip("'\"")


def env_set(text: str, key: str, value: str, multiline: bool = False) -> str:
    """原位替换 export（保留其余所有行与注释）。multiline=True 时按
    「单引号多行块」匹配（如 SAPPER_KEY_POOL='[...多行...]'），替换为单行。"""
    line = f"export {key}={value}"
    if multiline:
        pat = rf"^export {re.escape(key)}='.*?'[ \t]*$"
        if re.search(pat, text, flags=re.M | re.S):
            return re.sub(pat, lambda _: line, text, flags=re.M | re.S)
        return text.rstrip("\n") + "\n" + line + "\n"
    if re.search(rf"^export {re.escape(key)}=.*$", text, flags=re.M):
        return re.sub(rf"^export {re.escape(key)}=.*$", lambda _: line, text, flags=re.M)
    return text.rstrip("\n") + "\n" + line + "\n"


def env_write(text: str) -> None:
    if ENV_PATH.is_file():
        bak = ENV_PATH.with_suffix(".env.bak-" + time.strftime("%Y%m%d-%H%M%S"))
        bak.write_text(ENV_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        # 只保留最近 5 份备份
        for old in sorted(ENV_PATH.parent.glob(ENV_PATH.name + ".bak-*"))[:-5]:
            try:
                old.unlink()
            except OSError:
                pass
    ENV_PATH.write_text(text, encoding="utf-8")
    try:
        ENV_PATH.chmod(0o600)
    except OSError:
        pass


def mask(key: str) -> str:
    if not key:
        return ""
    return key[:6] + "…" + key[-4:] if len(key) > 14 else key[:2] + "…"


# ---------------------------------------------------------------- pi providers
def pi_read() -> dict:
    if not PI_PATH.is_file():
        return {}
    try:
        return json.loads(PI_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def pi_providers() -> dict:
    return pi_read().get("providers", {}) or {}


def pi_upsert(name: str, base_url: str, api_key: str, model: str) -> None:
    data = pi_read() if PI_PATH.is_file() else {}
    providers = data.setdefault("providers", {})
    old = providers.get(name, {})
    models = old.get("models", [])
    for m in models:
        if isinstance(m, dict) and m.get("id") == model:
            break
    else:
        models = [{"id": model, "name": model, "reasoning": True,
                   "input": ["text"], "contextWindow": 128000, "maxTokens": 32768}]
    providers[name] = {"name": name, "baseUrl": base_url, "api": "openai-completions",
                       "apiKey": api_key, "models": models}
    if PI_PATH.is_file():
        bak = PI_PATH.with_suffix(".json.bak-" + time.strftime("%Y%m%d-%H%M%S"))
        bak.write_text(PI_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    PI_PATH.parent.mkdir(parents=True, exist_ok=True)
    PI_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def pool_parse(text: str) -> list:
    """SAPPER_KEY_POOL 可能是多行单引号 JSON 块，整块提取再解析。"""
    m = re.search(r"^export SAPPER_KEY_POOL='(.*?)'[ \t]*$", text, flags=re.M | re.S)
    raw = (m.group(1) if m else env_get(text, "SAPPER_KEY_POOL")).strip()
    if not raw:
        return []
    try:
        entries = json.loads(raw)
        return [e for e in entries if isinstance(e, dict)]
    except Exception:
        return []


# ---------------------------------------------------------------- 冒烟测试
def err_class(status: int, body: str, exc: str) -> str:
    low = (body or "").lower()
    if exc:
        if "timed out" in exc or "timeout" in exc:
            return "超时"
        if "connectionrefused" in exc.replace(" ", ""):
            return "连接拒绝"
        if "name or service not known" in exc or "nodename" in exc:
            return "域名解析失败"
        return "网络错误"
    if any(k in low for k in ["insufficient", "余额", "balance", "quota", "欠费", "arrears"]):
        return "额度不足"
    if status in (401, 403):
        return "key 无效/未授权"
    if status == 404:
        return "路径或模型不存在"
    if status == 429:
        return "限流"
    if status >= 500:
        return "平台错误"
    if status:
        return f"HTTP {status}"
    return "未知"


def smoke(base_url: str, key: str, model: str) -> dict:
    """对 base_url 发最小 chat completion。base_url 缺 /v1 时自动补试一次。
    返回 base_used = 实际打通的 base（写配置用它，别从 url 反推）。"""
    t0 = time.time()
    b = base_url.rstrip("/")
    attempts = [(b, b + "/chat/completions")]
    if not re.search(r"/v\d+$", b):
        attempts.append((b + "/v1", b + "/v1/chat/completions"))
    note = "自动补 /v1 重试" if len(attempts) > 1 else ""
    last = {"ok": False, "http": 0, "err_class": "未知", "detail": "", "latency_ms": 0,
            "tokens": None, "reply": "", "url": "", "base_used": b, "note": ""}
    for i, (cand, url) in enumerate(attempts):
        final = i == len(attempts) - 1
        body = json.dumps({"model": model, "stream": False, "max_tokens": TEST_MAX_TOKENS,
                           "messages": [{"role": "user", "content": "ping"}]}).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + key})
        try:
            with urllib.request.urlopen(req, timeout=TEST_TIMEOUT, context=TLS_CTX) as r:
                text = r.read().decode("utf-8", "replace")
                ms = int((time.time() - t0) * 1000)
                try:
                    d = json.loads(text)
                except Exception:
                    d = {}
                usage = d.get("usage") or {}
                ch = ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                return {"ok": True, "http": r.status, "err_class": "", "detail": "",
                        "latency_ms": ms, "tokens": usage or None,
                        "reply": ch[:80], "url": url, "base_used": cand,
                        "note": note if final and len(attempts) > 1 else ""}
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")[:300]
            cls = err_class(e.code, text, "")
            last = {"ok": False, "http": e.code, "err_class": cls, "detail": text,
                    "latency_ms": int((time.time() - t0) * 1000), "tokens": None,
                    "reply": "", "url": url, "base_used": cand,
                    "note": note if final and len(attempts) > 1 else ""}
            if cls != "路径或模型不存在" or final:
                return last           # 非 404 或已重试到底：直接返回
        except Exception as exc:
            cls = err_class(0, "", f"{type(exc).__name__}: {exc}")
            last = {"ok": False, "http": 0, "err_class": cls, "detail": str(exc)[:300],
                    "latency_ms": int((time.time() - t0) * 1000), "tokens": None,
                    "reply": "", "url": url, "base_used": cand, "note": ""}
            if final:
                return last
    return last


def balance_probe(base_url: str, key: str) -> dict:
    """one-api 平台常见计费端点探测（不支持则静默返回 None）。"""
    o = urlparse(base_url if "://" in base_url else "http://" + base_url)
    origin = f"{o.scheme}://{o.netloc}"
    out = {}
    for name, path in [("quota", "/v1/dashboard/billing/subscription"),
                       ("usage", "/v1/dashboard/billing/usage")]:
        req = urllib.request.Request(origin + path, headers={"Authorization": "Bearer " + key})
        try:
            with urllib.request.urlopen(req, timeout=8, context=TLS_CTX) as r:
                out[name] = json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            out[name] = None
    return out


# ---------------------------------------------------------------- HTTP
INDEX = (Path(__file__).parent / "index.html").read_bytes() if \
    (Path(__file__).parent / "index.html").exists() else b"<h1>index.html missing</h1>"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, d, code=200):
        body = json.dumps(d, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/overview":
            try:
                return self._json(self.overview())
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        if path not in ("/", "/index.html"):
            return self._json({"error": "not found"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(INDEX)))
        self.end_headers()
        self.wfile.write(INDEX)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/overview":
                return self._json(self.overview())
            if path == "/api/provider/save":
                b = self._body()
                name = re.sub(r"[^a-z0-9_-]", "", str(b.get("name") or "").lower())
                base_url = str(b.get("base_url") or "").strip()
                key = str(b.get("key") or "").strip()
                model = str(b.get("model") or "").strip()
                if not (name and base_url and model):
                    return self._json({"error": "档位名/平台地址/模型名 必填（key 留空=沿用原 key）"}, 400)
                if not re.match(r"^https?://", base_url):
                    return self._json({"error": "平台地址须以 http:// 或 https:// 开头"}, 400)
                if not key:
                    key = pi_providers().get(name, {}).get("apiKey", "")
                if not key:
                    return self._json({"error": "该档位还没有 key，请填 API Key"}, 400)
                pi_upsert(name, base_url, key, model)
                return self._json({"ok": True, "overview": self.overview()})
            if path in ("/api/venue_setup", "/api/venue_restore"):
                b = self._body()
                script = Path.home() / "ctf" / "venue-setup.sh"
                if not script.is_file():
                    return self._json({"error": f"{script} 不存在"}, 404)
                env = dict(os.environ)
                # 勾选「同时配置 CTF-BTFly 备份链路」→ 脚本追加 daemon 阶段
                if b.get("btfly"):
                    env["VENUE_BTFLY"] = "1"
                # 沙箱测试口：可选指定 env/pi 路径（生产不传=真实配置）
                if b.get("env_path"):
                    env["VENUE_SAPPER_ENV"] = str(b["env_path"])
                if b.get("pi_path"):
                    env["VENUE_MODELS_JSON"] = str(b["pi_path"])
                if path == "/api/venue_restore":
                    cmd = ["bash", str(script), "--restore"]
                    timeout = 60
                else:
                    base = str(b.get("base_url") or "").strip()
                    key = str(b.get("key") or "").strip()
                    model = str(b.get("model") or "").strip()
                    if not (base and key and model):
                        return self._json({"error": "平台地址/API Key/模型名 全部必填"}, 400)
                    cmd = ["bash", str(script), base, key, model]
                    timeout = 300   # 含 pi 直连+引擎两重冒烟
                try:
                    p = subprocess.run(cmd, capture_output=True, text=True,
                                       timeout=timeout, env=env)
                except subprocess.TimeoutExpired as e:
                    def _s(x):
                        if x is None:
                            return ""
                        return x.decode("utf-8", "replace") if isinstance(x, bytes) else str(x)
                    out = (_s(e.stdout) + _s(e.stderr))[-4000:]
                    return self._json({"ok": False, "code": "timeout", "out": out + "\n[超时]"})
                out = (p.stdout + p.stderr)[-4000:]
                resp = {"ok": p.returncode == 0, "code": p.returncode, "out": out}
                if resp["ok"]:
                    resp["overview"] = self.overview()
                return self._json(resp)
            if path == "/api/provider/delete":
                b = self._body()
                name = str(b.get("name") or "").strip()
                data = pi_read()
                if name not in (data.get("providers", {}) or {}):
                    return self._json({"error": f"档位 {name} 不存在"}, 404)
                mp = PI_PATH
                if mp.is_file():
                    bak = mp.with_suffix(".json.bak-" + time.strftime("%Y%m%d-%H%M%S"))
                    bak.write_text(mp.read_text(encoding="utf-8"), encoding="utf-8")
                data["providers"].pop(name)
                mp.parent.mkdir(parents=True, exist_ok=True)
                mp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
                warn = ""
                if self.overview().get("cur_provider") == name:
                    warn = f"注意：SAPPER_MODEL 仍指向已删除的 {name}，请尽快「设为当前」其它档位"
                return self._json({"ok": True, "warn": warn, "overview": self.overview()})
            if path == "/api/provider/use":
                b = self._body()
                name = str(b.get("name") or "").strip()
                model = str(b.get("model") or "").strip()
                prov = pi_providers().get(name)
                if not prov:
                    return self._json({"error": f"档位 {name} 不存在"}, 404)
                if not model:
                    ms = [m.get("id") for m in prov.get("models", []) if isinstance(m, dict)]
                    model = ms[0] if ms else ""
                text = env_set(env_read(), "SAPPER_MODEL", f"{name}/{model}")
                env_write(text)
                return self._json({"ok": True, "overview": self.overview()})
            if path == "/api/pool/save":
                b = self._body()
                entries = b.get("entries")
                if not isinstance(entries, list):
                    return self._json({"error": "entries 必须是数组"}, 400)
                clean = []
                for e in entries:
                    e = {k: str(e.get(k) or "").strip().replace("'", "")
                         for k in ("name", "base_url", "model", "token")}
                    if e["base_url"]:
                        clean.append(e)
                # token 留空=沿用池里旧 token（按 name+base_url 匹配，再退 base_url）
                old_pool = pool_parse(env_read())
                old = {(x.get("name"), x.get("base_url")): x.get("token", "") for x in old_pool}
                old_bu = {x.get("base_url"): x.get("token", "") for x in old_pool}
                for e in clean:
                    if not e["token"]:
                        e["token"] = old.get((e["name"], e["base_url"])) or old_bu.get(e["base_url"], "")
                clean = [e for e in clean if e["token"]]
                text = env_set(env_read(), "SAPPER_KEY_POOL",
                               "'" + json.dumps(clean, ensure_ascii=False) + "'", multiline=True)
                env_write(text)
                return self._json({"ok": True, "overview": self.overview()})
            if path == "/api/pool/from_provider":
                b = self._body()
                prov = pi_providers().get(str(b.get("name") or ""), {})
                if not prov:
                    return self._json({"error": "档位不存在"}, 404)
                ms = [m.get("id") for m in prov.get("models", []) if isinstance(m, dict)]
                model = str(b.get("model") or "") or (ms[0] if ms else "")
                pool = pool_parse(env_read())
                pool = [e for e in pool if not (e.get("name") == str(b.get("name")) and e.get("model") == model)]
                pool.insert(0, {"name": str(b.get("name")), "base_url": prov.get("baseUrl", ""),
                                "model": model, "token": prov.get("apiKey", "")})
                text = env_set(env_read(), "SAPPER_KEY_POOL",
                               "'" + json.dumps(pool, ensure_ascii=False) + "'", multiline=True)
                env_write(text)
                return self._json({"ok": True, "overview": self.overview()})
            if path == "/api/budget/save":
                b = self._body()
                text = env_read()
                for k in BUDGET_KEYS:
                    v = str(b.get(k) or "").strip()
                    if v.isdigit():
                        text = env_set(text, k, v)
                env_write(text)
                return self._json({"ok": True, "overview": self.overview()})
            if path == "/api/test":
                b = self._body()
                base_url = str(b.get("base_url") or "").strip()
                key = str(b.get("key") or "").strip()
                model = str(b.get("model") or "").strip()
                if not (base_url and key and model):
                    return self._json({"error": "base_url/key/model 必填"}, 400)
                res = smoke(base_url, key, model)
                res["balance"] = balance_probe(base_url, key) if res["ok"] else None
                return self._json(res)
            if path == "/api/test_provider":
                b = self._body()
                prov = pi_providers().get(str(b.get("name") or ""))
                if not prov:
                    return self._json({"error": "档位不存在"}, 404)
                ms = [m.get("id") for m in prov.get("models", []) if isinstance(m, dict)]
                model = str(b.get("model") or "") or (ms[0] if ms else "")
                res = smoke(prov.get("baseUrl", ""), prov.get("apiKey", ""), model)
                res["provider"], res["model"] = str(b.get("name")), model
                return self._json(res)
            if path == "/api/test_pool":
                pool = pool_parse(env_read())
                out = []
                for e in pool:
                    res = smoke(e.get("base_url", ""), e.get("token", ""), e.get("model", ""))
                    res["name"] = e.get("name", "")
                    out.append(res)
                return self._json({"results": out})
            return self._json({"error": "not found"}, 404)
        except Exception as exc:
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    # ---------------------------------------------------------------- 概览
    def overview(self) -> dict:
        text = env_read()
        cur = env_get(text, "SAPPER_MODEL")
        pname, _, pmodel = cur.partition("/")
        provs = {}
        for k, v in pi_providers().items():
            provs[k] = {"base_url": v.get("baseUrl", ""), "api": v.get("api", ""),
                        "key_masked": mask(v.get("apiKey", "")), "key": v.get("apiKey", ""),
                        "models": [m.get("id") for m in v.get("models", []) if isinstance(m, dict)]}
        budget = {k: env_get(text, k) for k in BUDGET_KEYS}
        return {"env_path": str(ENV_PATH), "pi_path": str(PI_PATH),
                "sapper_model": cur, "cur_provider": pname, "cur_model": pmodel,
                "providers": provs,
                "pool": pool_parse(text),
                "budget": budget,
                "anthropic_fallback": {"base_url": env_get(text, "ANTHROPIC_BASE_URL"),
                                       "token_masked": mask(env_get(text, "ANTHROPIC_AUTH_TOKEN"))}}


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[token-console] http://{HOST}:{PORT}   env={ENV_PATH}  pi={PI_PATH}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
