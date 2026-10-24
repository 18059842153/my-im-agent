#!/usr/bin/env bash
# 在腾讯云轻量（Ubuntu 24.04 / Debian 12）上一键部署 my-im-agent。
# 用法：sudo bash install.sh
# 可选环境变量：REPO_URL / APP_DIR / DATA_DIR / PYPI_URL

set -euo pipefail

APP_NAME="my-im-agent"
REPO_URL="${REPO_URL:-https://github.com/18059842153/my-im-agent.git}"
APP_DIR="${APP_DIR:-/opt/my-im-agent}"
DATA_DIR="${DATA_DIR:-/var/lib/my-im-agent}"
SERVICE_USER="${SERVICE_USER:-my-im-agent}"
PYPI_URL="${PYPI_URL:-https://pypi.org/simple}"
REQUIRED_PY_MAJOR=3
REQUIRED_PY_MINOR=11

log()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 root 执行：sudo bash install.sh"

# 1. 检查 Python 版本（项目要求 >= 3.11；Ubuntu 22.04 的 3.10 不够）
command -v python3 >/dev/null 2>&1 || die "没找到 python3"
PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major} {sys.version_info.minor}")')"
PY_MAJOR="${PY_VER%% *}"
PY_MINOR="${PY_VER##* }"
if [ "$PY_MAJOR" -lt "$REQUIRED_PY_MAJOR" ] ||
   { [ "$PY_MAJOR" -eq "$REQUIRED_PY_MAJOR" ] && [ "$PY_MINOR" -lt "$REQUIRED_PY_MINOR" ]; }; then
    die "Python 版本过低（当前 $(python3 -V)），需要 >= ${REQUIRED_PY_MAJOR}.${REQUIRED_PY_MINOR}。
     建议重装系统镜像为 Ubuntu 24.04 或 Debian 12。"
fi
log "Python 版本检查通过：$(python3 -V)"

# 2. 装系统依赖
if command -v apt-get >/dev/null 2>&1; then
    log "安装系统依赖（python3-venv / git）"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq python3-venv python3-pip git curl ca-certificates
fi

# 3. 建系统用户（无登录权限）
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    log "创建系统用户 $SERVICE_USER"
    useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

# 4. 拉代码（已存在则更新）
if [ -d "$APP_DIR/.git" ]; then
    log "更新代码：$APP_DIR"
    git -C "$APP_DIR" pull --ff-only
else
    log "克隆代码到 $APP_DIR"
    rm -rf "$APP_DIR"
    git clone --depth 1 "$REPO_URL" "$APP_DIR"
fi

# 5. 建虚拟环境并安装依赖
log "创建虚拟环境并安装依赖（源：$PYPI_URL）"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install --upgrade pip -q
# 注意：不要给整个 namespace 赋值插件配置（Agently 会丢默认 path_mapping）
"$APP_DIR/.venv/bin/python" -m pip install --index-url "$PYPI_URL" -e "$APP_DIR" -q

# 6. 数据目录
log "准备数据目录 $DATA_DIR"
mkdir -p "$DATA_DIR"
chown -R "$SERVICE_USER":"$SERVICE_USER" "$DATA_DIR"
chmod 700 "$DATA_DIR"

# 7. 安装 systemd 服务
log "安装 systemd 服务"
install -m 0644 "$APP_DIR/deploy/my-im-agent.service" /etc/systemd/system/my-im-agent.service
chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR"
systemctl daemon-reload
systemctl enable my-im-agent

cat <<EOF

$(printf '\033[1;32m安装完成\033[0m')

接下来两步（必须做，否则服务起不来）：

  1) 填配置
     sudo mkdir -p $APP_DIR
     sudo vim $APP_DIR/.env
     最少要填：IM_AGENT_LLM_API_KEY / WECOM_CORP_ID / WECOM_AGENT_ID /
              WECOM_SECRET / WECOM_TOKEN / WECOM_ENCODING_AES_KEY
     （模板见 $APP_DIR/.env.example）

  2) 启动并自检
     sudo systemctl start my-im-agent
     sudo journalctl -u my-im-agent -f        # 看日志
     cd $APP_DIR && sudo -u $SERVICE_USER ./.venv/bin/python -m my_im_agent --check

别忘了在腾讯云控制台防火墙放行 TCP 8666 端口。
EOF
