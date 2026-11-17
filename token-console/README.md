# Token 管理台

比赛单模型场景的 key/档位管理界面。`http://127.0.0.1:8790`，纯 Python 标准库，无依赖。

启动: 双击桌面「Token 管理台」图标，或 `~/ctf/token-console-start.sh`。
重启(改了代码后): `pkill -f "token-console/[s]erver.py"` 再启动。

管理的三个落点:
- 档位(pi 引擎 provider): `~/.pi/agent/models.json` — key 实际存放处，pi 每次任务即时读，改完即生效
- 当前档: `~/ctf/sapper.env` 的 `SAPPER_MODEL=档位名/模型名` — sapper-ui 重启后对新任务生效
- key 池: `~/ctf/sapper.env` 的 `SAPPER_KEY_POOL` — sapper 侧 failover，额度/欠费错误自动轮下一条

测试: 每条 key 发一条 max_tokens=4 的最小请求(几乎不花 token)。
错误自动分类: key 无效/额度不足/限流/路径或模型不存在(自动补 /v1 重试)/超时/连接拒绝。
one-api 平台若暴露计费端点会顺带显示额度/已用。

所有写操作自动备份到 `sapper.env.bak-时间戳` / `models.json.bak-时间戳`(保留最近5份)。
环境变量覆盖: TOKEN_CONSOLE_ENV / TOKEN_CONSOLE_PI / TOKEN_CONSOLE_PORT。
