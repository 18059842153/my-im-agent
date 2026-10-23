# my-im-agent

一个用 Python 写的即时通讯（IM）AI Agent：把「收到消息 → 交给大模型 → 回复消息」这条链路抽象出来，
通过可插拔的 **Adapter** 对接不同的 IM 平台（终端、企业微信、钉钉、飞书、Telegram…）。

当前处于骨架阶段，包含：配置加载、Agent 编排、消息模型、控制台 Adapter、CI 与测试。

## 快速开始

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

cp .env.example .env             # 填好 LLM 配置
my-im-agent --adapter console        # 输入 exit 或 quit 退出
```

也可以不安装，直接跑：

```bash
python -m my_im_agent --adapter console
```

覆盖配置（优先级高于 .env）：

```bash
my-im-agent --model gpt-4o-mini --base-url https://api.openai.com/v1
```

## 环境变量

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `IM_AGENT_LLM_BASE_URL` | OpenAI 兼容接口地址 | `https://api.openai.com/v1` |
| `IM_AGENT_LLM_API_KEY` | API Key | 空（缺省时走 `EchoProvider`，不联网） |
| `IM_AGENT_LLM_MODEL` | 模型名 | `gpt-4o-mini` |
| `IM_AGENT_SYSTEM_PROMPT` | 系统提示词 | 见 `config.py` |
| `IM_AGENT_MAX_TURNS` | 单轮最大历史条数 | `20` |

## 目录结构

```
src/my_im_agent/
├── __init__.py
├── __main__.py        # python -m my_im_agent
├── cli.py             # 命令行入口
├── config.py          # 环境变量 / .env 配置
├── agent.py           # Agent 编排 + LLM Provider
└── adapters/
    ├── base.py        # 消息模型 + Adapter 抽象基类
    └── console.py     # 终端 REPL Adapter
tests/
.github/workflows/ci.yml
```

## 扩展新的 IM 平台

实现 `adapters/base.py` 里的 `IMAdapter` 即可：

```python
class IMAdapter(ABC):
    async def start(self, on_message: Handler) -> None: ...   # 接收消息
    async def send(self, msg: OutboundMessage) -> None: ...   # 发送消息
```

## 开发

```bash
pytest          # 跑测试
ruff check .    # 静态检查
```

## License

MIT
