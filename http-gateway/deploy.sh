#!/usr/bin/env bash
# Leads Plaza HTTP 网关部署脚本 —— 在 VPS 的 web console 里执行（不要假设 SSH 可用）。
#
# 用法（二选一）：
#   A. 已把 leads-plaza 目录传到 VPS（web console 文件上传）：
#        cd ~/leads-plaza && bash http-gateway/deploy.sh
#   B. 从公开仓库拉取（需先把 http-gateway/ 推到公开仓库，见部署文档）：
#        bash deploy.sh --clone
#
# 做什么：建 venv、装依赖、把代码装到 /opt/leads-gateway、写 systemd + nginx、
# 启动服务并本地 curl 验 /healthz。HTTPS 证书用 certbot 另起一步（见文档）。
set -euo pipefail

INSTALL_DIR=/opt/leads-gateway
REPO_URL=https://github.com/liangh2020/leads-plaza-mcp-server.git

if [ "${1:-}" = "--clone" ]; then
  TMP=$(mktemp -d)
  echo "== clone $REPO_URL"
  git clone --depth 1 "$REPO_URL" "$TMP/repo"
  SRC="$TMP/repo"
  # 公开仓库的布局：根目录即 mcp_server 内容，http-gateway/ 在根目录下（推送后）
  if [ ! -f "$SRC/http-gateway/gateway.py" ]; then
    echo "!! 公开仓库里还没有 http-gateway/gateway.py：请先把 http-gateway/ 推上去，"
    echo "   或改用方式 A（文件上传）再跑本脚本。"
    exit 1
  fi
  GW_SRC="$SRC/http-gateway"
  MCP_SRC="$SRC"   # mcp-server-oss 根目录 = mcp_server 内容
else
  # 方式 A：脚本位于 leads-plaza/http-gateway/deploy.sh
  SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
  GW_SRC="$SCRIPT_DIR"
  MCP_SRC=$(cd "$SCRIPT_DIR/../mcp_server" && pwd)
fi

echo "== 先装系统依赖（必须在建 venv 之前：新机常缺 git / python3-venv / nginx / curl）"
if ! command -v nginx >/dev/null 2>&1 || ! command -v git >/dev/null 2>&1 \
   || ! command -v curl >/dev/null 2>&1 \
   || ! python3 -c "import venv, ensurepip" 2>/dev/null; then
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nginx git curl python3-venv python3.12-venv
fi
sudo mkdir -p /etc/nginx/sites-available /etc/nginx/sites-enabled

echo "== 安装到 $INSTALL_DIR（需要 sudo/root）"
# 注意：必须拷目录“内容”而不是目录本身——旧版 cp -r "$SRC" "$DEST"
# 在 DEST 已存在时会把新代码嵌套成 DEST/repo 或 DEST/http-gateway，
# 服务实际加载的顶层文件仍是旧版（2026-10-25 重部署实测踩坑）。
sudo mkdir -p "$INSTALL_DIR/http-gateway" "$INSTALL_DIR/mcp_server"
sudo cp -r "$GW_SRC/." "$INSTALL_DIR/http-gateway/"
sudo cp -r "$MCP_SRC/." "$INSTALL_DIR/mcp_server/"
# 清掉旧版嵌套留下的残目录，避免下次再被误加载
sudo rm -rf "$INSTALL_DIR/http-gateway/http-gateway" "$INSTALL_DIR/mcp_server/repo"
sudo rm -rf "$INSTALL_DIR/http-gateway/.venv" "$INSTALL_DIR/mcp_server/.venv" \
           "$INSTALL_DIR/mcp_server/__pycache__" "$INSTALL_DIR/http-gateway/__pycache__"

echo "== 建 venv、装依赖"
sudo python3 -m venv "$INSTALL_DIR/.venv"
sudo "$INSTALL_DIR/.venv/bin/pip" install -q --upgrade pip
sudo "$INSTALL_DIR/.venv/bin/pip" install -q -r "$INSTALL_DIR/http-gateway/requirements.txt"

echo "== 写环境文件 /etc/leads-gateway.env（LEADS_PAY_TO 为空时先占位，见文档第 2 步补）"
if [ ! -f /etc/leads-gateway.env ]; then
  echo "LEADS_NETWORK=base" | sudo tee /etc/leads-gateway.env >/dev/null
  echo "LEADS_PAY_TO=${LEADS_PAY_TO:-}" | sudo tee -a /etc/leads-gateway.env >/dev/null
  echo "GATEWAY_ALLOW_MOCK=false" | sudo tee -a /etc/leads-gateway.env >/dev/null
fi

echo "== 装 systemd + nginx"
sudo cp "$INSTALL_DIR/http-gateway/leads-gateway.service" /etc/systemd/system/
sudo cp "$INSTALL_DIR/http-gateway/nginx-leads-gateway.conf" /etc/nginx/sites-available/leads-gateway
sudo ln -sf /etc/nginx/sites-available/leads-gateway /etc/nginx/sites-enabled/leads-gateway
sudo systemctl daemon-reload
sudo systemctl enable --now leads-gateway
sudo nginx -t && sudo systemctl reload nginx

echo "== 本地验证"
sleep 2
curl -fsS http://127.0.0.1:8090/healthz && echo
echo "部署完成。下一步：DNS + certbot（见 docs/gateway-deploy-2026-10-24.md）。"
if ! grep -q "^LEADS_PAY_TO=0x" /etc/leads-gateway.env; then
  echo "!! 注意：LEADS_PAY_TO 未填，网关当前为占位 402 模式；主网结算前务必填入收款地址并重启服务。"
fi
