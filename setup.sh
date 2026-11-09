#!/usr/bin/env bash
# Claude Max 订阅 → 本地反代网关（CLIProxyAPI）一键安装
# 适配：macOS / Apple Silicon / 不依赖 brew、go、node、docker
# 用法：bash setup.sh
set -euo pipefail

INSTALL_DIR="$HOME/claude-gateway"
BIN="$INSTALL_DIR/cli-proxy-api"
ARCH="arm64"                    # Apple Silicon；Intel Mac 改成 amd64
PORT="8317"

mkdir -p "$INSTALL_DIR/logs"
cd "$INSTALL_DIR"

echo "==> 1/5  查询 CLIProxyAPI 最新版本"
REL=$(curl -fsSL https://api.github.com/repos/router-for-me/CLIProxyAPI/releases/latest)
TAG=$(printf '%s' "$REL" | jq -r '.tag_name')
echo "    latest = $TAG"

echo "==> 2/5  挑选 darwin_${ARCH} 资产"
URL=$(printf '%s' "$REL" \
  | jq -r --arg a "darwin_${ARCH}" '.assets[]
      | select(.name | ascii_downcase | test($a))
      | .browser_download_url' | head -1)
if [ -z "$URL" ]; then
  echo "!! 没有找到 darwin_${ARCH} 资产。请打开下面页面手动下载后放到 $INSTALL_DIR："
  echo "   https://github.com/router-for-me/CLIProxyAPI/releases"
  exit 1
fi
echo "    $URL"

echo "==> 3/5  下载并解包"
curl -fL --progress-bar -o pkg.download "$URL"
case "$URL" in
  *.zip)          unzip -o -q pkg.download -d . ;;
  *.tar.gz|*.tgz) tar xzf pkg.download -C . ;;
  *)              mv -f pkg.download cli-proxy-api ;;
esac
rm -f pkg.download

# 在解包结果里找可执行文件（不同版本打包层级不同）
CAND=$(find "$INSTALL_DIR" -maxdepth 3 -type f \
        \( -name 'cli-proxy-api' -o -name 'cli-proxy-api-*' -o -name 'CLIProxyAPI*' \) \
        ! -name '*.tar.gz' ! -name '*.zip' | head -1)
if [ -z "$CAND" ]; then
  CAND=$(find "$INSTALL_DIR" -maxdepth 3 -type f -perm -u+x ! -name '*.sh' | head -1)
fi
[ -n "$CAND" ] || { echo "!! 解包里没找到可执行文件，请手动确认"; exit 1; }
[ "$CAND" = "$BIN" ] || mv -f "$CAND" "$BIN"
chmod +x "$BIN"
xattr -d com.apple.quarantine "$BIN" 2>/dev/null || true
echo "    binary = $BIN"
"$BIN" --version 2>/dev/null || true

echo "==> 4/5  生成 config.yaml（已存在则跳过）"
if [ ! -f "$INSTALL_DIR/config.yaml" ]; then
  K1="sk-local-$(openssl rand -hex 12)"
  K2="sk-local-$(openssl rand -hex 12)"
  K3="sk-local-$(openssl rand -hex 12)"
  SECRET=$(openssl rand -hex 24)
  cat > "$INSTALL_DIR/config.yaml" <<YAML
# Claude Max 订阅反代网关
host: "127.0.0.1"        # 同局域网直连可改 0.0.0.0（务必保留 api-keys）
port: $PORT
auth-dir: "$INSTALL_DIR/auth"

api-keys:
  - "$K1"
  - "$K2"
  - "$K3"

remote-management:
  allow-remote: false
  secret-key: "$SECRET"

# 统一模型别名：客户端只写 opus / sonnet，上游换版本只改这里
oauth-model-alias:
  claude:
    - name: "claude-opus-4-5-20251101"
      alias: "opus"
      fork: true
    - name: "claude-sonnet-4-5-20250929"
      alias: "sonnet"
      fork: true
    - name: "claude-haiku-4-5-20251001"
      alias: "haiku"
      fork: true
YAML
  echo "    已生成。发给各设备的 Key："
  grep -E '^  - "sk-local' "$INSTALL_DIR/config.yaml" | tr -d ' "'
else
  echo "    config.yaml 已存在，保留不动"
fi

echo "==> 5/5  下一步：登录 Claude Max 账号"
cat <<EOF

  1) 登录（会打开浏览器，选择你的 Max 账号授权）：
       cd $INSTALL_DIR && ./cli-proxy-api -claude-login
     （部分版本参数是 -login，报错就 ./cli-proxy-api -h 看菜单）

  2) 常驻启动：
       ./cli-proxy-api -config $INSTALL_DIR/config.yaml
     或者装成开机自启（推荐）：
       cp $(dirname "$0")/com.local.cliproxyapi.plist ~/Library/LaunchAgents/
       launchctl load -w ~/Library/LaunchAgents/com.local.cliproxyapi.plist

  3) 自测：
       curl -s http://127.0.0.1:$PORT/v1/models \\
         -H "Authorization: Bearer <上面任一 Key>" | jq '.data[].id'

  4) 手机/平板接入：见同目录 README.md

EOF
