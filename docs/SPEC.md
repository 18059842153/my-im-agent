# my-im-agent 技术规格（SPEC）

版本：v0.3.0 · 最后更新：2026-10-27 18:15 (UTC+8)
对应提交：`f4ec8dd`（GitHub Actions CI 于 Python 3.11 / 3.12 / 3.13 均 success）

---

## 0. 变更记录

| 时间 | 版本 | 变更内容 | 状态 |
| --- | --- | --- | --- |
| 2026-10-23 17:30 | v0.2.0 | 企业微信回调适配器 + 主动发消息 + 闹钟调度；模型层改用 Agently ModelRequest | 已实现并验证 |
| 2026-10-27 18:15 | v0.3.0 | 每日新闻推送：RSS → 去重选条 → AI 摘要 → 09:30 定时推送（科技/综合各 15 条） | 已实现并验证 |
| 2026-10-27 18:15 | v0.3.0 | 每日新闻推送：RSS → 去重选条 → AI 摘要 → 09:30 定时推送（科技/综合各 15 条） | 已实现并验证 |
| 2026-10-23 16:53 | v0.1.0 | Python 骨架：配置 / Agent / Adapter 抽象 / 终端 REPL / CI | 已实现并验证 |

## 0.1 当前状态

| 项 | 状态 |
| --- | --- |
| 意图识别与聊天回复（真实模型） | ✅ 已用 `deepseek-flash` 验证 |
| 闹钟设置 / 列举 / 取消 | ✅ 主机侧逻辑 100% 测试覆盖 + 真实模型 E2E |
| 闹钟到点推送 | ✅ E2E 验证（设闹钟 → 到点 → 主动推送） |
| 企业微信回调加解密与签名 | ✅ 单元测试覆盖（往返 / corp_id 不符 / 篡改） |
| 企业微信真实联调 | ⏳ 待提供 CorpID / AgentId / Secret / Token / AESKey 与公网回调地址 |
| 闹钟推送失败重试 | ❌ 未实现（见 §11 限制 L3） |

---

## 1. 业务目标与完成不变量

**目标**：一个接入企业微信的 Python AI Agent，能回答成员提问，并支持用自然语言设置定时闹钟，到点主动推送提醒。

**完成不变量（Completion Invariants）**

| # | 不变量 |
| --- | --- |
| I1 | 回调必须在 5 秒内返回，企业微信不重试 |
| I2 | 每条消息最多被处理一次（MsgId 去重） |
| I3 | 模型产出的一律只是「意图 + 参数」，所有副作用由宿主代码校验后执行 |
| I4 | 闹钟时间必须落在 (当前时间 − 30s, 当前时间 + 366d] 区间 |
| I5 | 只能取消「本次请求提供给模型的 id 集合」内的闹钟，且只限本会话 |
| I6 | 闹钟跨进程重启后不丢失 |
| I7 | 处理失败时用户一定会收到一条明确反馈，不会无声失败 |

**非目标**：群聊 @ 机器人、多租户、分布式部署、流式输出、多模态输入、闹钟的自然语言二次改期。

---

## 2. 所有权账本（Owner / Invariant Ledger）

| 不变量 / 决策 | Owner | 机制 | 说明 |
| --- | --- | --- | --- |
| 用户这句话是聊天还是设闹钟 | **模型** | `AgentlyIntentEngine.route` 结构化输出 | 语义判定，禁止用关键词/正则 |
| 从「明天早上9点」推出绝对时间 | **模型** | 输出 `alarm_at`（ISO8601） | 自然语言时间解析 |
| `alarm_at` 是否合法 / 是否已过期 | **宿主** | `core._set_alarm` | 确定性校验 |
| 闹钟 id 是否属于本会话 | **宿主** | `AlarmStore.get(conv, id)` | offered-key 成员校验 |
| 重复规则推进（每天/每周/工作日） | **宿主** | `next_run()` | 日期算术 |
| 存储、调度、到点推送 | **宿主** | `AlarmStore` / `AlarmScheduler` | 副作用 |
| 回调解密与签名 | **宿主** | `WeComCrypto` | 密码学 |
| access_token 获取与失效重试 | **宿主** | `WeComAPI` | 协议状态机 |
| 聊天回复文本 | **模型** | 同一 `ModelRequest` 的 `answer` 字段 | 与意图共享同一证据快照，合并为一次请求 |
| 会话历史裁剪 | **宿主** | `Brain._histories`（deque, max_turns） | 存储策略 |

---

## 3. 节点账本（Planned Node Ledger）

### 3.1 拓扑

```
企业微信服务器
   │  POST /wecom/callback (加密 XML)
   ▼
[Host] 签名校验 + AES 解密 ──失败──> 403 / 400
   │
   ├── MsgId 去重 ──重复──> 丢弃
   ▼
[Host] 内存队列 + 立即返回 success        ← 不变量 I1
   │
   ▼
[Host] worker: Brain.handle()
   │
   ├──> [Model] ModelRequest（意图 + 抽取 + 回复）  ← 唯一模型节点
   │         输入：now / user_text / history / alarms(投影)
   │         输出：IntentResult（Pydantic 校验）
   ▼
[Host] 分派（宿主校验后才产生副作用）
   ├── chat        → answer 文本
   ├── set_alarm   → 校验时间 → 入库 → 生成确认文本
   ├── list_alarms → 宿主格式化列表
   └── cancel_alarm→ id 成员校验 → 删除 → 生成确认文本
   │
   ▼
[Host] 主动发消息 API ──────────────────────────┐
                                                │
[Host] AlarmScheduler（1s tick）                │
   └── 到点 → notify(user, conv, text) ─────────┘
```

### 3.2 节点

| 节点 | 类型 | 职责 | 边界理由 |
| --- | --- | --- | --- |
| 回调验签解密 | Host | 解密 + sha1 校验 + corp_id 校验 | 密钥与协议归宿主 |
| 入队 + 立即 ack | Host | 5 秒内返回 | 平台时限，与业务解耦 |
| MsgId 去重 | Host | 500 条滑窗 | 平台重试语义 |
| **ModelRequest** | **Model** | 意图判定、时间/内容抽取、聊天回复 | 唯一需要语义判断的地方 |
| 时间合法性校验 | Host | 区间判断 | 确定性规则优先给模型后再由宿主兜底 |
| 闹钟入库/取消/列举 | Host | 持久化 + 文本生成 | 副作用与确定性格式化 |
| 调度器 | Host | tick + 推进 + 补偿 | 时间驱动，非模型职责 |
| 主动发消息 | Host | token 缓存 + 发送 | 外部 API 副作用 |

> 为什么只有**一个**模型节点：意图、抽取、回复共享同一份请求时证据快照，且后续字段不依赖任何「派发后才产生的观察结果」，合并成一次请求即可（Agently 输出契约有序：字段先声明，结论后出）。

---

## 4. 值 / 事件边账本（Edge Ledger）

| 边 | 生产者 | 校验 | 消费者 |
| --- | --- | --- | --- |
| 加密 XML → 明文 XML | WeComCrypto | 签名 + corp_id + PKCS7 | 适配器 |
| 明文 → `InboundMessage` | 适配器 | MsgType=text、FromUserName 非空 | 队列 |
| `InboundMessage` → 模型输入 | Brain | 去空白、非空 | ModelRequest |
| 模型 → `IntentResult` | ModelRequest | Pydantic + Literal 枚举 + Agently 重试 | Brain |
| `alarm_at` → `datetime` | `parse_local_time` | ISO8601；带时区转本地 naive；区间校验 | `_set_alarm` |
| `cancel_id` → 闹钟 | `AlarmStore.get` | 会话隔离 + id 存在性 | `_cancel_alarm` |
| 闹钟 → 持久化 | `AlarmStore._save` | 原子写（tmp + replace） | 下次启动 |
| 闹钟到点 → 推送 | `AlarmScheduler._fire` | 无（失败仅记录日志） | `notify` → 主动发消息 API |
| 回复文本 → 用户 | `WeComAPI.send_text` | errcode==0；40014/42001 刷新 token 重试一次 | 企业微信 |

---

## 5. 数据契约

### 5.1 `IntentResult`（模型输出，Pydantic + `format="json"`）

| 字段 | 类型 | 必填 | 约束 | 消费者 | 失败行为 |
| --- | --- | --- | --- | --- | --- |
| `intent` | `Literal[chat, set_alarm, list_alarms, cancel_alarm]` | 是 | 四选一 | `Brain._apply` 路由 | Agently 校验重试；终态失败则该消息报错 |
| `answer` | `str` | 否（默认 `""`） | 仅 chat 时有效，其他意图必须为空 | 直接回复用户 | 空且 chat → 兜底提示 |
| `alarm_at` | `str \| None` | set_alarm 时是 | ISO8601 本地时间 | `parse_local_time` → 校验 | 空 → 引导重说；格式错 → 提示；过期 → 拒绝入库 |
| `alarm_message` | `str \| None` | set_alarm 时是 | 非空白 | 闹钟内容 | 缺失 → 默认「提醒」 |
| `repeat` | `Literal[none, daily, weekly, workdays]` | 否（默认 `none`） | 四选一 | `next_run` | 非法值 → 降级为 `none` |
| `cancel_id` | `int \| None` | cancel_alarm 时是 | 必须来自本次 `input.alarms` | `AlarmStore.get` | 越权/不存在 → 「没有找到 #N」 |

### 5.2 `Alarm`（持久化，`data/alarms.json`）

```json
{
  "id": 1,
  "conversation_id": "ZhangSan",
  "user_id": "ZhangSan",
  "run_at": "2026-10-24T09:00:00",
  "message": "开会",
  "repeat": "none"
}
```

- `conversation_id` = `user_id` = 企业微信成员 UserID（单聊）
- `run_at` 为**本地时间 naive datetime**，秒精度
- 写入为整体原子替换（tmp + `os.replace`）

### 5.3 消息模型

`InboundMessage(content, user_id, conversation_id, message_type, message_id, timestamp, raw)`
`OutboundMessage(content, conversation_id, reply_to, message_type, raw)`

> `OutboundMessage.raw["user_id"]` 是适配器发送所需的收件人标识——由 `Brain` 从入站消息带入，避免适配器反查。

---

## 6. 闹钟规则

| 规则 | 取值 | Owner |
| --- | --- | --- |
| 时间解析 | `datetime.fromisoformat`；带 tz 转本地 naive | Host |
| 过去容忍 | < `now − 30s` 视为过期，拒绝 | Host |
| 最远未来 | > `now + 366d` 拒绝 | Host |
| 精度 | 秒（`microsecond=0`） | Host |
| 重复规则 | `daily` +1d；`weekly` +7d；`workdays` 跳过周六日 | Host |
| 调度周期 | 每秒 tick | Host |
| 重启补偿 | 错过 ≤ 10min 补发一次；> 10min：repeat 推进到未来、一次性丢弃 | Host |
| 触发文本 | `⏰ 闹钟提醒：{message}` | Host |

**状态机**

```
none:      待触发 ──到点──> 推送 ──> 删除
daily/weekly/workdays:
           待触发 ──到点──> 推送 ──> run_at = next_run() ──> 待触发
           取消   ──> 删除
```

---

## 7. 企业微信集成契约

| 环节 | 契约 |
| --- | --- |
| URL 验证（GET） | `sha1(sort(token, timestamp, nonce, echostr))` 校验 → 解密 `echostr` → 返回明文 |
| 收消息（POST） | 外层 XML 取 `Encrypt` → 验签 → 解密 → 内层 XML 取 `MsgType/Content/FromUserName/MsgId` |
| 加密 | AES-256-CBC，`key = b64decode(EncodingAESKey + "=")`，`iv = key[:16]`，PKCS7(32)，明文 = `random16 + len(4B大端) + msg + corp_id` |
| 响应 | 一律 `success`（纯文本），**不做任何耗时操作** |
| 去重 | MsgId 滑窗 500 条 |
| 主动发消息 | `POST /cgi-bin/message/send`，`{touser, msgtype:"text", agentid, text:{content}}` |
| token | `GET /cgi-bin/gettoken`；缓存至 `expires_in − 60s`；`40014 / 42001` 强制刷新重试一次 |
| 常见错误 | `60020` = 出口 IP 不在「企业可信 IP」白名单（**配置项，非代码问题**） |

**必需配置**：`WECOM_CORP_ID` / `WECOM_AGENT_ID` / `WECOM_SECRET` / `WECOM_TOKEN` / `WECOM_ENCODING_AES_KEY`，缺失时 `require_wecom()` 快速失败。

---

## 8. 外部依赖

| 依赖 | 用途 | 版本约束 |
| --- | --- | --- |
| `agently` | ModelRequest / 结构化输出 | `==4.1.4.10`（与 skill catalog v3 对齐） |
| `fastapi` + `uvicorn` | 回调服务 | `>=0.115` / `>=0.30` |
| `pycryptodome` | AES-CBC 加解密 | `>=3.21` |
| `httpx` | 企业微信 API | `>=0.27` |
| `pydantic` | 输出契约 | `>=2.0` |
| `python-dotenv` | `.env` 加载 | `>=1.0` |

**框架使用约束（踩过的坑）**：插件 settings 必须**按键逐个**设置
（`Agently.set_settings("plugins.ModelRequester.OpenAICompatible.base_url", ...)`）；
对整个 namespace 赋 dict 会覆盖插件默认的 `path_mapping` / `content_mapping`，导致请求直接崩。

---

## 9. 生产必要性账本（Production-Necessity Ledger）

| 请求字段 / 产物 | 授权消费者 | 必要性 |
| --- | --- | --- |
| `intent` | `Brain._apply` 路由 | 必需，决定副作用分支 |
| `answer` | 直接回用户 | chat 分支唯一产物 |
| `alarm_at` | `_set_alarm` 校验后入库 | set_alarm 必需 |
| `alarm_message` | 入库 + 推送文本 | set_alarm 必需 |
| `repeat` | `next_run` | 支持重复闹钟 |
| `cancel_id` | `AlarmStore.get` | 取消分支必需 |
| 会话 `history` | 同一次请求的 `input` | 多轮问答连续性 |
| `alarms` 投影（id/run_at/message/repeat） | 模型做取消决策 | offered-key 集合，宿主校验成员 |
| `⏰ 闹钟提醒：` 前缀 | 用户 | 区分主动推送与被动回复 |

无未消费字段；`alarm_at` 在 `list/cancel/chat` 分支不被消费（模型需置空）。

---

## 10. 测试与验收

### 10.1 确定性测试（hard gate，CI 必过）

| 范围 | 覆盖点 | 数量 |
| --- | --- | --- |
| `test_crypto.py` | 加解密往返、corp_id 不符、垃圾密文、签名正反例、非法 AESKey | 5 |
| `test_alarms.py` | 递增 id、过滤排序、会话隔离删除、持久化往返、损坏文件降级、due 过滤、非法 repeat、`next_run` 各规则与过期推进、到点触发、repeat 推进、通知失败仍推进、重启补偿三种情形 | 19 |
| `test_core.py` | 时间解析、**过期拒绝且无副作用**、缺时间引导、格式错误、超远拒绝、列举、取消（有效/越权/跨会话/无 id）、历史累积、空白消息忽略 | 18 |
| `test_wecom_adapter.py` | URL 验证（成功/签名错）、收消息入队、签名错 403、MsgId 去重、篡改拒绝、配置缺失快速失败、发送取 `raw.user_id` | 8 |

合计 **50 项**，全部通过；`ruff check .` 无告警（行宽 110）。

### 10.2 语义质量（soft target，真实模型）

已用 `deepseek-flash` 跑通的代表性用例（声明：为实测记录，非模拟）：

| 输入 | 输出 | 判定 |
| --- | --- | --- |
| 明天早上9点提醒我开会 | `set_alarm / 2026-10-24T09:00:00 / 开会 / none` | ✅ |
| 10分钟后提醒我喝水 | `set_alarm / +10min / 喝水 / none` | ✅ |
| 每个工作日下午6点提醒我填日报 | `set_alarm / 18:00 / 填日报 / workdays` | ✅ |
| 帮我查一下闹钟 | `list_alarms` | ✅ |
| 今天天气怎么样 | `chat` + 回复文本 | ✅ |

**E2E**：设 `17:26` 闹钟 → 确认文本 → `查看闹钟` 列出 → 17:26 准点收到 `⏰ 闹钟提醒：端到端测试`。

### 10.3 尚未验证

- 企业微信真实收发（缺企业凭据与公网地址）
- 多用户并发下的调度精度
- `deepseek-flash` 在长对话下的意图稳定性

---

## 11. 已知限制

| # | 限制 | 影响 | 缓解 |
| --- | --- | --- | --- |
| L1 | 队列为进程内存级 | 进程崩溃时**已 ack 但没处理完**的消息丢失 | 后续可换 Redis/RQ；或回调先落盘 |
| L2 | 单进程单实例 | 多实例会重复处理；闹钟重复触发 | 部署只跑一个实例 |
| L3 | 闹钟推送失败不重试 | 网络抖动时该次提醒丢失（一次性闹钟仍会被移除） | 后续加失败队列与退避重试 |
| L4 | 无用户鉴权/配额 | 任何能通过验签的请求都被处理 | 依赖企业微信验签（需 Token+AESKey） |
| L5 | 时间由模型解析 | 分钟级表述在边界时刻可能被顺延到次日 | 已在 `_set_alarm` 明确拒绝过期并提示 |
| L6 | 仅单聊、仅文本 | 群聊、图片、文件不处理 | 非目标 |
| L7 | 本地时间 | 服务器时区变更会影响已存闹钟 | 部署时区固定 |
| L8 | 闹钟列表常驻进程内存 | 手工改 `alarms.json` 后会被进程内副本覆盖 | 改文件前先停服务 |
| L9 | 新闻摘要可能失败 | 模型 JSON 校验失败时该块退化为「仅标题」 | 三层降级 + 页脚提示，不影响推送 |
| L10 | 新闻 at-most-once | 崩在发送中途当天不再重试（宁可漏发不重复） | `--check` 显示 `sending`，需要时 `--news-now` |
| L9 | 新闻摘要可能失败 | 模型 JSON 校验失败时该块退化为「仅标题」 | 三层降级 + 页脚提示，不影响推送 |
| L10 | 新闻 at-most-once | 崩在发送中途当天不再重试（宁可漏发不重复） | `--check` 显示 `sending`，需要时 `--news-now` |

---

## 12. 演进方向（未承诺）

1. 闹钟持久化从 JSON 换 SQLite（并发与查询更好）
2. 群聊 @ 机器人 + 会话维度改为 `corp + chat_id`
3. 工具调用（ActionRuntime）：查天气、查日程等真实能力
4. 闹钟推送失败重试 + 死信队列
5. 多 IM 平台（飞书 / 钉钉）复用 `IMAdapter` 抽象

---

## 13. 每日新闻推送（v0.3.0）

**目标**：每天 09:30 主动推送一份早报，科技 15 条 + 综合 15 条，每条一句 AI 摘要。

### 13.1 所有权

| 决策 / 动作 | Owner | 位置 |
| --- | --- | --- |
| 抓取与解析 RSS（含 gb2312 / CST 时区纠正） | Host | `news.decode_xml` / `parse_feed` |
| 跨源去重、按时间选前 15 条、跨天 `seen` 去重 | Host | `news.select_items` |
| 压成一句话摘要 | **Model** | `news.AgentlyNewsSummarizer` |
| 摘要 id 回显校验、缺失补位、去重排序 | Host | `news.reconcile` |
| 渲染与按字节切分（3800B） | Host | `news.render_section` / `split_markdown` |
| 何时推、是否已推、失败重试上限 | Host | `news.should_fire` / `NewsStateStore` |

### 13.2 数据契约

`NewsItem(id, title, link, source, published)`；AI 输出 `NewsDigest{items: NewsSummary{id, title, summary}}`
（`summary` 对模型必填，`min_length=1` —— 给默认值模型会直接留空）。
状态文件 `data/news_state.json`：`last_date/last_status/attempts/seen`。

### 13.3 时序

```
09:30 tick → should_fire? → 落盘 sending → 并发抓 3 个源 → 各区选 15 条
         → 每 5 条一 chunk 调模型（2~6 次）→ reconcile → 渲染
         → 依次发送（科技 1 条、综合 1 条）→ 落盘 ok/partial/failed
```

补发窗口 90 分钟（09:30–11:00 内启动会补发并标注）；超出窗口记 `skipped`，
需要时 `--news-now` 人工触发。

### 13.4 已知限制

见 §11 L9（摘要失败降级为标题版）、L10（at-most-once 不重复推送）。
