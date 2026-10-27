# 部署到云服务器（可选）

本地 + 内网穿透跑通之后，如果想让它 7×24 挂着，再搬到这里。
下面的步骤以腾讯云轻量为例，其它厂商同理（换 IP 和防火墙位置即可）。

---

## 第 1 步 · 选镜像（重要）

**选 Ubuntu 24.04 或 Debian 12。**

这两个自带 Python 3.12 / 3.11。项目要求 `Python >= 3.11`，
选 Ubuntu 22.04（自带 3.10）会在安装阶段直接报错退出。

配置 2核2G 足够——Agent 常驻只占百来 MB 内存。

## 第 2 步 · 防火墙放行 8666

控制台 → 实例 → **防火墙** → 添加规则：

| 协议 | 端口 | 来源 |
| --- | --- | --- |
| TCP | **8666** | 全部 IPv4 |

> 轻量只管控**入站**，出站默认全放行（主动调企业微信 API 不受影响）。

## 第 3 步 · 跑安装脚本

```bash
git clone https://github.com/18059842153/my-im-agent.git
cd my-im-agent
sudo bash deploy/install.sh
```

脚本做这些事：检查 Python 版本 → 建 `my-im-agent` 系统用户 → 代码放
`/opt/my-im-agent` → 建虚拟环境装依赖 → 数据目录 `/var/lib/my-im-agent`
→ 安装并 enable systemd 服务。

pip 默认走 `pypi.org`。国内慢就换源：

```bash
sudo PYPI_URL=https://pypi.tuna.tsinghua.edu.cn/simple bash deploy/install.sh
```

## 第 4 步 · 填 .env

```bash
sudo vim /opt/my-im-agent/.env
```

照 `.env.example` 填，最少六项：

```ini
IM_AGENT_LLM_BASE_URL=https://api.agicto.cn/v1
IM_AGENT_LLM_API_KEY=...
IM_AGENT_LLM_MODEL=deepseek-flash

WECOM_CORP_ID=ww...
WECOM_AGENT_ID=1000002
WECOM_SECRET=...
WECOM_TOKEN=...
WECOM_ENCODING_AES_KEY=...
```

改完**必须重启**才生效：`sudo systemctl restart my-im-agent`

## 第 5 步 · 启动并自检

```bash
sudo systemctl start my-im-agent
sudo journalctl -u my-im-agent -f              # 实时日志
cd /opt/my-im-agent && sudo -u my-im-agent ./.venv/bin/python -m my_im_agent --check
```

## 第 6 步 · 换掉企业微信的两处配置

搬到云上后，这两处**必须跟着改**，否则收不到消息也发不出去：

1. **企业可信 IP**：改成云服务器的公网 IP
2. **回调 URL**：改成 `http://<服务器公网IP>:8666/wecom/callback`

## 运维命令

```bash
sudo systemctl status my-im-agent     # 状态
sudo systemctl restart my-im-agent    # 改了 .env 之后
sudo journalctl -u my-im-agent -n 100 # 最近日志
cd /opt/my-im-agent && git pull && sudo systemctl restart my-im-agent   # 升级
```

## 关于 HTTPS

企业微信回调用 `http://IP:8666` 即可。想要 HTTPS，可以：
在前面挂一层 Nginx 反向代理 + 免费证书（腾讯云可申请免费 SSL 证书），
把 443 转到本机 8666，回调 URL 填 `https://域名/wecom/callback`。

## 数据备份

闹钟存在 `/var/lib/my-im-agent/alarms.json`，单文件。
升级代码不会覆盖它（目录独立），但换机器时记得拷走。

> ⚠️ **要手工改这个文件，必须先 `systemctl stop my-im-agent`。**
> 服务进程会把闹钟列表缓存在内存里，改完不停服务的话，
> 下一次新增闹钟会用内存里那份把文件覆盖回去（表现为你的修改"不生效"）。
