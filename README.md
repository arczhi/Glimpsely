# Glimpsely

一瞥即懂。用户在微信里照常截图/转发，Glimpsely（本地多模态 LLM）静默看懂、
记入私人记忆库，在恰当时刻主动推送贴心提醒与日报。

架构与设计详见 [DESIGN.md](DESIGN.md)。

## 快速开始

```bash
# 0) 依赖环境（一次性）
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install wechat-ilink-bot apscheduler pytest pytest-asyncio pillow

# 1) 启动本地模型服务（一次性，默认端口 8000）
omlx start

# 2) 验证 LLM 视觉理解链路
.venv/bin/python -m glimpsely.main test-llm

# 3) 无微信端到端演示（理解→入库→触发→推送打印）
.venv/bin/python -m glimpsely.main demo

# 4) 真机运行（见下方 runbook）
.venv/bin/python -m glimpsely.main run
```

## 真机 Runbook（M4 里程碑）

1. `omlx start` 确认 `curl -H "Authorization: Bearer 1234" http://127.0.0.1:8000/v1/models` 正常
2. `.venv/bin/python -m glimpsely.main run` → 终端打印登录二维码
3. 用微信扫码 → 手机上点「确认登录」
4. 在微信里给该 bot 发任意消息（bot 是你的另一台设备账号；建议用小号或家人号）
5. 验证闭环（**全部语义触发，无需记命令**）：
   - 转发一张截图 → 收到「已记下 ✓ + 摘要」
   - 说「看看最近记了什么」→ 模型自动路由到 query，基于记忆库回答
   - 说「给我发份日报」→ 模型自动路由到 digest 并推送
   - 直接聊天（问任何问题）→ 正常对话回复
   - 快捷方式仍保留：`/digest` 直接触发日报（跳过路由，省一次 LLM 调用）
6. 注意：终端进程需保持运行（长轮询 + 调度都依赖它）；建议 `nohup` 或 tmux

## Token 稳定性长测（M5 里程碑）

`scripts/token_watch.py` 每 30 分钟主动推送一条心跳，记录 session 失效
（`-14`）首次出现时间与恢复路径，产出 `data/token_watch.jsonl`：

```bash
.venv/bin/python scripts/token_watch.py --hours 48
```

- 观察点：`-14` 出现时间 → 决定每日日报可靠性
- 恢复：用户在微信里给 bot 发任意一条消息即自动恢复（alive 置回 1）

## 项目命令门禁

```bash
make ci    # ruff lint + 全量 pytest（单元 + LLM 集成）
```

## 已知约束

- 仅 1v1（iLink 协议无群聊）；无撤回/引用回复
- bot_token/context_token 生命周期未实测 → runbook 首日挂长测
- 主动推送保守限速 3 条/小时、15 条/天
- 全部数据（事件/画像/原图）仅存本机 `data/`
