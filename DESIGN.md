# Glimpsely — 可执行设计文档

> 一瞥即懂。用户在微信里照常截图/转发，Glimpsely 静默看懂、记入私人记忆库，
> 在恰当时刻主动给出贴心提醒与推荐。用户唯一动作 = 转发。

## 1. 产品定义

| 维度 | 决定 |
|---|---|
| 形态 | 微信 1v1 好友 bot（iLink 官方协议），无独立 App |
| 核心动作 | 用户转发截图/文字 → bot 回「已记下 ✓」→ 恰当时刻主动推送 |
| 记忆 | 本地 SQLite，事件 + 画像 + 去重，全部数据不出本机（LLM 推理本地 omlx） |
| 推荐 | 规则引擎决定「何时推/推什么」，LLM 只负责措辞（两段式） |
| MVP 边界 | 仅文字+图片转发；仅时效提醒+每日日报；单用户；本机运行 |

## 2. 系统架构

```
微信用户 --转发/截图--> [1] Bot 入口 (iLink 长轮询)
                           │ wechat-ilink-bot SDK
                           ▼
                      [2] 理解管道 (omlx :8000, Qwen3.5-9B-4bit)
                           │ 单次调用：图/文 → 结构化 JSON
                           ▼
                      [3] 记忆库 (SQLite: events / profile / dedupe)
                           ▼
                      [4] 触发器引擎 (APScheduler, 60s tick)
                           │ 时效触发 / 上下文触发 / 每日日报
                           ▼
                      [5] 主动推送 (缓存 context_token, -14 降级)
```

## 3. 目录结构

```
Glimpsely/
├── DESIGN.md               # 本文档
├── README.md               # 快速开始
├── pyproject.toml          # 项目元数据
├── .env.example            # 配置样例
├── glimpsely/
│   ├── __init__.py
│   ├── config.py           # 配置加载（.env + 默认值）
│   ├── llm.py              # omlx OpenAI 兼容客户端（视觉）
│   ├── understand.py       # 理解管道：消息 → Record(JSON)
│   ├── memory.py           # SQLite：events / profile / dedupe / push_state
│   ├── push.py             # 推送层：context_token 管理 + 发送
│   ├── triggers.py         # 触发器引擎（时效/上下文/日报）
│   ├── bot.py              # iLink Bot 入口（长轮询 + handler）
│   ├── demo.py             # 无微信端到端演示（喂样例消息）
│   └── main.py             # CLI：run / demo / test-llm / scan
├── assets/                 # 样例测试图
├── scripts/
│   └── token_watch.py      # token 48h 长测
└── tests/
    ├── test_understand.py
    └── test_memory.py
```

## 4. 数据模型（SQLite DDL）

```sql
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,                  -- 记录时间 ISO8601
  kind TEXT NOT NULL,                -- courier|bill|coupon|event|address|person|chat_digest|note|other
  title TEXT,                        -- 一句话摘要
  entities_json TEXT,                -- {"单号": "...", ...}
  deadline TEXT,                     -- ISO8601，无则 NULL
  importance INTEGER DEFAULT 3,      -- 1-5
  user_intent TEXT,                  -- 用户为什么转这条
  raw_text TEXT,                     -- 原始文本（文本消息）
  media_path TEXT,                   -- 原图存档路径（图片消息）
  source TEXT DEFAULT 'wechat'       -- 来源
);

CREATE TABLE IF NOT EXISTS profile (
  key TEXT PRIMARY KEY,
  value TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS dedupe (
  hash TEXT PRIMARY KEY,             -- 内容指纹（文本hash / 媒体md5+尺寸）
  event_id INTEGER,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS push_state (
  user_id TEXT PRIMARY KEY,          -- ...@im.wechat
  context_token TEXT,
  alive INTEGER DEFAULT 1,           -- 1=可推送 0=-14 降级
  last_push_at TEXT,
  last_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS trigger_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER,
  trigger_type TEXT,                 -- deadline|daily_digest|context
  fired_at TEXT
);
```

## 5. 理解管道（核心，单次 LLM 调用）

输入：文本 + 图片（base64，omlx 视觉）。输出固定 schema，解析失败降级为 kind=note。

```json
{
  "kind": "courier",
  "title": "顺丰快递，单号 SF13688889999",
  "entities": {"单号": "SF13688889999", "取件码": "8-2-3002"},
  "deadline": null,
  "importance": 3,
  "user_intent": "记下快递单号方便之后查",
  "memory_note": "一个顺丰快递，取件码 8-2-3002"
}
```

- kind 枚举：courier / bill / coupon / event / address / person / chat_digest / note / other
- deadline：有时效必填（优惠券到期、日程、账单还款日），否则 null
- 工程：temperature=0.1，max_tokens=500，`直接输出JSON不要解释`；重试 1 次；失败降级 note

## 6. 触发器引擎

| 类型 | 规则 | 默认时间 |
|---|---|---|
| deadline | 事件 deadline 前 1 天 & 前 1 小时各推一次 | - |
| courier_arrive | kind=courier 且含取件码：记录后 3h 提醒一次取件 | - |
| daily_digest | 每天 21:00：今日 events 汇总 + 明日 deadline + 一条基于画像的推荐 | 21:00 |
| context | MVP 只实现「连续 3 天有 coupon 类记录却无消费记录」提示 | - |

推送文案两段式：规则选出 event 列表 → LLM 将其写成一句 ≤60 字的贴心话（fallback：模板直出）。

## 7. 推送层（关键约束来自官方协议）

1. 收到用户消息时：`push_state` 写入 `(user_id, context_token, alive=1)`
2. 主动推送：读 push_state → sendmessage（context_token 原样回传）
3. `ret=-14`（session timeout）：alive=0，暂停推送；用户下次发消息自动恢复
4. bot_token 持久化（SDK 已做）；context_token 由本层持久化
5. 频率保护：同一 user_id 每小时最多 3 条主动推送

## 8. 配置（.env）

```
OMLX_BASE_URL=http://127.0.0.1:8000
OMLX_API_KEY=1234
OMLX_MODEL=Qwen3.5-9B-4bit
DB_PATH=data/glimpsely.db
MEDIA_DIR=data/media
DIGEST_HOUR=21
PUSH_LIMIT_PER_HOUR=3
```

## 9. 测试计划

1. **单元测试**（pytest）：理解管道 schema 校验/降级、记忆库 CRUD/去重、触发器规则
2. **LLM 实测**（`python -m glimpsely.main test-llm`）：真实调 omlx，验证视觉提取
3. **端到端 demo**（`python -m glimpsely.main demo`）：不依赖微信——喂样例消息（文本 2 条 + 图片 2 张），
   走完 理解→入库→触发→推送(打印到终端) 全链路
4. **真机 runbook**：扫码登录 → 自发消息 → 验证收/回/主动推送
5. **token 长测**（scripts/token_watch.py）：每 30min 主动推一条心跳，记录 -14 首次出现时间 → 决定日报可靠性

## 10. 里程碑与验收标准

| 里程碑 | 验收 |
|---|---|
| M1 理解管道 | test-llm 对 3 类样例截图输出合法 schema |
| M2 记忆库 | demo 模式入库+去重正确，重复转发不产生重复记录 |
| M3 触发器 | 人为构造 deadline 过期场景，触发并生成推送文案 |
| M4 真机闭环 | 真实微信：转发→收到「已记下✓」→21:00 收到日报 |
| M5 稳定性 | token 长测报告：-14 出现时间与恢复流程实测通过 |

## 11. 已知风险

- ⚠️ context_token 生命周期未知（M5 专门验证；兜底=用户下次互动恢复）
- ⚠️ 9B 模型 JSON 稳定性（temperature+重试+降级兜底；必要时换云 API 混合）
- ⚠️ iLink 频率配额未公开（推送限速保守 3/h）
- 仅 1v1、无群聊（产品上即定位个人助理，不是缺陷）

## 12. 追加设计（v0.2）

### 语义 Skill 路由（skills.py）
字面命令（/list 等）替换为 LLM 单次路由：record/query/digest/clear/chat 五技能，
输出 `{skill, record?, reply?}`；对话历史入 `chat_log`，多轮上下文进 prompt。
纯文本才走路由；图片消息始终逐张记录。

### OCR 全文层
每张图一次独立 OCR 调用（逐字转录，`enable_thinking` 关闭），存 `events.ocr_text`。
查询/日报 prompt 携带原文摘录（≤400字/条），对话可引用订单号、金额、菜名等细节。

### 多图与并发
一条消息多张图：逐张下载（绕过 SDK `download_media` 只取第一张的限制）→
逐张路由+OCR → 聚合回复「已记下 N 条 ✓」。连续多条消息：handler 立即返回、
后台任务并发（`asyncio.Semaphore(3)`），DB 操作原子。

### 自然遗忘机制
`events.forgotten/forgotten_at` 软标记列。调度器每小时扫描，超 `MEMORY_TTL_DAYS`（默认3天）
标记遗忘（不删除）。提醒/日报/查询默认只看 active。**唤醒**：向量检索命中的遗忘记录
自动重新激活，deadline 按「原始剩余时长」重新锚定到当前（`reactivate()`）。

### 向量检索（sqlite-vec）
`event_vecs`（vec0 虚表，512 维 float32），入库时自动索引，启动时回填存量。
嵌入源：omlx `/v1/embeddings` 若可用则用之；当前无 embedding 模型，兜底为
本地字符 bigram+词元哈希袋向量（确定性、中文可用，占位实现——将来向 omlx
装载 bge 系模型即可无缝切换）。查询路径：`answer_query` = 向量检索 top8
（含遗忘）→ 自动唤醒 → LLM 基于原文作答，回复尾注「已唤醒 N 条」。

### 日报 v2
`compose_daily_report`：输入=近 TTL 全部 events（含 OCR 原文摘录）+画像，
输出<=160字（第一句概括 + 2-3条引用具体细节的建议/启发，不流水账）；
LLM 失败降级为清单+通用建议。
