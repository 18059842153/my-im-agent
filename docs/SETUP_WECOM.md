# 企业微信接入操作手册

从零到「在企业微信里跟 Agent 对话、并收到定时闹钟」的完整步骤。
按顺序做，每步都有验证方法，做完一步确认一步。

---

## 第 0 步：准备清单

先确认你手上有什么：

- [ ] 企业微信管理员权限（能进管理后台）
- [ ] 一台能跑 Python 的机器（当前这台就行）
- [ ] 出口公网 IP：**36.251.68.43**（2026-10-24 实测，福建福州联通；宽带重拨会变，用前再确认）

> 复核 IP 的方法：浏览器打开 https://myip.ipip.net 看显示的地址。

---

## 第 1 步：创建自建应用，拿 AgentId / Secret

1. 打开 https://work.weixin.qq.com/wework_admin/frame 登录管理后台
2. 左侧 **应用管理** → **应用** → 下方 **自建** → 点 **创建应用**
3. 填应用名（比如 `IM Agent`）、上传个图标、可见成员**至少选你自己**
4. 创建完成后进入应用详情页，记下：
   - **AgentId**：页面上的一串数字
   - **Secret**：点「查看」，会推送到你的企业微信 App 里，去手机上看

✅ **验证**：能同时看到 AgentId 和 Secret 就对了。

⚠️ 坑：Secret 是**应用**的 Secret，不是「通讯录同步」的 Secret，别拿错。

---

## 第 2 步：拿 CorpID

**我的企业** → **企业信息** → 拉到页面最底部 → **企业ID**

一串 `ww` 开头的字符串。

✅ **验证**：以 `ww` 开头。

---

## 第 3 步：填 .env（先填已有的部分）

在项目目录里：

```bash
cp .env.example .env
```

编辑 `.env`，把这三项先填上（后面两步再补 Token 和 AESKey）：

```ini
IM_AGENT_LLM_BASE_URL=https://api.agicto.cn/v1
IM_AGENT_LLM_API_KEY=<你的 key>
IM_AGENT_LLM_MODEL=deepseek-flash

WECOM_CORP_ID=ww........   # 第 2 步
WECOM_AGENT_ID=1000002     # 第 1 步
WECOM_SECRET=<第 1 步的 Secret>
```

✅ **验证**：

```bash
my-im-agent --check --probe-llm
```

看到 `✓ LLM API Key` 和 `✓ 模型可用` 就说明大模型这条路通了。
（企业微信那几项还是 ✗ 没关系，下一步才配。）

---

## 第 4 步：把出口 IP 加进「企业可信 IP」

**这一步必须做，否则发消息一定报 60020。**

1. 应用管理 → 你的应用 → 详情页往下翻，找到 **企业可信IP**
2. 点「配置」，填入 `36.251.68.43`，保存
3. 需要企业微信管理员扫码/验证

✅ **验证**：

```bash
my-im-agent --check
```

`gettoken 成功 ✓` 说明 IP 已经被信任了。
如果还报 `60020`，说明填的 IP 不对——看报错里 `from ip: x.x.x.x`，填那个。

> 注意：主动发消息是**本机直接调 API**，出口 IP 是本机公网 IP；
> 内网穿透只管「入站回调」，不影响这个值。只有把服务搬到云服务器上时才要换成服务器的 IP。

---

## 第 5 步：搞一个公网回调地址（内网穿透）

企业微信要把消息推给你，需要能访问到你的机器。本机调试用穿透工具。

**方案 A：cpolar（推荐，有免费固定域名）**

1. 注册 https://www.cpolar.com 并登录，拿到 authtoken
2. 下载 Windows 客户端并安装
3. 配置 token：`cpolar authtoken <你的token>`
4. 起隧道：

```bash
cpolar http 8666
```

会显示类似 `https://xxxx.r6.cpolar.top -> http://127.0.0.1:8666`
记下这个 `https://xxxx.r6.cpolar.top`。

**方案 B：natapp**

1. 注册 https://natapp.cn ，免费/付费买一条隧道，**隧道协议选 Web（HTTP/HTTPS）**，本地端口填 `8666`
2. 下载 Windows 客户端，把 `config.ini` 里的 authtoken 换成你自己的
3. 双击 `natapp.exe`，记下它显示的 `http://xxxx.natappfree.cc`

> ⚠️ 免费域名每次重启会变。变了就要回企业微信后台改回调 URL。
> 想要稳定，买个固定二级域名（十几块/月）。

✅ **验证**：浏览器打开 `https://你的域名/healthz`，应该看到 `{"ok": true, ...}`（需要先做完第 6 步启动服务）。

---

## 第 6 步：生成 Token / EncodingAESKey，填进 .env

1. 应用详情页 → **接收消息** → **设置API接收** → 点 **随机生成** 两个按钮
2. 把生成出来的 **Token** 和 **EncodingAESKey** 复制到 `.env`：

```ini
WECOM_TOKEN=<粘贴>
WECOM_ENCODING_AES_KEY=<粘贴，43 位>
```

3. **先别点保存**，先启动服务（第 7 步）

---

## 第 7 步：启动服务

```bash
my-im-agent --adapter wecom
```

看到 `WeCom callback listening on http://0.0.0.0:8666/wecom/callback` 就成了。
**这个窗口保持开着。**

✅ **验证**：

```bash
my-im-agent --check
```

应该看到 `✓ EncodingAESKey 可用（加解密往返）`、`✓ gettoken 成功`。

---

## 第 8 步：保存回调配置（触发官方验证）

回到企业微信后台那一页：

1. URL 填：`https://你的域名/wecom/callback`（第 5 步拿到的域名 + `/wecom/callback`）
2. Token / EncodingAESKey 就是第 6 步生成的那两个
3. 点 **保存**

企业微信会立刻向你的地址发一次 GET 验证请求。
如果服务日志出现 `GET /wecom/callback 200`，**保存成功**。

❌ 如果保存失败，按顺序排查：
- 服务没启动 / 崩了 → 看终端日志
- 域名填错、带了多余的路径或斜杠
- 穿透工具的端口不是 8666
- `.env` 里的 Token/AESKey 和页面上**不一致**（改了 .env 要重启服务）

---

## 第 9 步：给自己发一条测试消息

需要你的**成员 UserID**：通讯录 → 点自己的名字 → 页面上的「账号」就是（不是姓名，不是手机号）。

```bash
my-im-agent --check --send-to ZhangSan
```

✅ **验证**：企业微信里收到「my-im-agent 自检消息：配置正常 ✅」。

---

## 第 10 步：开始用

在企业微信里给你的应用发消息：

| 你说 | Agent 做什么 |
| --- | --- |
| 你好，介绍一下你自己 | 正常聊天 |
| 明天早上9点提醒我开会 | 建闹钟（一次性） |
| 每个工作日下午6点提醒我填日报 | 建闹钟（工作日重复） |
| 查看闹钟 | 列出所有闹钟 |
| 取消闹钟 1 | 删掉 #1 |

到点后，你会**主动收到**一条 `⏰ 闹钟提醒：...`，不需要你先说话。

---

## 错误码速查

| 错误码 | 含义 | 怎么办 |
| --- | --- | --- |
| `60020` | 出口 IP 不在可信 IP 白名单 | 第 4 步，填报错里 `from ip:` 显示的那个 IP |
| `40013` | CorpID 不对 | 第 2 步，去「我的企业→企业信息」重新复制 |
| `40001` | Secret 不对 | 第 1 步，确认是**这个应用**的 Secret |
| `82001` / `301002` | 成员不在应用可见范围 | 应用详情 → 可见范围，把自己加进去 |
| `45009` | 接口调用超过频率限制 | 稍等重试 |
| 回调保存失败 | 服务没在跑 / Token 不一致 | 第 7、8 步 |

---

## 常用命令

```bash
my-im-agent --check                      # 全量体检
my-im-agent --check --send-to <UserID>   # 发一条测试消息
my-im-agent --adapter wecom              # 正式运行（企业微信）
my-im-agent --adapter console            # 终端调试（不联网也能跑）
my-im-agent --adapter wecom --verbose    # 出问题时看详细日志
my-im-agent --adapter wecom-bot          # 智能机器人长连接（流式，见文末附录）
```

---

# 附：改用「智能机器人」长连接（想看到处理过程就用这个）

自建应用是「一问一答的纯文本」，消息发出去就定死，聊天窗口里看不到 Agent 干了什么。
如果想让用户看到 **「正在理解 → 正在设置闹钟 → 结果」** 这样的过程，改用**智能机器人 + 长连接**。

## 好处

| 项 | 自建应用（回调） | 智能机器人（长连接） |
| --- | --- | --- |
| 过程可见 | ✗ | **✓ 流式刷新同一条气泡** |
| 公网 IP / 回调地址 | 必需 | **不需要** |
| 消息加解密 | 需要 | **不需要（明文）** |
| 闹钟主动推送 | 需要「企业可信 IP」 | 内置 `aibot_send_msg` |

## 开通步骤

1. 企业微信 PC 端 → **工作台 → 智能机器人 → 创建**
2. 进入机器人配置页 → 开启「**API 模式**」→ 选择「**长连接**」
3. 复制 **BotID** 和 **Secret**

> ⚠️ 这个 Secret 是**长连接专用**的，跟回调模式的 Token / EncodingAESKey 不是同一套。
> ⚠️ API 模式只能二选一：切到长连接后，原来的回调地址会**失效**；反之亦然。

## 填配置并启动

```ini
WECOM_BOT_ID=你的BotID
WECOM_BOT_SECRET=你的Secret
# 可选：机器人名字里带空格时填上，群聊 @ 才能正确剥离
WECOM_BOT_NAME=IM Agent
```

```bash
my-im-agent --adapter wecom-bot
```

日志出现 `WeCom bot authenticated (long connection ready)` 就通了。

## 你会在聊天窗口看到

```
用户：明天早上9点提醒我开会

机器人（同一条气泡，逐段刷新）：
🔍 正在理解你的意思…
⏰ 正在设置闹钟…
⏰ 已设好闹钟 #1：2026-10-28 09:00（一次性）开会
```

## 注意事项

| 事项 | 说明 |
| --- | --- |
| 单连接 | 同一机器人同时只能有一条长连接，别多开，否则新连接会踢掉旧的 |
| 心跳 | SDK 内部每 30 秒自动 ping，断线自动重连 |
| userid 可能被加密 | 若机器人创建者不是超级管理员，回调里的 `from.userid` 是加密值；主动推送仍可用同一 id |
| 服务器要求 | 只需能出网访问 `wss://openws.work.weixin.qq.com:443`，内网也行 |

