#!/usr/bin/env bash
# ============================================================================
# venue-setup.sh — 赛场 one-api token 平台一键接入（sapper + pi 链路）
# 用法:
#   venue-setup.sh <BASE_URL> <API_KEY> <MODEL_NAME>    接入新平台
#   venue-setup.sh --restore                            恢复默认档 bjp-uzip
#   例: venue-setup.sh http://10.1.2.3:3000/v1 sk-xxxx deepseek-flash
# 说明:
#   - BASE_URL 建议带 /v1（one-api 标准 openai 兼容端点）；漏了会自动补试，
#     并把补全后的地址写入配置
#   - MODEL_NAME 必须与 one-api 渠道里的模型名逐字一致（区分大小写）
#   - 幂等：重复执行只覆盖 provider "venue" 与 sapper.env 模型相关行
#     （预算/路径/GLM 兜底凭据原样保留，不再整文件重写）
#   - 先冒烟后写配置：网关不通/坏 key 时不动任何文件
#   - 配套管理台 http://127.0.0.1:8790（逐条测 key/看额度/改预算）
# ============================================================================
set -euo pipefail

MODELS_JSON="${VENUE_MODELS_JSON:-$HOME/.pi/agent/models.json}"
SAPPER_ENV="${VENUE_SAPPER_ENV:-$HOME/ctf/sapper.env}"
PROVIDER="venue"
DEFAULT_MODEL="bjp-uzip/deepseek-v4.1-flash"
TC_DIR="$HOME/ctf/token-console"

if [ "${1:-}" = "--restore" ]; then
  echo "恢复默认档 → $DEFAULT_MODEL"
  TOKEN_CONSOLE_ENV="$SAPPER_ENV" TOKEN_CONSOLE_PI="$MODELS_JSON" python3 - "$PROVIDER" "$DEFAULT_MODEL" "$TC_DIR" <<'EOF'
import json, pathlib, sys, time
prov, default_model, tc_dir = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, tc_dir)
import server as tc

mp = pathlib.Path(tc.PI_PATH)
if mp.is_file():
    bak = mp.with_suffix(".json.bak-" + time.strftime("%Y%m%d-%H%M%S"))
    bak.write_text(mp.read_text(encoding="utf-8"), encoding="utf-8")
    d = json.loads(mp.read_text(encoding="utf-8"))
    d.get("providers", {}).pop(prov, None)
    mp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  ✓ provider '{prov}' 已从 models.json 删除")

text = tc.env_read()
text = tc.env_set(text, "SAPPER_MODEL", default_model)
pool = [e for e in tc.pool_parse(text) if not str(e.get("name", "")).startswith(prov)]
text = tc.env_set(text, "SAPPER_KEY_POOL",
                  "'" + json.dumps(pool, ensure_ascii=False) + "'", multiline=True)
tc.env_write(text)
print(f"  ✓ SAPPER_MODEL={default_model}，池中 venue 条目已清（其余保留）")
EOF
  exit 0
fi

BASE_URL="${1:?用法: venue-setup.sh <BASE_URL(带/v1)> <API_KEY> <MODEL_NAME> | --restore}"
API_KEY="${2:?缺少 API_KEY}"
MODEL="${3:?缺少 MODEL_NAME}"

echo "[1/5] 网关快速冒烟（max_tokens=4，几乎不花 token；失败不写任何配置）"
SMOKE_JSON=$(TOKEN_CONSOLE_ENV="$SAPPER_ENV" TOKEN_CONSOLE_PI="$MODELS_JSON" \
  python3 - "$BASE_URL" "$API_KEY" "$MODEL" "$TC_DIR" <<'EOF'
import json, sys
base, key, model, tc_dir = sys.argv[1:5]
sys.path.insert(0, tc_dir)
import server as tc
print(json.dumps(tc.smoke(base, key, model), ensure_ascii=False))
EOF
)
echo "$SMOKE_JSON" | python3 -c '
import json, sys
r = json.load(sys.stdin)
if r.get("ok"):
    u = r.get("tokens") or {}
    extra = "  [" + r["note"] + "]" if r.get("note") else ""
    print("    ✓ 网关通 {}ms  tokens prompt={}/completion={}{}".format(
        r["latency_ms"], u.get("prompt_tokens", "?"), u.get("completion_tokens", "?"), extra))
else:
    print("    ✗ {}  {}".format(r.get("err_class", "失败"), (r.get("detail") or "")[:120]))
    print("    逐项排查: 地址带/v1? key 有效? 模型名与 one-api 渠道逐字一致?")
    sys.exit(1)
'
# 实际打通的 base（smoke 可能补了 /v1）——直接用 smoke 返回的 base_used
WORK_BASE=$(echo "$SMOKE_JSON" | python3 -c '
import json, sys
r = json.load(sys.stdin)
print(r.get("base_used") or sys.argv[1])
' "$BASE_URL")
if [ "$WORK_BASE" != "$BASE_URL" ]; then
  echo "    地址已规范化: $BASE_URL → $WORK_BASE"
fi

echo "[2/5] 写入 pi provider '$PROVIDER'（models.json 自动备份）"
TOKEN_CONSOLE_ENV="$SAPPER_ENV" TOKEN_CONSOLE_PI="$MODELS_JSON" python3 - "$PROVIDER" "$WORK_BASE" "$API_KEY" "$MODEL" "$TC_DIR" <<'EOF'
import sys
prov, base, key, model, tc_dir = sys.argv[1:6]
sys.path.insert(0, tc_dir)
import server as tc
tc.pi_upsert(prov, base, key, model)
print(f"    ✓ provider '{prov}' → {base} (model={model})")
EOF

echo "[3/5] 更新 sapper.env 模型相关行（预算/路径/GLM 兜底凭据原样保留，自动备份）"
TOKEN_CONSOLE_ENV="$SAPPER_ENV" TOKEN_CONSOLE_PI="$MODELS_JSON" python3 - "$PROVIDER" "$WORK_BASE" "$API_KEY" "$MODEL" "$TC_DIR" <<'EOF'
import json, pathlib, sys
prov, base, key, model, tc_dir = sys.argv[1:6]
sys.path.insert(0, tc_dir)
import server as tc

if not tc.env_read().strip():
    tc.env_write("# sapper-v2 赛场配置（venue-setup 生成）\n")
    print("    ⚠ sapper.env 原不存在，已生成最小模板；GLM 兜底凭据需手工补 ANTHROPIC_BASE_URL/AUTH_TOKEN")

text = tc.env_read()
binnow = tc.env_get(text, "SAPPER_BIN")
absbin = str(pathlib.Path.home() / ".local" / "bin" / "sapper-pi-engine")
if not binnow or not binnow.startswith("/"):
    text = tc.env_set(text, "SAPPER_BIN", absbin)
    print(f"    ✓ SAPPER_BIN → {absbin}")
qualified = f"{prov}/{model}"
text = tc.env_set(text, "SAPPER_MODEL", qualified)
print(f"    ✓ SAPPER_MODEL={qualified}")
# key 池：venue 条目置顶（model 用线上裸名，管理台可直接测），其余平台条目保留
pool = [e for e in tc.pool_parse(text) if e.get("base_url") != base]
pool.insert(0, {"name": f"{prov}-{model}", "base_url": base, "model": model, "token": key})
text = tc.env_set(text, "SAPPER_KEY_POOL",
                  "'" + json.dumps(pool, ensure_ascii=False) + "'", multiline=True)
print(f"    ✓ key 池 {len(pool)} 条（venue 置顶，其余平台条目保留）")
tc.env_write(text)
EOF

echo "[4/5] 冒烟② pi 直连（真模型调用，≤90s）"
# shellcheck disable=SC1090
source "$SAPPER_ENV"
if OUT=$(echo '只输出两个字:直连' | timeout 90 pi --model "$PROVIDER/$MODEL" -p --no-session 2>&1); then
    echo "    ✓ pi 回复: $(echo "$OUT" | tail -1 | cut -c1-40)"
else
    echo "    ✗ pi 直连失败（看上方网关冒烟是否通过；pi 是否在 PATH/nvm）"; exit 1
fi

echo "[5/5] 冒烟③ sapper 引擎包装器（含工具执行，≤120s）"
if OUT=$(echo '{"smoke":true}' | timeout 120 sapper-pi-engine -p --output-format json --model "$SAPPER_MODEL" <<< '执行 echo venue-ok 并只告诉我输出' 2>&1); then
    echo "$OUT" | grep -q '"is_error": false' && echo "    ✓ 引擎信封 OK: $(echo "$OUT" | tail -1 | cut -c1-60)" || { echo "    ✗ 引擎信封异常: $OUT"; exit 1; }
else
    echo "    ✗ 引擎调用失败"; exit 1
fi

# Token 管理台保活（已跑则跳过；不弹浏览器）
if ! pgrep -f "token-console/[s]erver.py" >/dev/null 2>&1; then
  setsid nohup python3 "$TC_DIR/server.py" >> /tmp/token-console.log 2>&1 &
fi

# ---- 可选阶段：CTF-BTFly 备份链路（VENUE_BTFLY=1 时执行；管理台勾选触发）----
if [ "${VENUE_BTFLY:-0}" = "1" ]; then
  echo "[+] CTF-BTFly 备份链路：写 bin/.env venue 档 + 重启 daemon + 健康检查"
  REPO="/home/hgg/data/03tools/CTF-BTFly-1.3.1"
  ENVF="$REPO/bin/.env"
  if [ ! -f "$ENVF" ]; then
    echo "    ✗ 找不到 $ENVF，跳过 CTF-BTFly 链路"
    exit 0
  fi
  cp "$ENVF" "$ENVF.bak-$(date +%Y%m%d-%H%M%S)"
  python3 - "$ENVF" "$WORK_BASE" "$API_KEY" "$MODEL" <<'EOF'
import pathlib, re, sys
envf, base, key, model = sys.argv[1:5]
text = pathlib.Path(envf).read_text(encoding="utf-8")
# 清掉旧 venue 行（幂等），追加新档
text = re.sub(r"^CTF_DEFAULT_MODEL=venue-.*$\n?", "", text, flags=re.M)
text = re.sub(r"^CTF_MODELS=venue-.*$\n?", "", text, flags=re.M)
text = re.sub(r"^CTF_MODEL_VENUE_.*$\n?", "", text, flags=re.M)
block = (f"CTF_DEFAULT_MODEL=venue-flash\n"
         f"CTF_MODELS=venue-flash\n"
         f"CTF_MODEL_VENUE_FLASH_API_KEY={key}\n"
         f"CTF_MODEL_VENUE_FLASH_BASE_URL={base}\n"
         f"CTF_MODEL_VENUE_FLASH_ID={model}\n"
         f"CTF_MODEL_VENUE_FLASH_INCLUDE_STREAM_USAGE=true\n")
text = text.rstrip("\n") + "\n" + block
pathlib.Path(envf).write_text(text, encoding="utf-8")
print("    ✓ bin/.env 已写入 venue 档（原文件已备份）")
EOF
  for p in $(pgrep -x ctfagent-daemon); do kill "$p" 2>/dev/null; done
  sleep 3
  ( cd "$REPO" && CTF_DAEMON_ADDRESS=0.0.0.0:18731 setsid nohup ./bin/ctfagent-daemon >> bin/data/daemon.log 2>&1 </dev/null & )
  ok=0
  for i in 1 2 3 4 5; do
    sleep 3
    curl -sf -m 3 http://127.0.0.1:18731/health >/dev/null 2>&1 && { ok=1; break; }
  done
  [ "$ok" = "1" ] && echo "    ✓ daemon 已重启并健康（模型选 venue-flash）" || { echo "    ✗ daemon 健康检查失败（看 bin/data/daemon.log）"; exit 1; }
fi

cat <<'EOF'

=== 接入完成 ===
  Token 管理台: http://127.0.0.1:8790 （逐条测 key / 看额度 / 改预算）
  source ~/ctf/sapper.env
  sapper-ctf-new <题名> "<目标描述>" [靶标URL]
  cd ~/data/01code/16CTF/sapper-runs/<题名>
  sapper auto --model "$SAPPER_MODEL" --minutes 30 --skip-permissions --verify
桌面: 「sapper-ui 控制台」图标（读同一份 sapper.env；改过配置需重开 UI 窗口）。
恢复默认档: venue-setup.sh --restore
EOF
