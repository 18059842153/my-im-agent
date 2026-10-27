# my-im-agent

接入**企业微信**的 Python IM AI Agent：能聊天，也有**定时闹钟**能力。

模型调用走 [Agently](https://github.com/AgentEra/Agently) 框架（ModelRequest 结构化输出），
企业微信自建应用负责收发消息，闹钟由宿主侧调度器到点主动推送。

```
企业微信 --(回调 POST, 加密)--> FastAPI 回调服务 --(立即返回 success)--> 内存队列
                                                          |
                          +-------------------------------+
                          v
                    worker: Brain.handle()
                    ├─ LLM 意图识别（chat / set_alarm / list_alarms / cancel_alarm）
                    ├─ chat       → 生成回复 ──────────────┐
                    └─ set_alarm  → 校验 + 入库             │
                                                          v
AlarmScheduler ──(到点)──> 主动发消息 API <─── 回复文本 ────┘
```

回复一律走**主动发消息 API**，回调立即返回 `success`，彻底绕开企业微信被动回复 5 秒时限。

## 功能

- **聊天问答**：多轮对话（每个会话独立历史，可配置长度）
- **定时闹钟**：自然语言设置，支持一次性 / 每天 / 每周 / 工作日
  - 「明天早上9点提醒我开会」「10分钟后提醒我喝水」「每个工作日下午6点提醒我填日报」
  - 「查看闹钟」「取消闹钟 1」
- 闹钟持久化到 `data/alarms.json`，重启自动恢复（错过 10 分钟内的补发）
- **每日新闻**：每天 09:30 推「科技 15 条 + 综合 15 条」，AI 一句话摘要，链接可点
  - 有 90 分钟补发窗口；同一天只推一次（宁可漏发不重复）
  - `my-im-agent --news-now --news-dry-run` 可先在终端预览

## 两种接入方式

| 方式 | 命令 | 过程可见 | 需要公网 IP |
| --- | --- | --- | --- |
| 自建应用（回调） | `--adapter wecom` | 只看到最终回复 | 需要 |
| **智能机器人（长连接）** | `--adapter wecom-bot` | **流式：正在理解 → 正在设置闹钟 → 结果** | **不需要** |

想看到 Agent 的处理过程就用 `wecom-bot`，见
[docs/SETUP_WECOM.md 附录](docs/SETUP_WECOM.md#附改用智能机器人长连接想看到处理过程就用这个)。


## 快速开始

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

cp .env.example .env             # 填好 LLM 配置
my-im-agent --adapter console    # 先用终端调试，输入 exit 退出
```

## 接入企业微信

1. 企业微信管理后台 → 应用管理 → 创建自建应用，拿到 **AgentId / Secret**
2. 我的企业 → 企业信息 → **CorpID**
3. 自建应用 → 接收消息 → 设置 API 接收：
   - URL：`https://<你的公网地址>/wecom/callback`
   - Token / EncodingAESKey：随机生成，填到 `.env`
4. 自建应用 → 企业可信 IP：把**出口公网 IP** 加进去（不加会报 60020，发不出消息）
5. 启动：

```bash
my-im-agent --adapter wecom      # 默认监听 0.0.0.0:8666
```

> 本机调试没有公网地址时，用内网穿透（cpolar / natapp / 花生壳）把 8666 端口映射出去。
> **每一步怎么点、怎么验，见 [docs/SETUP_WECOM.md](docs/SETUP_WECOM.md)。**

配好后每一步都能立刻验证：

```bash
my-im-agent --check                      # 体检：配置 + 加解密 + 接口连通性
my-im-agent --check --send-to <UserID>   # 给指定成员发一条测试消息
```

## 环境变量

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `IM_AGENT_LLM_BASE_URL` | OpenAI 兼容接口地址 | `https://api.openai.com/v1` |
| `IM_AGENT_LLM_API_KEY` | API Key | 空（缺省走 Echo，不联网） |
| `IM_AGENT_LLM_MODEL` | 模型名 | `gpt-4o-mini` |
| `IM_AGENT_MAX_TOKENS` | 单次请求 token 上限（推理模型要给足） | `4000` |
| `IM_AGENT_MAX_TURNS` | 单会话历史条数 | `20` |
| `IM_AGENT_DATA_DIR` | 闹钟/新闻等数据目录 | `data` |
| `IM_AGENT_NEWS_ENABLED` | 新闻推送开关 | `1` |
| `IM_AGENT_NEWS_AT` | 每天推送时刻 | `09:30` |
| `IM_AGENT_NEWS_TARGET_USER` | 接收者 UserID | `ChenKeCheng` |
| `IM_AGENT_NEWS_TOP_N` | 每个板块条数 | `15` |
| `WECOM_CORP_ID` | 企业 ID | — |
| `WECOM_AGENT_ID` | 自建应用 AgentId | — |
| `WECOM_SECRET` | 自建应用 Secret | — |
| `WECOM_TOKEN` | 回调 Token | — |
| `WECOM_ENCODING_AES_KEY` | 回调 EncodingAESKey（43 位） | — |
| `WECOM_CALLBACK_HOST` / `WECOM_CALLBACK_PORT` | 回调监听地址 | `0.0.0.0` / `8666` |
| `WECOM_CALLBACK_PATH` | 回调路径 | `/wecom/callback` |

## 设计要点（owner 划分）

- **模型负责**：意图判定、从自然语言抽取闹钟时间/内容、生成聊天回复
  （Agently `ModelRequest` + Pydantic 契约，见 `llm.py`）
- **宿主负责**：时间解析与合法性校验、闹钟 id 成员校验、入库/调度/推送、
  加解密与签名（全部确定性逻辑，有测试覆盖）

## 目录结构

```
src/my_im_agent/
├── config.py           # 环境变量 / .env 配置（LLM + 企业微信）
├── llm.py              # 意图契约 + Agently IntentEngine
├── core.py             # Brain：意图路由 → 回复 / 闹钟副作用
├── alarms.py           # 闹钟存储（JSON 原子写）+ 调度器 + 重启补偿
├── adapters/
│   ├── base.py         # 消息模型 + IMAdapter 抽象
│   └── console.py      # 终端调试适配器
├── wecom/
│   ├── crypto.py       # 回调加解密 + 签名（AES-256-CBC / sha1）
│   ├── api.py          # access_token 缓存 + 主动发消息
│   └── adapter.py      # FastAPI 回调服务 + 队列 + worker
└── cli.py              # 装配入口
tests/                  # 50 个测试：加解密 / 调度 / 意图副作用 / 回调接口
docs/SPEC.md            # 技术规格：所有权账本 / 节点账本 / 数据契约 / 已知限制
```

## 开发

```bash
pytest          # 跑测试
ruff check .    # 静态检查
```

## License

MIT
