#!/usr/bin/env bash
# ==============================================================================
#  Claude Gateway 一键安装器
#  把订阅额度（Claude Max / ChatGPT-Plus-Codex / Grok-xAI）变成一台本地 API 网关
#
#  用法：
#    bash install.sh              # 交互安装
#    bash install.sh --dry-run    # 只打印将做什么，不改动系统
#    bash install.sh --yes        # 全默认，不提问
#    bash install.sh --uninstall  # 卸载
#    bash install.sh --help
#
#  环境变量覆盖（主要给测试用）：
#    CG_DIR=/tmp/cgtest  CG_PORT=18317  bash install.sh
# ==============================================================================
set -euo pipefail

SCRIPT_VERSION="0.1.0"
REPO="${CG_REPO:-router-for-me/CLIProxyAPI}"
GW_DIR="${CG_DIR:-$HOME/claude-gateway}"
BK_DIR="${CG_BACKUP_DIR:-$HOME/claude-gateway-backups}"
PORT="${CG_PORT:-8317}"
# 这几个也可用环境变量覆盖 —— 让「装到临时目录」的测试不会碰真实安装
LABEL_GW="${CG_LABEL:-com.local.cliproxyapi}"
LABEL_WD="${CG_LABEL_WD:-com.local.cgwatchdog}"
KEYCHAIN_SVC="${CG_KEYCHAIN_SVC:-claude-gateway-backup}"
AGENT_DIR="${CG_AGENT_DIR:-$HOME/Library/LaunchAgents}"
UI_PORT="${CG_UI_PORT:-8899}"
LABEL_UI="${CG_CONSOLE_LABEL:-com.local.cgconsole}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DRY_RUN=0
ASSUME_YES=0
DO_UNINSTALL=0
SKIP_LOGIN=0

# ---------- 输出 ----------
if [ -t 1 ]; then
  C_R=$'\033[31m'; C_G=$'\033[32m'; C_Y=$'\033[33m'
  C_B=$'\033[36m'; C_D=$'\033[2m'; C_W=$'\033[1m'; C_0=$'\033[0m'
else
  C_R=; C_G=; C_Y=; C_B=; C_D=; C_W=; C_0=
fi

say()  { printf '%s\n' "$*"; }
ok()   { printf '  %s✓%s %s\n' "$C_G" "$C_0" "$*"; }
warn() { printf '  %s!%s %s\n' "$C_Y" "$C_0" "$*"; }
die()  { printf '\n%s✗ %s%s\n' "$C_R" "$*" "$C_0" >&2; exit 1; }
step() { printf '\n%s▶ %s%s\n' "$C_W" "$*" "$C_0"; }
dim()  { printf '%s%s%s\n' "$C_D" "$*" "$C_0"; }

# dry-run 下唯一允许“执行”的入口：只打印
run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  %s[dry-run]%s %s\n' "$C_D" "$C_0" "$*"
    return 0
  fi
  "$@"
}

need() {
  command -v "$1" >/dev/null 2>&1
}

# ==============================================================================
#  探测
# ==============================================================================

OS=""
ARCH=""
ASSET=""
TMPDIR_INSTALL=""

detect_platform() {
  case "$(uname -s)" in
    Darwin) OS="darwin" ;;
    Linux)  OS="linux" ;;
    *) die "不支持的系统：$(uname -s)（只支持 macOS 与 Linux）" ;;
  esac

  case "$(uname -m)" in
    arm64|aarch64) ARCH="aarch64" ;;
    x86_64|amd64)  ARCH="amd64" ;;
    *) die "不支持的架构：$(uname -m)" ;;
  esac

  if [ "$OS" = "darwin" ] && [ "$ARCH" = "aarch64" ]; then
    ok "平台：macOS Apple Silicon"
  elif [ "$OS" = "darwin" ]; then
    ok "平台：macOS Intel"
  elif [ "$ARCH" = "aarch64" ]; then
    ok "平台：Linux ARM64"
  else
    ok "平台：Linux x86_64"
  fi

  # 服务管理器
  if [ "$OS" = "darwin" ]; then
    need launchctl || die "找不到 launchctl"
    ok "服务管理：launchd"
  else
    need systemctl || die "找不到 systemctl（需要 systemd 才能常驻）"
    ok "服务管理：systemd"
  fi
}

detect_tools() {
  local miss=""
  need curl   || miss="$miss curl"
  need tar    || miss="$miss tar"
  need openssl || miss="$miss openssl"
  if [ -n "$miss" ]; then die "缺少必要命令：$miss"; fi
  ok "基础命令：curl / tar / openssl"

  # 校验工具
  if need shasum; then
    SHA_CMD="shasum -a 256"
  elif need sha256sum; then
    SHA_CMD="sha256sum"
  else
    die "缺少 shasum 或 sha256sum，无法校验下载文件"
  fi
  ok "校验工具：${SHA_CMD%% *}"

  # 口令存储
  if [ "$OS" = "darwin" ] && need security; then
    SECRET_BACKEND="keychain"
    ok "口令存储：钥匙串"
  else
    SECRET_BACKEND="file"
    warn "无钥匙串，备份口令将存到 $GW_DIR/.backup-pass（权限 600）"
  fi
}

# 探测某个 URL 是否可达：返回 0=可达
# 判据：拿到任何 HTTP 状态码都算可达（401/403/404 都正常），000 才算不通
probe_url() {
  local url="$1" proxy="${2:-}" tries="${3:-1}" tmo="${4:-20}"
  local -a a=(-s -o /dev/null -w '%{http_code}' -m "$tmo")
  if [ -n "$proxy" ]; then a+=(-x "$proxy"); fi
  local i code
  for ((i = 0; i < tries; i++)); do
    code=$(curl "${a[@]}" "$url" 2>/dev/null) || code="000"
    if [ -n "$code" ] && [ "$code" != "000" ]; then
      return 0
    fi
    [ "$i" -lt $((tries - 1)) ] && sleep 2
  done
  return 1
}

# 端口开没开（只看 TCP 层，1 秒出结果，不关心目标站点慢不慢）
port_open() {
  nc -z -w 1 127.0.0.1 "$1" >/dev/null 2>&1
}

# 为选中的 provider 找出一个可用的出站方式
# 结果写进 OUTBOUND（空=直连，否则是代理 URL）
OUTBOUND=""
PROXY_CANDIDATES=(7890 7897 1080 1087)

detect_outbound() {
  local test_url="$1"
  say ""
  say "  测试出站连通性：$test_url"
  say ""

  # 1) 直连
  if probe_url "$test_url" "" 2 20; then
    OUTBOUND=""
    ok "直连可用 —— 不需要代理"
    return 0
  fi
  dim "  直连不通"

  # 2) 环境变量里已有的代理
  local envp="${https_proxy:-${HTTPS_PROXY:-}}"
  if [ -n "$envp" ] && probe_url "$test_url" "$envp" 2 12; then
    OUTBOUND="$envp"
    ok "环境变量代理可用：$envp"
    return 0
  fi

  # 3) 本地常见端口：先 TCP 快筛（1 秒/个），再对活着的端口做应用层验证
  say "  扫本地代理端口…"
  local p alive=()
  for p in "${PROXY_CANDIDATES[@]}"; do
    if port_open "$p"; then
      dim "    127.0.0.1:$p 在监听"
      alive+=("$p")
    fi
  done
  if [ "${#alive[@]}" -eq 0 ]; then
    dim "    没有本地代理端口在监听"
  fi
  for p in ${alive[@]+"${alive[@]}"}; do
    say "  验证 127.0.0.1:$p 能否转发到上游…"
    if probe_url "$test_url" "http://127.0.0.1:$p" 2 25; then
      OUTBOUND="http://127.0.0.1:$p"
      ok "发现可用代理：127.0.0.1:$p"
      return 0
    fi
  done

  # 4) 全都不行
  warn "没找到能访问该域名的出站方式"
  say ""
  dim "  常见原因："
  dim "    • 命令行进程不读 macOS「系统代理」设置（浏览器能开≠终端能开）"
  dim "    • 代理软件只在 TUN 模式下接管流量，而 fake-ip 域名解析到了假地址"
  say ""
  if [ "$ASSUME_YES" -eq 0 ] && [ -t 0 ]; then
    printf '  手动指定代理（如 http://127.0.0.1:7897，直接回车=留空并继续）：'
    local manual
    read -r manual || manual=""
    if [ -n "$manual" ]; then
      if probe_url "$test_url" "$manual" 2 15; then
        OUTBOUND="$manual"
        ok "手动代理可用：$manual"
        return 0
      else
        warn "该代理也不通，仍会写入配置（之后可用 cg-doctor 复查）"
        OUTBOUND="$manual"
        return 0
      fi
    fi
  fi
  OUTBOUND=""
  warn "继续安装，但网关可能连不上上游。装完用 cg-doctor 复查。"
  return 0
}

# ==============================================================================
#  交互问答
# ==============================================================================

PROVIDERS=""      # 空格分隔：claude codex xai
NET_MODE=""       # local | lan | tailscale
DEVICE_COUNT=3    # 生成几把客户端 Key

# 读一行；非 tty 或 --yes 时返回默认值
ask() {
  local prompt="$1" default="$2" answer=""
  if [ "$ASSUME_YES" -eq 1 ] || [ ! -t 0 ]; then
    printf '%s%s%s\n' "$C_D" "$default" "$C_0"
    printf '%s' "$default"
    return 0
  fi
  printf '  %s' "$prompt"
  read -r answer || answer=""
  if [ -z "$answer" ]; then answer="$default"; fi
  printf '%s' "$answer"
}

ask_providers() {
  step "选择要反代的额度来源"

  say "  1) Claude Max      —— 走 claude.ai OAuth（需要浏览器授权）"
  say "  2) ChatGPT / Codex —— 走 OpenAI device code（手机或电脑输码即可）"
  say "  3) Grok / xAI      —— 走 xAI device code（同上）"
  say ""

  if [ "$ASSUME_YES" -eq 1 ] || [ ! -t 0 ]; then
    PROVIDERS="claude"
    ok "默认只装 Claude"
    return 0
  fi

  printf '  输入编号，逗号分隔（直接回车 = 1）：'
  local raw
  read -r raw || raw=""
  if [ -z "$raw" ]; then raw="1"; fi

  local n
  for n in $(printf '%s' "$raw" | tr ',' ' '); do
    case "$n" in
      1) PROVIDERS="$PROVIDERS claude" ;;
      2) PROVIDERS="$PROVIDERS codex" ;;
      3) PROVIDERS="$PROVIDERS xai" ;;
      *) warn "忽略无效选项：$n" ;;
    esac
  done
  PROVIDERS=$(printf '%s' "$PROVIDERS" | tr ' ' '\n' | grep -v '^$' | sort -u | tr '\n' ' ' || true)
  if [ -z "$(printf '%s' "$PROVIDERS" | tr -d ' ')" ]; then die "至少要选一个"; fi

  ok "已选：$PROVIDERS"
}

ask_network() {
  step "选择网络范围"

  say "  1) 只本机         —— 127.0.0.1，最安全"
  say "  2) 局域网         —— 同 Wi-Fi 的手机/电脑可连（0.0.0.0）"
  say "  3) 局域网+Tailscale —— 外地设备也能连（需自己装 Tailscale）"
  say ""

  local c
  c=$(ask "输入编号（直接回车 = 2）：" "2")
  case "$c" in
    1) NET_MODE="local" ;;
    3) NET_MODE="tailscale" ;;
    *) NET_MODE="lan" ;;
  esac

  case "$NET_MODE" in
    local)     ok "绑定 127.0.0.1（只有本机能连）" ;;
    lan)       ok "绑定 0.0.0.0（同一局域网可连）" ;;
    tailscale) ok "绑定 0.0.0.0 + Tailscale（装完会提示下一步）" ;;
  esac
}

ask_devices() {
  step "要发几把客户端 Key"

  say "  每台设备一把，泄露时只吊销那一把。"
  local c
  c=$(ask "数量（直接回车 = 3）：" "3")
  case "$c" in
    ''|*[!0-9]*) DEVICE_COUNT=3 ;;
    *) [ "$c" -ge 1 ] && [ "$c" -le 20 ] && DEVICE_COUNT="$c" || DEVICE_COUNT=3 ;;
  esac
  ok "生成 $DEVICE_COUNT 把 Key"
}

# ==============================================================================
#  下载与校验
# ==============================================================================

LATEST_TAG=""
LATEST_VER=""
BIN_PATH=""

fetch_latest() {
  step "查询最新版本"

  LATEST_TAG="${CG_TAG:-}"
  local json=""
  if [ -z "$LATEST_TAG" ]; then
    local api="https://api.github.com/repos/$REPO/releases/latest"
    json=$(curl -sL -m 20 "$api" 2>/dev/null || true)
    LATEST_TAG=$(printf '%s' "$json" | grep -o '"tag_name"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"\([^"]*\)"$/\1/' || true)
  fi

  # 共享出口 IP 容易撞上 GitHub REST API 匿名限流（60 次/小时），回退走 Web 302 重定向解析 tag
  if [ -z "$LATEST_TAG" ]; then
    local eff_url=""
    eff_url=$(curl -fsSL -I -o /dev/null -w '%{url_effective}' -m 20 "https://github.com/$REPO/releases/latest" 2>/dev/null || true)
    case "$eff_url" in
      */releases/tag/*)
        LATEST_TAG="${eff_url##*/releases/tag/}"
        LATEST_TAG="${LATEST_TAG%%\?*}"
        LATEST_TAG="${LATEST_TAG%%/*}"
        ;;
    esac
  fi

  if [ -z "$LATEST_TAG" ] && [ "$DRY_RUN" -eq 1 ]; then
    warn "当前网络无法解析最新 tag，dry-run 使用占位版本 v0.0.0"
    LATEST_TAG="v0.0.0"
  fi
  if [ -z "$LATEST_TAG" ]; then die "解析 tag 失败：$(printf '%s' "$json" | head -3)"; fi

  LATEST_VER="${LATEST_TAG#v}"
  ASSET="CLIProxyAPI_${LATEST_VER}_${OS}_${ARCH}.tar.gz"

  ok "最新版本：$LATEST_TAG"
  ok "对应资产：$ASSET"
}

download_release() {
  step "下载并校验"

  local base="https://github.com/$REPO/releases/download/$LATEST_TAG"
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  %s[dry-run]%s curl -fL %s/%s\n' "$C_D" "$C_0" "$base" "$ASSET"
    return 0
  fi
  TMPDIR_INSTALL=$(mktemp -d "${TMPDIR:-/tmp}/cg-install.XXXXXX")
  # shellcheck disable=SC2064
  trap "rm -rf '$TMPDIR_INSTALL'" EXIT

  # checksums
  dim "  取 checksums.txt…"
  if curl -fsSL -m 30 "$base/checksums.txt" -o "$TMPDIR_INSTALL/checksums.txt" 2>/dev/null; then
    ok "checksums.txt 已获取"
  else
    warn "取不到 checksums.txt，将跳过哈希校验（不推荐）"
    : > "$TMPDIR_INSTALL/checksums.txt"
  fi

  # tarball
  dim "  下载 ${ASSET}（约 22MB）…"
  curl -fL --progress-bar -m 300 "$base/$ASSET" -o "$TMPDIR_INSTALL/$ASSET" \
    || die "下载失败。若在受限网络下，可手动下载后放到 $GW_DIR 再重跑"

  # 校验
  local want got
  want=$(grep -E "[[:space:]]${ASSET}\$" "$TMPDIR_INSTALL/checksums.txt" 2>/dev/null | awk '{print $1}' | head -1 || true)
  if [ -n "$want" ]; then
    got=$($SHA_CMD "$TMPDIR_INSTALL/$ASSET" | awk '{print $1}')
    if [ "$want" = "$got" ]; then
      ok "SHA256 校验通过（${got:0:16}…）"
    else
      die "SHA256 不匹配！
  期望：$want
  实际：$got
  文件可能被篡改或下载损坏，已中止。"
    fi
  else
    warn "checksums.txt 里没有 ${ASSET}，跳过校验"
  fi

  # 解包
  tar -xzf "$TMPDIR_INSTALL/$ASSET" -C "$TMPDIR_INSTALL" || die "解包失败"

  # 找可执行文件：优先 cli-proxy-api，其次任何 Mach-O/ELF
  if [ -f "$TMPDIR_INSTALL/cli-proxy-api" ]; then
    BIN_PATH="$TMPDIR_INSTALL/cli-proxy-api"
  else
    BIN_PATH=$(find "$TMPDIR_INSTALL" -maxdepth 2 -type f -perm -u+x ! -name '*.txt' ! -name '*.md' ! -name '*.yaml' 2>/dev/null | head -1 || true)
  fi
  [ -n "$BIN_PATH" ] && [ -f "$BIN_PATH" ] || die "解包后找不到可执行文件"

  local size
  size=$(stat -f%z "$BIN_PATH" 2>/dev/null || stat -c%s "$BIN_PATH" 2>/dev/null || echo 0)
  [ "$size" -gt 5000000 ] || die "可执行文件太小（$size 字节），疑似损坏"

  ok "解包完成（$(printf '%.0f' "$(echo "$size/1048576" | bc -l 2>/dev/null || echo 60)")MB）"
}

install_binary() {
  step "安装到 $GW_DIR"

  run mkdir -p "$GW_DIR"/{auth,logs,.state}
  run chmod 700 "$GW_DIR/auth" "$GW_DIR/logs" "$GW_DIR/.state"

  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  %s[dry-run]%s cp %s %s/cli-proxy-api\n' "$C_D" "$C_0" "$BIN_PATH" "$GW_DIR"
    return 0
  fi

  # 保留上一版以便回滚
  if [ -f "$GW_DIR/cli-proxy-api" ]; then
    cp -p "$GW_DIR/cli-proxy-api" "$GW_DIR/cli-proxy-api.prev"
    ok "旧版本已备份为 cli-proxy-api.prev"
  fi

  cp "$BIN_PATH" "$GW_DIR/cli-proxy-api"
  chmod 755 "$GW_DIR/cli-proxy-api"
  # macOS 去隔离属性，否则 Gatekeeper 会拦
  if [ "$OS" = "darwin" ]; then
    xattr -d com.apple.quarantine "$GW_DIR/cli-proxy-api" 2>/dev/null || true
  fi
  ok "二进制已就位：$GW_DIR/cli-proxy-api"
}

# ==============================================================================
#  生成 config.yaml
# ==============================================================================

CONFIG="$GW_DIR/config.yaml"
SECRET_FILE="$GW_DIR/.management-secret"
GEN_KEYS=()          # 本次生效的客户端 Key
MGMT_SECRET=""       # 管理面板明文口令

gen_key() { printf 'sk-local-%s' "$(openssl rand -hex 20)"; }

# 从已有 config 里捞回 api-keys（重装时不换 Key，否则所有客户端都要改）
load_existing_keys() {
  [ -f "$CONFIG" ] || return 0
  local k
  while IFS= read -r k; do
    if [ -n "$k" ]; then GEN_KEYS+=("$k"); fi
  done < <(sed -n '/^access:/,/^[a-z]/p' "$CONFIG" 2>/dev/null \
           | grep -oE 'sk-local-[A-Za-z0-9_-]+' | sort -u)
}

# 管理口令：优先复用已有明文，其次从 config 里取（可能是 hash）
load_existing_secret() {
  if [ -f "$SECRET_FILE" ]; then
    MGMT_SECRET=$(cat "$SECRET_FILE" 2>/dev/null || true)
  if [ -n "$MGMT_SECRET" ]; then ok "复用已有管理口令"; return 0; fi
  fi
  local cfg_sec
  cfg_sec=$(grep -E '^[[:space:]]*secret-key:' "$CONFIG" 2>/dev/null | head -1 | sed -E 's/.*secret-key:[[:space:]]*"?([^"]*)"?.*/\1/' || true)
  if [ -n "$cfg_sec" ] && [ "${cfg_sec:0:4}" != '$2a$' ] && [ "${cfg_sec:0:4}" != '$2b$' ]; then
    MGMT_SECRET="$cfg_sec"
    ok "复用已有管理口令"
    return 0
  fi
  MGMT_SECRET=""
}

gen_config() {
  step "生成配置 $CONFIG"

  load_existing_keys
  load_existing_secret

  if [ "${#GEN_KEYS[@]}" -gt 0 ]; then
    ok "复用已有 ${#GEN_KEYS[@]} 把客户端 Key（不会让已配好的设备失效）"
  else
    local i
    for ((i=1; i<=DEVICE_COUNT; i++)); do
      GEN_KEYS+=("$(gen_key)")
    done
    ok "新生成 $DEVICE_COUNT 把客户端 Key"
  fi

  if [ -z "$MGMT_SECRET" ]; then
    MGMT_SECRET="$(openssl rand -hex 16)"
    ok "新生成管理面板口令"
  fi

  # 网络绑定
  local host="0.0.0.0"
  if [ "$NET_MODE" = "local" ]; then host="127.0.0.1"; fi

  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  %s[dry-run]%s 写入 config.yaml（host=%s port=%s proxy=%s）\n' \
      "$C_D" "$C_0" "$host" "$PORT" "${OUTBOUND:-直连}"
    return 0
  fi

  # 备份旧配置
  if [ -f "$CONFIG" ]; then
    cp -p "$CONFIG" "$CONFIG.bak.$(date +%Y%m%d%H%M%S)"
    ok "旧配置已备份"
  fi

  {
    cat <<EOF
# Claude Gateway 配置（由 install.sh v$SCRIPT_VERSION 生成）
# 生成时间：$(date '+%Y-%m-%d %H:%M:%S')
# 本文件含凭证，权限必须是 600
# 注意：网关首次启动后会用 YAML 库重写此文件（把 secret-key 换成 hash），
#       注释位置可能被移动到块外，属正常现象，不影响解析。
config-version: 8

server:
  host: "$host"
  port: $PORT
  response-compression:
    enabled: true
    min-bytes: 1024
  discovery:
    enabled: false

management:
  # 管理面板：装好后访问 http://<地址>:$PORT/v8/management
  # 口令见 ${SECRET_FILE}（本文件里这条会被宿主程序改写成 hash）
  allow-remote: false
  secret-key: "$MGMT_SECRET"

access:
  api-keys:
EOF
    local k
    for k in ${GEN_KEYS[@]+"${GEN_KEYS[@]}"}; do
      printf '    - "%s"\n' "$k"
    done
    cat <<EOF

routing:
  strategy: round-robin
  retry:
    request-retry: 3
    max-retry-interval: 30

requests:
  # 命令行进程不读系统代理；这里显式指定出站
  proxy-url: "${OUTBOUND}"
  tls-handshake-timeout-seconds: 0
  streaming:
    keepalive-seconds: 15

oauth:
  auth-dir: "$GW_DIR/auth"
EOF

    # 预设三家短别名与 upstream 默认参数（无对应凭证时网关会忽略，在控制台接入后即可开箱即用）
    cat <<'EOF'
  # 别名让客户端只填 opus / codex / grok，上游换版本时不用改客户端
  # fork: true 表示保留原始模型名，同时额外暴露别名
  model-alias:
    claude:
      - name: claude-opus-5-5
        alias: opus
        display-name: Opus（最强）
        fork: true
      - name: claude-sonnet-5-5
        alias: sonnet
        display-name: Sonnet（均衡）
        fork: true
      - name: claude-haiku-5-5
        alias: haiku
        display-name: Haiku（最快最省）
        fork: true
    codex:
      # 装完后用 cg-models 查看真实模型名，或导入凭证后由控制台自动校准
      - name: gpt-5.3-codex
        alias: codex
        display-name: Codex（OpenAI）
        fork: true
    xai:
      - name: grok-4.5
        alias: grok
        display-name: Grok（xAI）
        fork: true
EOF

    cat <<EOF

upstream:
  claude:
    # false = 对非 Claude Code 客户端自动套用官方 CLI 指纹（上游只认这个）
    disable-claude-cloak-mode: false
    session-date-pin-days: 7
    model-level-cooling: false
  codex:
    disable-codex-cloaking: false
  xai:
    inject-x-search: false
EOF

    cat <<EOF

observability:
  logs:
    debug: false
    logging-to-file: true
    logs-max-total-size-mb: 50
    error-logs-max-files: 10
    request-log: false
  usage:
    usage-statistics-enabled: true

plugins:
  enabled: false
EOF
  } > "$CONFIG"

  chmod 600 "$CONFIG"
  printf '%s' "$MGMT_SECRET" > "$SECRET_FILE"
  chmod 600 "$SECRET_FILE"

  ok "config.yaml 已写入（$(wc -l < "$CONFIG" | tr -d ' ') 行，权限 600）"
  ok "管理口令已存 $SECRET_FILE"
}

# ==============================================================================
#  服务托管
# ==============================================================================

write_launchd_plists() {
  local gw_plist="$AGENT_DIR/$LABEL_GW.plist"
  local wd_plist="$AGENT_DIR/$LABEL_WD.plist"

  run mkdir -p "$AGENT_DIR"

  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  %s[dry-run]%s 写 %s\n' "$C_D" "$C_0" "$gw_plist"
    printf '  %s[dry-run]%s 写 %s\n' "$C_D" "$C_0" "$wd_plist"
    return 0
  fi

  cat > "$gw_plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL_GW</string>
    <key>ProgramArguments</key>
    <array>
        <string>$GW_DIR/cli-proxy-api</string>
        <string>-config</string>
        <string>$CONFIG</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$GW_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>Umask</key>
    <integer>63</integer>
    <key>StandardOutPath</key>
    <string>$GW_DIR/logs/out.log</string>
    <key>StandardErrorPath</key>
    <string>$GW_DIR/logs/err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>$HOME</string>
    </dict>
    <key>ProcessType</key>
    <string>Standard</string>
</dict>
</plist>
EOF

  cat > "$wd_plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL_WD</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$GW_DIR/watchdog.sh</string>
    </array>
    <key>StartInterval</key>
    <integer>300</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$GW_DIR/logs/watchdog.out.log</string>
    <key>StandardErrorPath</key>
    <string>$GW_DIR/logs/watchdog.err.log</string>
</dict>
</plist>
EOF

  chmod 600 "$gw_plist" "$wd_plist"
  plutil -lint "$gw_plist" >/dev/null || die "生成的 plist 格式有误：$gw_plist"
  plutil -lint "$wd_plist" >/dev/null || die "生成的 plist 格式有误：$wd_plist"
  ok "launchd plist 已写入并校验"
}

write_systemd_units() {
  local as_root=0
  if [ "$(id -u)" -eq 0 ]; then as_root=1; fi

  if [ "$DRY_RUN" -eq 1 ]; then
    if [ "$as_root" -eq 1 ]; then
      printf '  %s[dry-run]%s 写 /etc/systemd/system/cliproxyapi.service（root 模式，建 cliproxy 用户）\n' "$C_D" "$C_0"
    else
      printf '  %s[dry-run]%s 写 ~/.config/systemd/user/cliproxyapi.service（用户模式 + enable-linger）\n' "$C_D" "$C_0"
    fi
    return 0
  fi

  if [ "$as_root" -eq 1 ]; then
    # ---- 系统级：建专用低权限用户 ----
    if ! id cliproxy >/dev/null 2>&1; then
      useradd --system --home-dir "$GW_DIR" --shell /usr/sbin/nologin cliproxy 2>/dev/null \
        || adduser -S -H -h "$GW_DIR" -s /sbin/nologin cliproxy 2>/dev/null \
        || warn "无法创建 cliproxy 用户，将用当前用户运行"
    fi
    if id cliproxy >/dev/null 2>&1; then
      chown -R cliproxy: "$GW_DIR"
      RUN_USER="cliproxy"
    else
      RUN_USER="root"
    fi

    cat > /etc/systemd/system/cliproxyapi.service <<EOF
[Unit]
Description=Claude Gateway (CLIProxyAPI)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$GW_DIR
ExecStart=$GW_DIR/cli-proxy-api -config $CONFIG
Restart=always
RestartSec=5
LimitNOFILE=65536
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$GW_DIR

[Install]
WantedBy=multi-user.target
EOF

    cat > /etc/systemd/system/cliproxyapi-watchdog.service <<EOF
[Unit]
Description=Claude Gateway watchdog

[Service]
Type=oneshot
ExecStart=/bin/bash $GW_DIR/watchdog.sh
EOF

    cat > /etc/systemd/system/cliproxyapi-watchdog.timer <<'EOF'
[Unit]
Description=Run Claude Gateway watchdog every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
AccuracySec=30s

[Install]
WantedBy=timers.target
EOF

    systemctl daemon-reload
    ok "systemd 单元已写入（运行用户：${RUN_USER}）"
    SVC_MODE="system"
  else
    # ---- 用户级 ----
    local udir="$HOME/.config/systemd/user"
    mkdir -p "$udir"

    cat > "$udir/cliproxyapi.service" <<EOF
[Unit]
Description=Claude Gateway (CLIProxyAPI)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$GW_DIR
ExecStart=$GW_DIR/cli-proxy-api -config $CONFIG
Restart=always
RestartSec=5
LimitNOFILE=65536

[Install]
WantedBy=default.target
EOF

    cat > "$udir/cliproxyapi-watchdog.service" <<EOF
[Unit]
Description=Claude Gateway watchdog

[Service]
Type=oneshot
ExecStart=/bin/bash $GW_DIR/watchdog.sh
EOF

    cat > "$udir/cliproxyapi-watchdog.timer" <<'EOF'
[Unit]
Description=Run Claude Gateway watchdog every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
AccuracySec=30s

[Install]
WantedBy=timers.target
EOF

    systemctl --user daemon-reload
    # 登出后仍继续运行
    if command -v loginctl >/dev/null 2>&1; then
      loginctl enable-linger "$(id -un)" 2>/dev/null || warn "enable-linger 失败，注销后服务可能停止"
    fi
    ok "systemd 用户单元已写入（已开 linger）"
    SVC_MODE="user"
  fi
}

load_service() {
  step "启动服务"

  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  %s[dry-run]%s bootout + bootstrap %s\n' "$C_D" "$C_0" "$LABEL_GW"
    return 0
  fi

  if [ "$OS" = "darwin" ]; then
    launchctl bootout "gui/$(id -u)/$LABEL_GW" 2>/dev/null || true
    launchctl bootout "gui/$(id -u)/$LABEL_WD" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$AGENT_DIR/$LABEL_GW.plist" 2>/dev/null \
      || launchctl load -w "$AGENT_DIR/$LABEL_GW.plist" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$AGENT_DIR/$LABEL_WD.plist" 2>/dev/null \
      || launchctl load -w "$AGENT_DIR/$LABEL_WD.plist" 2>/dev/null || true
    # 若端口被别的手工进程占着，launchd 起不来 —— 提前提示
    local holder
    holder=$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true)
    if [ -n "$holder" ]; then
      warn "端口 $PORT 已被 PID $holder 占用"
      dim "  如果那是之前手工启动的网关，执行：kill $holder 然后 launchctl kickstart -k gui/$(id -u)/$LABEL_GW"
    fi
  else
    if [ "${SVC_MODE:-user}" = "system" ]; then
      systemctl enable --now cliproxyapi.service
      systemctl enable --now cliproxyapi-watchdog.timer
    else
      systemctl --user enable --now cliproxyapi.service
      systemctl --user enable --now cliproxyapi-watchdog.timer
    fi
  fi
  ok "服务已装入"
}

wait_ready() {
  step "等待网关就绪"

  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  %s[dry-run]%s 轮询 http://127.0.0.1:%s/v1/models\n' "$C_D" "$C_0" "$PORT"
    return 0
  fi

  local i code
  for i in $(seq 1 40); do
    code=$(curl -s -o /dev/null -w '%{http_code}' -m 3 "http://127.0.0.1:$PORT/v1/models" 2>/dev/null) || code="000"
    if [ "$code" = "200" ]; then
      ok "网关已就绪（${i}00ms 内响应）"
      return 0
    fi
    sleep 0.5
  done

  warn "20 秒内没等到 200（最后状态码：${code}）"
  dim "  常见原因："
  dim "    • 端口被旧进程占着 —— lsof -iTCP:$PORT -sTCP:LISTEN"
  dim "    • 配置有误 —— tail -50 $GW_DIR/logs/err.log"
  dim "    • 还没登录任何账号（此时 /v1/models 也应返回 200，若为 000 则是进程没起）"
  return 1
}

# ==============================================================================
#  watchdog.sh（自愈巡检）
# ==============================================================================

install_watchdog_script() {
  step "安装 watchdog（每 5 分钟自检）"

  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  %s[dry-run]%s 写 %s/watchdog.sh\n' "$C_D" "$C_0" "$GW_DIR"
    return 0
  fi

  cat > "$GW_DIR/watchdog.sh" <<WDEOF
#!/usr/bin/env bash
# Claude Gateway watchdog —— 由 install.sh 生成
# 分类处置：端口没了→重启；200→清计数；401/403→只告警；429→只记录；超时/5xx 连 3 次→重启
set -uo pipefail

GW_DIR="\${CG_DIR:-\$(cd "\$(dirname "\$0")" && pwd)}"
PORT="\${CG_PORT:-$PORT}"
STATE_DIR="\$GW_DIR/.state"
LOG="\$GW_DIR/logs/watchdog.log"
SERVICE_LABEL="$LABEL_GW"
SVC_MODE="${SVC_MODE:-launchd}"
WATCHDOG_KEY="\${CG_WATCHDOG_KEY:-}"
if [ -z "\$WATCHDOG_KEY" ] && [ -f "\$GW_DIR/config.yaml" ]; then
  WATCHDOG_KEY=\$(sed -nE "s/^[[:space:]]*-[[:space:]]*[\"']?(sk-[^\"'#[:space:]]+).*/\1/p" "\$GW_DIR/config.yaml" | head -n 1)
fi

mkdir -p "\$STATE_DIR" "\$GW_DIR/logs"

log() { printf '[%s] %s\n' "\$(date '+%Y-%m-%d %H:%M:%S')" "\$*" >> "\$LOG"; }

notify() {
  local stamp="\$STATE_DIR/last_notify" now last
  now=\$(date +%s); last=0
  [ -f "\$stamp" ] && last=\$(cat "\$stamp" 2>/dev/null || echo 0)
  [ \$((now - last)) -lt 3600 ] && return 0
  printf '%s' "\$now" > "\$stamp"
  if [ "\$(uname -s)" = "Darwin" ]; then
    osascript -e "display notification \\"\$1\\" with title \\"Claude Gateway\\"" 2>/dev/null || true
  elif command -v notify-send >/dev/null 2>&1; then
    notify-send "Claude Gateway" "\$1" 2>/dev/null || true
  fi
}

perm_of() {
  if [ "\$(uname -s)" = "Darwin" ]; then stat -f '%Lp' "\$1" 2>/dev/null; else stat -c '%a' "\$1" 2>/dev/null; fi
}

# ---------- 0. 权限兜底 ----------
check_perms() {
  local changed=""
  if [ -d "\$GW_DIR/auth" ] && [ "\$(perm_of "\$GW_DIR/auth")" != "700" ]; then
    chmod 700 "\$GW_DIR/auth" 2>/dev/null && changed="\$changed auth-dir→700"
  fi
  local f
  for f in "\$GW_DIR"/auth/*.json; do
    [ -e "\$f" ] || continue
    [ "\$(perm_of "\$f")" != "600" ] && chmod 600 "\$f" 2>/dev/null && changed="\$changed \$(basename "\$f")→600"
  done
  for f in "\$GW_DIR/config.yaml" "\$GW_DIR/.management-secret" "\$GW_DIR/backup.sh"; do
    [ -e "\$f" ] || continue
    local want=600
    [ "\$(basename "\$f")" = "backup.sh" ] && want=700
    [ "\$(perm_of "\$f")" != "\$want" ] && chmod "\$want" "\$f" 2>/dev/null && changed="\$changed \$(basename "\$f")→\$want"
  done
  [ -n "\$changed" ] && log "PERM-FIX:\$changed"
  return 0
}

# ---------- 0.5 每日加密备份 ----------
run_backup() {
  [ -x "\$GW_DIR/backup.sh" ] || return 0
  bash "\$GW_DIR/backup.sh" >/dev/null 2>&1 || log "WARN: backup.sh 执行异常"
}

# ---------- HTTP 探活 ----------
probe_models() {
  local c
  if [ -n "\${WATCHDOG_KEY:-}" ]; then
    c=\$(curl --noproxy '*' -s -o /dev/null -w '%{http_code}' -m 10 \
      -H "Authorization: Bearer \$WATCHDOG_KEY" \
      "http://127.0.0.1:\$PORT/v1/models" 2>/dev/null) || c="000"
  else
    c=\$(curl --noproxy '*' -s -o /dev/null -w '%{http_code}' -m 10 \
      "http://127.0.0.1:\$PORT/v1/models" 2>/dev/null) || c="000"
  fi
  printf '%s' "\${c:-000}"
}

# ---------- 重启 ----------
restart_service() {
  log "RESTART: 尝试重启 \$SERVICE_LABEL"
  if [ "\$SVC_MODE" = "launchd" ]; then
    launchctl kickstart -k "gui/\$(id -u)/\$SERVICE_LABEL" 2>/dev/null
  elif [ "\$SVC_MODE" = "system" ]; then
    systemctl restart cliproxyapi 2>/dev/null
  else
    systemctl --user restart cliproxyapi 2>/dev/null
  fi
  local i=0
  while [ \$i -lt 10 ]; do
    if lsof -iTCP:"\$PORT" -sTCP:LISTEN >/dev/null 2>&1 || ss -lntH 2>/dev/null | grep -q ":\$PORT "; then
      sleep 1
      return 0
    fi
    sleep 1
    i=\$((i + 1))
  done
}

check_perms
run_backup

# ---------- 1. 端口在听吗 ----------
if ! lsof -iTCP:"\$PORT" -sTCP:LISTEN >/dev/null 2>&1 && ! ss -lntH 2>/dev/null | grep -q ":\$PORT "; then
  log "DOWN: 端口 \$PORT 没有监听"
  restart_service
  if lsof -iTCP:"\$PORT" -sTCP:LISTEN >/dev/null 2>&1 || ss -lntH 2>/dev/null | grep -q ":\$PORT "; then
    log "RECOVERED: 端口已恢复监听"
    notify "网关曾停止，已自动拉起"
  else
    log "FAILED: 重启后端口仍无监听"
    notify "网关无法启动，请检查日志"
  fi
  exit 0
fi

# ---------- 2. HTTP 健康 ----------
CODE=\$(probe_models)

case "\$CODE" in
  200)
    rm -f "\$STATE_DIR/fails"
    log "OK: 200"
    ;;
  401|403)
    log "AUTH-FAIL: \${CODE}（OAuth 可能已失效，重启无用，需重新登录）"
    notify "登录已失效，需要重新授权"
    ;;
  429)
    log "RATE-LIMIT: 429（撞窗口限额，重启会更糟，跳过）"
    ;;
  *)
    N=0
    [ -f "\$STATE_DIR/fails" ] && N=\$(cat "\$STATE_DIR/fails" 2>/dev/null || echo 0)
    N=\$((N + 1))
    printf '%s' "\$N" > "\$STATE_DIR/fails"
    log "UNHEALTHY: \${CODE}（连续第 \$N 次）"
    if [ "\$N" -ge 3 ]; then
      restart_service
      CODE2=\$(probe_models)
      log "AFTER-RESTART: \$CODE2"
      [ "\$CODE2" = "200" ] && { rm -f "\$STATE_DIR/fails"; notify "网关曾无响应，已重启恢复"; }
    fi
    ;;
esac

exit 0
WDEOF

  chmod 700 "$GW_DIR/watchdog.sh"
  bash -n "$GW_DIR/watchdog.sh" || die "生成的 watchdog.sh 语法有误"
  ok "watchdog.sh 已生成（700）"
}

# ==============================================================================
#  backup.sh（加密备份）
# ==============================================================================

install_backup_script() {
  step "安装加密备份（每天一次，留 7 份）"

  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  %s[dry-run]%s 写 %s/backup.sh\n' "$C_D" "$C_0" "$GW_DIR"
    return 0
  fi

  run mkdir -p "$BK_DIR"
  run chmod 700 "$BK_DIR"

  cat > "$GW_DIR/backup.sh" <<BKEOF
#!/usr/bin/env bash
# Claude Gateway 加密备份 —— 由 install.sh 生成
set -uo pipefail

GW_DIR="\${CG_DIR:-\$(cd "\$(dirname "\$0")" && pwd)}"
BK_DIR="\${CG_BACKUP_DIR:-$BK_DIR}"
KEEP=7
ITER=200000
KS="$KEYCHAIN_SVC"
ACC="\$(id -un)"
LOG="\$GW_DIR/logs/backup.log"
STAMP="\$GW_DIR/.state/last_backup"

mkdir -p "\$BK_DIR" "\$GW_DIR/.state" "\$GW_DIR/logs"
chmod 700 "\$BK_DIR" 2>/dev/null || true

log() { printf '[%s] %s\n' "\$(date '+%Y-%m-%d %H:%M:%S')" "\$*" >> "\$LOG"; }

TODAY=\$(date +%F)
if [ -f "\$STAMP" ] && [ "\$(cat "\$STAMP" 2>/dev/null)" = "\$TODAY" ]; then
  exit 0
fi

# 取口令
PASS=""
if command -v security >/dev/null 2>&1; then
  PASS=\$(security find-generic-password -s "\$KS" -a "\$ACC" -w 2>/dev/null) || PASS=""
fi
if [ -z "\$PASS" ] && [ -f "\$GW_DIR/.backup-pass" ]; then
  PASS=\$(cat "\$GW_DIR/.backup-pass" 2>/dev/null)
fi
if [ -z "\$PASS" ]; then
  log "SKIP: 找不到备份口令（钥匙串 service=\$KS account=\${ACC}）"
  exit 1
fi

# 打包
TMP=\$(mktemp "\${TMPDIR:-/tmp}/cg-bk.XXXXXX")
trap 'rm -f "\$TMP"' EXIT
tar czf "\$TMP" -C "\$GW_DIR" auth config.yaml 2>/dev/null || { log "FAIL: 打包失败"; exit 1; }

OUT="\$BK_DIR/auth-\$TODAY.enc"
printf '%s\n' "\$PASS" | openssl enc -aes-256-cbc -pbkdf2 -iter "\$ITER" -salt -pass stdin \
  -in "\$TMP" -out "\$OUT" 2>/dev/null || { log "FAIL: 加密失败"; exit 1; }
chmod 600 "\$OUT"

printf '%s' "\$TODAY" > "\$STAMP"

# 轮转
ls -1t "\$BK_DIR"/auth-*.enc 2>/dev/null | tail -n +\$((KEEP + 1)) | while IFS= read -r old; do
  rm -f "\$old"
  log "PRUNE: \$(basename "\$old")"
done

log "OK: \$(basename "\$OUT") \$(du -h "\$OUT" 2>/dev/null | awk '{print \$1}')"
exit 0
BKEOF

  chmod 700 "$GW_DIR/backup.sh"
  bash -n "$GW_DIR/backup.sh" || die "生成的 backup.sh 语法有误"
  ok "backup.sh 已生成（700）"

  # 口令
  if [ "$SECRET_BACKEND" = "keychain" ]; then
    if security find-generic-password -s "$KEYCHAIN_SVC" -a "$(id -un)" >/dev/null 2>&1; then
      ok "备份口令已在钥匙串中，沿用"
    else
      local pass
      pass=$(openssl rand -base64 32 | tr -d '\n')
      security add-generic-password -s "$KEYCHAIN_SVC" -a "$(id -un)" -w "$pass" >/dev/null 2>&1 \
        && ok "备份口令已存入钥匙串" \
        || warn "钥匙串写入失败"
    fi
  else
    if [ ! -f "$GW_DIR/.backup-pass" ]; then
      openssl rand -base64 32 | tr -d '\n' > "$GW_DIR/.backup-pass"
      chmod 600 "$GW_DIR/.backup-pass"
      ok "备份口令已写入 $GW_DIR/.backup-pass（600）"
    else
      ok "备份口令文件已存在，沿用"
    fi
  fi
}

# ==============================================================================
#  登录向导
# ==============================================================================

auth_count() {
  find "$GW_DIR/auth" -name '*.json' -type f 2>/dev/null | wc -l | tr -d ' '
}

open_url() {
  local url="$1"
  if [ "$OS" = "darwin" ]; then
    open "$url" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url" >/dev/null 2>&1 || true
  fi
}

login_claude() {
  say ""
  say "  ${C_W}Claude Max 登录${C_0}"
  say "  ────────────────────────────────────────────"
  say "  下一步会显示一个授权链接，在浏览器里点 Authorize 即可。"
  say "  浏览器必须是这台机器上的（或你能操作这台机器浏览器）。"
  say ""

  if [ ! -t 0 ]; then
    warn "当前没有可交互的终端，无法自动走完授权流程"
    say ""
    say "  请你手动执行下面这一行（必须在终端里，不能在管道里）："
    say ""
    say "    ${C_B}cd $GW_DIR && ./cli-proxy-api -config config.yaml -claude-login${C_0}"
    say ""
    return 0
  fi

  local before after
  before=$(auth_count)
  ( cd "$GW_DIR" && ./cli-proxy-api -config config.yaml -claude-login ) || true
  after=$(auth_count)

  if [ "$after" -gt "$before" ]; then
    ok "Claude 登录成功（auth 目录现有 $after 份凭证）"
  else
    warn "没有检测到新凭证 —— 授权可能没走完"
    say "  可稍后重试：cd $GW_DIR && ./cli-proxy-api -config config.yaml -claude-login"
  fi
}

# device code 类：codex / xai
login_device() {
  local flag="$1" name="$2" url_hint="$3"
  local out="$GW_DIR/logs/login-$name.log"

  say ""
  say "  ${C_W}$name 登录（设备码）${C_0}"
  say "  ────────────────────────────────────────────"

  local before
  before=$(auth_count)

  : > "$out"
  ( cd "$GW_DIR" && exec ./cli-proxy-api -config config.yaml "$flag" ) >"$out" 2>&1 &
  local pid=$!

  # 等第一行 URL 出现
  local i
  for i in $(seq 1 40); do
    grep -qE 'https?://' "$out" 2>/dev/null && break
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.5
  done

  local url code
  url=$(grep -oE 'https?://[^[:space:]]+' "$out" 2>/dev/null | head -1 || true)
  code=$(grep -oE '[A-Z0-9]{4}-[A-Z0-9]{5,6}' "$out" 2>/dev/null | head -1 || true)

  if [ -z "$url" ]; then
    warn "没能启动登录流程，输出如下："
    sed -n '1,15p' "$out" | sed 's/^/    /'
    kill "$pid" 2>/dev/null || true
    return 0
  fi

  say ""
  say "    1. 打开：${C_B}$url${C_0}"
  if [ -n "$code" ]; then
    say "    2. 输入码：${C_W}${C_G}$code${C_0}"
  fi
  say "    3. 确认授权后，本程序会自动继续"
  say ""

  open_url "$url"

  # 有 pbcopy 就顺手复制
  if [ -n "$code" ] && command -v pbcopy >/dev/null 2>&1; then
    printf '%s' "$code" | pbcopy 2>/dev/null && dim "  （授权码已复制到剪贴板）"
  fi

  say "  等待授权…（最多 5 分钟，按 Ctrl-C 可跳过）"
  local waited=0
  while [ "$waited" -lt 300 ]; do
    if ! kill -0 "$pid" 2>/dev/null; then break; fi
    if [ "$(auth_count)" -gt "$before" ]; then break; fi
    sleep 2
    waited=$((waited + 2))
    if [ $((waited % 20)) -eq 0 ]; then printf '.'; fi
  done
  printf '\n'

  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true

  if [ "$(auth_count)" -gt "$before" ]; then
    ok "$name 登录成功"
  else
    warn "$name 未完成授权（可稍后重跑：cd $GW_DIR && ./cli-proxy-api -config config.yaml ${flag}）"
  fi
}

do_logins() {
  if [ "$DRY_RUN" -eq 1 ]; then
    return 0
  fi
  step "登录账号"

  case " $PROVIDERS " in
    *" claude "*) login_claude ;;
  esac
  case " $PROVIDERS " in
    *" codex "*) login_device "-codex-device-login" "Codex / ChatGPT" "" ;;
  esac
  case " $PROVIDERS " in
    *" xai "*)   login_device "-xai-login" "Grok / xAI" "" ;;
  esac

  # 登录后重启，让新凭证生效
  if [ "$DRY_RUN" -eq 0 ]; then
    if [ "$OS" = "darwin" ]; then
      launchctl kickstart -k "gui/$(id -u)/$LABEL_GW" 2>/dev/null || true
    elif [ "${SVC_MODE:-user}" = "system" ]; then
      systemctl restart cliproxyapi 2>/dev/null || true
    else
      systemctl --user restart cliproxyapi 2>/dev/null || true
    fi
    sleep 3
    ok "已重启网关使凭证生效"
  fi
}

# ==============================================================================
#  汇总
# ==============================================================================

LAN_IP=""
detect_lan_ip() {
  if [ "$OS" = "darwin" ]; then LAN_IP=$(ipconfig getifaddr en0 2>/dev/null || echo ""); fi
  if [ -z "$LAN_IP" ] && command -v hostname >/dev/null 2>&1; then
    LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
  fi
}

print_summary() {
  detect_lan_ip

  local base_local="http://127.0.0.1:$PORT"
  local base_lan=""
  if [ -n "$LAN_IP" ]; then base_lan="http://$LAN_IP:$PORT"; fi

  say ""
  say "${C_G}══════════════════════════════════════════════════${C_0}"
  say "${C_W}  装好了${C_0}"
  say "${C_G}══════════════════════════════════════════════════${C_0}"
  say ""
  say "  ${C_W}网关地址${C_0}"
  say "    本机      $base_local"
  if [ "$NET_MODE" = "local" ]; then
    say "    局域网    （未开启，配置里 host=127.0.0.1）"
  elif [ -n "$base_lan" ]; then
    say "    局域网    $base_lan"
  else
    say "    局域网    （没探测到本机 IP，用 ifconfig 自己看）"
  fi
  say ""
  say "  ${C_W}客户端 Key${C_0}（一台设备一把，泄露只吊销那一把）"
  local i=1 k
  for k in ${GEN_KEYS[@]+"${GEN_KEYS[@]}"}; do
    say "    设备$i      $k"
    i=$((i + 1))
  done
  say ""
  say "  ${C_W}控制台（可视化面板）${C_0}"
  say "    http://127.0.0.1:$UI_PORT/"
  say "    改配置、加账号、测模型、备份、重启网关都在这里"
  say ""
  say "  ${C_W}官方管理面板${C_0}"
  say "    $base_local/v8/management"
  say "    口令      $(cat "$SECRET_FILE" 2>/dev/null || echo '（见 config.yaml）')"
  say "    口令文件  $SECRET_FILE"
  say ""
  say "  ${C_W}各客户端怎么填${C_0}"
  say "    Claude Code / dsh（Anthropic 协议，地址不带 /v1）"
  say "      Base URL  $base_local"
  say ""
  say "    Chatbox / Cherry / Cline / Trae（OpenAI 协议，地址带 /v1）"
  say "      Base URL  ${base_lan:-$base_local}/v1"
  say ""
  say "    模型填       opus / sonnet / haiku（或 codex / grok）"
  say "    最大回复     至少 2000，建议 4000（Opus 会先思考，思考也算 token）"
  say ""
  say "  ${C_W}日常命令${C_0}"
  say "    看状态      bash $GW_DIR/cg-status"
  say "    看模型      bash $GW_DIR/cg-models"
  say "    体检        bash $GW_DIR/cg-doctor"
  say "    立即备份    bash $GW_DIR/backup.sh"
  say ""
  say "  ${C_W}文件在哪${C_0}"
  say "    网关目录    $GW_DIR"
  say "    配置        $CONFIG"
  say "    凭证        $GW_DIR/auth/"
  say "    日志        $GW_DIR/logs/"
  say "    备份        $BK_DIR"
  say "    手册        $GW_DIR/QUICKSTART.md"
  say ""
  if [ "$NET_MODE" = "tailscale" ]; then
    say "  ${C_Y}还要一步：装 Tailscale${C_0}"
    say "    1. 这台机器和要连的设备都装 Tailscale，登同一账号"
    say "    2. 在 Tailscale 后台关掉 key expiry（否则 180 天后失联）"
    say "    3. 用 tailscale ip -4 拿 100.x.y.z，客户端地址改成 http://100.x.y.z:$PORT"
    say ""
  fi
  say "${C_G}══════════════════════════════════════════════════${C_0}"
  say ""
}

# ==============================================================================
#  辅助工具：cg-status / cg-models / cg-doctor
# ==============================================================================

install_helpers() {
  step "安装辅助命令"

  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  %s[dry-run]%s 写 cg-status / cg-models / cg-doctor\n' "$C_D" "$C_0"
    return 0
  fi

  # ---- cg-status ----
  cat > "$GW_DIR/cg-status" <<'STEOF'
#!/usr/bin/env bash
set -uo pipefail
GW_DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${CG_PORT:-PORT_PLACEHOLDER}"
SVC="LABEL_PLACEHOLDER"

echo "网关目录  $GW_DIR"
echo "端口      $PORT"
echo

echo "— 服务 —"
if [ "$(uname -s)" = "Darwin" ]; then
  launchctl list 2>/dev/null | grep -i "$SVC" || echo "  未注册到 launchd"
else
  systemctl --user is-active cliproxyapi 2>/dev/null || systemctl is-active cliproxyapi 2>/dev/null || echo "  unknown"
fi

echo
echo "— 监听 —"
lsof -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | tail -n +1 || echo "  没有进程在监听 $PORT"

echo
echo "— 健康 —"
CODE=$(curl -s -o /dev/null -w '%{http_code} %{time_total}s' -m 10 "http://127.0.0.1:$PORT/v1/models" 2>/dev/null) || CODE="000"
echo "  HTTP $CODE"

echo
echo "— 凭证 —"
found=0
for f in "$GW_DIR"/auth/*.json; do
  [ -e "$f" ] || continue
  found=1
  exp=$(grep -oE '"expired"[[:space:]]*:[[:space:]]*"[^"]*"' "$f" 2>/dev/null | sed 's/.*"\([^"]*\)"$/\1/')
  em=$(grep -oE '"email"[[:space:]]*:[[:space:]]*"[^"]*"' "$f" 2>/dev/null | sed 's/.*"\([^"]*\)"$/\1/')
  dis=$(grep -oE '"disabled"[[:space:]]*:[[:space:]]*(true|false)' "$f" 2>/dev/null | awk '{print $2}')
  echo "  $(basename "$f")"
  echo "    账号   ${em:-?}"
  echo "    过期   ${exp:-?}"
  echo "    禁用   ${dis:-false}"
done
[ "$found" -eq 0 ] && echo "  没有凭证，先登录：cd $GW_DIR && ./cli-proxy-api -config config.yaml -claude-login"

echo
echo "— 备份 —"
ls -1t "$HOME/claude-gateway-backups"/*.enc 2>/dev/null | head -3 || echo "  还没有备份"

echo
echo "— 最近日志 —"
tail -5 "$GW_DIR/logs/watchdog.log" 2>/dev/null || echo "  （无 watchdog 日志）"
STEOF

  # ---- cg-models ----
  cat > "$GW_DIR/cg-models" <<'MDEOF'
#!/usr/bin/env bash
set -uo pipefail
PORT="${CG_PORT:-PORT_PLACEHOLDER}"
KEY="${1:-}"

if [ -z "$KEY" ]; then
  GW_DIR="$(cd "$(dirname "$0")" && pwd)"
  KEY=$(sed -n '/^access:/,/^[a-z]/p' "$GW_DIR/config.yaml" 2>/dev/null \
        | grep -oE 'sk-local-[A-Za-z0-9_-]+' | head -1)
fi
[ -z "$KEY" ] && { echo "找不到 Key，用法：$0 sk-local-xxxx"; exit 1; }

curl -s -m 20 -H "Authorization: Bearer $KEY" "http://127.0.0.1:$PORT/v1/models" \
  | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin)
except Exception as e:
    print("解析失败:", e); sys.exit(1)
for m in d.get("data", []):
    print("  " + m.get("id",""))' 2>/dev/null \
  || curl -s -m 20 -H "Authorization: Bearer $KEY" "http://127.0.0.1:$PORT/v1/models"
MDEOF

  # ---- cg-doctor ----
  cat > "$GW_DIR/cg-doctor" <<'DREOF'
#!/usr/bin/env bash
set -uo pipefail
GW_DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${CG_PORT:-PORT_PLACEHOLDER}"
PASS=0; FAIL=0; WARN=0

ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; WARN=$((WARN+1)); }

echo "Claude Gateway 体检"
echo "════════════════════"

echo
echo "1. 文件"
[ -x "$GW_DIR/cli-proxy-api" ] && ok "二进制可执行" || bad "二进制缺失或不可执行"
[ -f "$GW_DIR/config.yaml" ] && ok "config.yaml 存在" || bad "config.yaml 缺失"
grep -q '^config-version:' "$GW_DIR/config.yaml" 2>/dev/null && ok "配置版本字段存在" || warn "配置缺 config-version（可能是老格式）"

echo
echo "2. 权限"
p=$(stat -f '%Lp' "$GW_DIR/auth" 2>/dev/null || stat -c '%a' "$GW_DIR/auth" 2>/dev/null)
[ "$p" = "700" ] && ok "auth 目录 700" || warn "auth 目录是 ${p}（应为 700）"
p=$(stat -f '%Lp' "$GW_DIR/config.yaml" 2>/dev/null || stat -c '%a' "$GW_DIR/config.yaml" 2>/dev/null)
[ "$p" = "600" ] && ok "config.yaml 600" || warn "config.yaml 是 ${p}（应为 600）"

echo
echo "3. 服务与端口"
if [ "$(uname -s)" = "Darwin" ]; then
  launchctl list 2>/dev/null | grep -q "LABEL_PLACEHOLDER" && ok "launchd 已注册" || bad "launchd 未注册"
else
  (systemctl --user is-active cliproxyapi 2>/dev/null || systemctl is-active cliproxyapi 2>/dev/null) | grep -q '^active' \
    && ok "systemd 运行中" || bad "systemd 未运行"
fi
lsof -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 && ok "端口 $PORT 在监听" || bad "端口 $PORT 无人监听"

echo
echo "4. HTTP"
c=$(curl -s -o /dev/null -w '%{http_code}' -m 10 "http://127.0.0.1:$PORT/v1/models" 2>/dev/null) || c="000"
[ "$c" = "200" ] && ok "/v1/models → 200" || bad "/v1/models → $c"

echo
echo "5. 凭证"
n=$(find "$GW_DIR/auth" -name '*.json' -type f 2>/dev/null | wc -l | tr -d ' ')
[ "$n" -gt 0 ] && ok "$n 份凭证" || bad "没有凭证，先登录"
for f in "$GW_DIR"/auth/*.json; do
  [ -e "$f" ] || continue
  d=$(grep -oE '"disabled"[[:space:]]*:[[:space:]]*true' "$f" 2>/dev/null)
  [ -n "$d" ] && warn "$(basename "$f") 标记为 disabled"
done

echo
echo "6. 出站代理"
proxy=$(grep -E '^[[:space:]]*proxy-url:' "$GW_DIR/config.yaml" 2>/dev/null | sed -E 's/.*proxy-url:[[:space:]]*"?([^"]*)"?.*/\1/')
if [ -z "$proxy" ]; then
  ok "配置为直连"
  curl -s -o /dev/null -m 10 https://api.anthropic.com/v1/models 2>/dev/null \
    && ok "直连 api.anthropic.com 可达" || warn "直连 api.anthropic.com 不通"
else
  ok "配置代理 $proxy"
  curl -s -o /dev/null -w '' -m 10 -x "$proxy" https://api.anthropic.com/v1/models 2>/dev/null \
    && ok "经代理可达 api.anthropic.com" || bad "经代理仍不通"
fi

echo
echo "7. 备份"
if ls "$HOME/claude-gateway-backups"/*.enc >/dev/null 2>&1; then
  newest=$(ls -1t "$HOME/claude-gateway-backups"/*.enc | head -1)
  age=$(( ( $(date +%s) - $(stat -f %m "$newest" 2>/dev/null || stat -c %Y "$newest" 2>/dev/null) ) / 86400 ))
  [ "$age" -le 1 ] && ok "最新备份 $age 天前" || warn "最新备份 $age 天前（应每天一份）"
else
  warn "还没有备份"
fi

echo
echo "════════════════════"
printf '通过 %d  警告 %d  失败 %d\n' "$PASS" "$WARN" "$FAIL"
[ "$FAIL" -eq 0 ] && echo "结论：可用" || echo "结论：有 $FAIL 项需要修"
exit 0
DREOF

  for f in cg-status cg-models cg-doctor; do
    # 把占位符换成真实值
    sed -i.bak -e "s|PORT_PLACEHOLDER|$PORT|g" -e "s|LABEL_PLACEHOLDER|$LABEL_GW|g" "$GW_DIR/$f" 2>/dev/null \
      || sed -i '' -e "s|PORT_PLACEHOLDER|$PORT|g" -e "s|LABEL_PLACEHOLDER|$LABEL_GW|g" "$GW_DIR/$f"
    rm -f "$GW_DIR/$f.bak"
    chmod 700 "$GW_DIR/$f"
    bash -n "$GW_DIR/$f" || die "$f 语法有误"
  done
  ok "cg-status / cg-models / cg-doctor 已生成"
}

# ---------- 控制台（可视化面板） ----------
install_console() {
  step "控制台（可视化面板）"

  local src="$SRC_DIR/../console"
  # 也允许 install.sh 与 console/ 同级（分发时打成同一个包）
  if [ ! -f "$src/server.py" ]; then src="$SRC_DIR/console"; fi
  if [ ! -f "$src/server.py" ]; then
    warn "没有找到 console/server.py，跳过控制台"
    say "  可稍后手动：把 console/ 放进 $GW_DIR/console/ 再跑 cg-console"
    return 0
  fi

  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  %s[dry-run]%s 安装控制台 → %s/console（端口 %s）\n' "$C_D" "$C_0" "$GW_DIR" "$UI_PORT"
    return 0
  fi

  mkdir -p "$GW_DIR/console"
  cp "$src/server.py"   "$GW_DIR/console/server.py"
  cp "$src/index.html"  "$GW_DIR/console/index.html"
  [ -f "$src/README.md" ] && cp "$src/README.md" "$GW_DIR/console/README.md"
  chmod 600 "$GW_DIR/console/server.py" "$GW_DIR/console/index.html" 2>/dev/null || true
  [ -f "$GW_DIR/console/README.md" ] && chmod 600 "$GW_DIR/console/README.md"
  ok "控制台文件就位"

  # Python 检查
  local PY=""
  for c in /usr/local/bin/python3 /opt/homebrew/bin/python3 "$(command -v python3 2>/dev/null)"; do
    if [ -n "$c" ] && [ -x "$c" ]; then PY="$c"; break; fi
  done
  if [ -z "$PY" ]; then
    warn "没找到 python3，控制台需要它（macOS 装 Xcode 命令行工具即可：xcode-select --install）"
    return 0
  fi

  case "$OS" in
  darwin)
    local plist="$AGENT_DIR/$LABEL_UI.plist"
    cat > "$plist" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL_UI</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$GW_DIR/console/server.py</string>
  </array>
  <key>WorkingDirectory</key><string>$GW_DIR/console</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>Umask</key><integer>63</integer>
  <key>ProcessType</key><string>Standard</string>
  <key>StandardOutPath</key><string>$GW_DIR/logs/console.log</string>
  <key>StandardErrorPath</key><string>$GW_DIR/logs/console.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key><string>$HOME</string>
    <key>CG_DIR</key><string>$GW_DIR</string>
    <key>CG_UI_PORT</key><string>$UI_PORT</string>
    <key>CG_LABEL</key><string>$LABEL_GW</string>
    <key>CG_LABEL_WD</key><string>$LABEL_WD</string>
    <key>PYTHONDONTWRITEBYTECODE</key><string>1</string>
  </dict>
</dict>
</plist>
PLISTEOF
    chmod 600 "$plist"
    plutil -lint "$plist" >/dev/null 2>&1 || { warn "plist 校验失败"; return 0; }
    launchctl bootout "gui/$(id -u)/$LABEL_UI" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$plist" 2>/dev/null || true
    ;;
  linux)
    local unit="$HOME/.config/systemd/user/$LABEL_UI.service"
    mkdir -p "$(dirname "$unit")"
    cat > "$unit" <<UNITEOF
[Unit]
Description=Claude Gateway Console
After=network.target

[Service]
Type=simple
ExecStart=$PY $GW_DIR/console/server.py
WorkingDirectory=$GW_DIR/console
Environment=CG_DIR=$GW_DIR CG_UI_PORT=$UI_PORT CG_LABEL=$LABEL_GW CG_LABEL_WD=$LABEL_WD PYTHONDONTWRITEBYTECODE=1
Restart=always
RestartSec=5
UMask=0077

[Install]
WantedBy=default.target
UNITEOF
    systemctl --user daemon-reload 2>/dev/null || true
    systemctl --user enable --now "$LABEL_UI.service" 2>/dev/null || true
    ;;
  esac

  # 等它就位
  local n=0
  while [ "$n" -lt 15 ]; do
    if curl -s -o /dev/null -m 2 "http://127.0.0.1:$UI_PORT/" 2>/dev/null; then break; fi
    n=$((n + 1)); sleep 1
  done
  if curl -s -o /dev/null -m 2 "http://127.0.0.1:$UI_PORT/" 2>/dev/null; then
    ok "控制台运行中 → http://127.0.0.1:$UI_PORT/"
  else
    warn "控制台没起来，看 $GW_DIR/logs/console.log"
  fi
}

# 打开浏览器到控制台
open_console() {
  local url="http://127.0.0.1:$UI_PORT/"
  case "$OS" in
  darwin) open "$url" >/dev/null 2>&1 || true ;;
  *) python3 -c "import webbrowser,sys;webbrowser.open(sys.argv[1])" "$url" >/dev/null 2>&1 || true ;;
  esac
}


# ==============================================================================
#  QUICKSTART.md
# ==============================================================================

write_quickstart() {
  if [ "$DRY_RUN" -eq 1 ]; then
    return 0
  fi
  detect_lan_ip
  local base="http://${LAN_IP:-127.0.0.1}:$PORT"

  {
    cat <<EOF
# Claude Gateway 使用手册

> 由 install.sh v$SCRIPT_VERSION 生成于 $(date '+%Y-%m-%d %H:%M:%S')

## 地址

| 用途 | 地址 |
|---|---|
| 本机 | http://127.0.0.1:$PORT |
| 局域网 | ${base} |
| 管理面板 | http://127.0.0.1:$PORT/v8/management |

## 客户端 Key

| 设备 | Key |
|---|---|
EOF
    local i=1 k
    for k in ${GEN_KEYS[@]+"${GEN_KEYS[@]}"}; do
      printf '| 设备%d | `%s` |\n' "$i" "$k"
      i=$((i + 1))
    done
    cat <<EOF

一台设备一把。要吊销某一台，删掉 config.yaml 里对应那行然后重启服务。

## 各客户端怎么填

**关键：/v1 的位置不一样，填错会 404。**

| 客户端 | 协议 | Base URL | 模型 |
|---|---|---|---|
| Claude Code | Anthropic | \`http://127.0.0.1:$PORT\` | opus |
| dsh（自定义模型 API） | Anthropic Messages | \`http://${LAN_IP:-127.0.0.1}:$PORT\` | opus |
| Chatbox / Cherry Studio | OpenAI 兼容 | \`${base}/v1\` | opus |
| Cline / Roo / Continue | OpenAI 兼容 | \`${base}/v1\` | opus |
| Trae | OpenAI 兼容 | \`${base}/v1\` | opus |
| OpenAI Python SDK | OpenAI | \`${base}/v1\` | opus |
| Anthropic Python SDK | Anthropic | \`http://127.0.0.1:$PORT\` | opus |

**必改：最大回复长度设 4000。** 模型会先思考，思考也算 token；
设 30 会得到一个空回答，设 60 会截半句。

## 日常命令

\`\`\`bash
bash $GW_DIR/cg-status     # 服务/端口/凭证/备份状态
bash $GW_DIR/cg-models     # 列出可用模型
bash $GW_DIR/cg-doctor     # 全项体检
bash $GW_DIR/backup.sh     # 立刻备份一次
\`\`\`

## 开关与维护

\`\`\`bash
# macOS
launchctl kickstart -k gui/\$(id -u)/$LABEL_GW       # 重启
launchctl bootout gui/\$(id -u)/$LABEL_GW            # 停止
launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/$LABEL_GW.plist  # 启动

# Linux
systemctl --user restart cliproxyapi
systemctl --user stop cliproxyapi
\`\`\`

## 凭证与备份

- 凭证目录：\`$GW_DIR/auth/\`（700，文件 600）
- 备份：\`$BK_DIR\`（每天一份，留 7 份，AES-256 加密）
- 备份口令：macOS 在钥匙串 \`service=$KEYCHAIN_SVC\`，Linux 在 \`$GW_DIR/.backup-pass\`

**换机器前必须导出备份口令**，否则备份打不开：

\`\`\`bash
security find-generic-password -s $KEYCHAIN_SVC -a \$(id -un) -w
\`\`\`

## 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| 401 | Key 不对 / 凭证失效 | 核对 Key；\`cd $GW_DIR && ./cli-proxy-api -config config.yaml -claude-login\` |
| 404 | Base URL 的 /v1 位置错了 | Anthropic 不带 /v1，OpenAI 带 /v1 |
| 429 | 撞了 5 小时窗口限额 | 等窗口重置，或把 max-concurrent 调小 |
| 回答为空 | max_tokens 太小 | 设到 4000 |
| 首次很慢（15-30 秒） | TLS 冷启动 | 正常，之后 1.5-2.6 秒 |
| 一直连不上 | 代理没配 / 代理挂了 | \`bash $GW_DIR/cg-doctor\` |
| 模型自称 Claude Code | 上游指纹伪装的必然结果 | 正常，不影响能力 |

## 卸载

\`\`\`bash
bash $GW_DIR/uninstall.sh
\`\`\`
EOF
  } > "$GW_DIR/QUICKSTART.md"

  chmod 600 "$GW_DIR/QUICKSTART.md"
  ok "QUICKSTART.md 已生成"
}

# ==============================================================================
#  卸载
# ==============================================================================

do_uninstall() {
  step "卸载 Claude Gateway"

  say ""
  say "  将要移除："
  say "    • 服务 $LABEL_GW / $LABEL_WD / $LABEL_UI"
  say "    • 目录 $GW_DIR"
  if [ "$SECRET_BACKEND" = "keychain" ]; then say "    • 钥匙串口令 ${KEYCHAIN_SVC}（可选保留）"; fi
  say ""

  if [ "$ASSUME_YES" -eq 0 ] && [ -t 0 ]; then
    printf '  确认卸载？(y/N) '
    local c
    read -r c || c=""
    case "$c" in
      y|Y|yes|YES) ;;
      *) say "已取消"; exit 0 ;;
    esac
  fi

  # 1. 停服务
  if [ "$OS" = "darwin" ]; then
    launchctl bootout "gui/$(id -u)/$LABEL_UI" 2>/dev/null || true
    launchctl bootout "gui/$(id -u)/$LABEL_WD" 2>/dev/null || true
    launchctl bootout "gui/$(id -u)/$LABEL_GW" 2>/dev/null || true
    rm -f "$AGENT_DIR/$LABEL_GW.plist" "$AGENT_DIR/$LABEL_WD.plist" "$AGENT_DIR/$LABEL_UI.plist"
    ok "launchd 服务已移除"
  else
    if [ "${SVC_MODE:-user}" = "system" ]; then
      systemctl --user disable --now "$LABEL_UI.service" 2>/dev/null || true
      rm -f "$HOME/.config/systemd/user/$LABEL_UI.service" 2>/dev/null || true
      systemctl disable --now cliproxyapi-watchdog.timer 2>/dev/null || true
      systemctl disable --now cliproxyapi.service 2>/dev/null || true
      rm -f /etc/systemd/system/cliproxyapi*.service /etc/systemd/system/cliproxyapi-watchdog.timer
      systemctl daemon-reload 2>/dev/null || true
    else
      systemctl --user disable --now cliproxyapi-watchdog.timer 2>/dev/null || true
      systemctl --user disable --now cliproxyapi.service 2>/dev/null || true
      rm -f "$HOME/.config/systemd/user/cliproxyapi"*.service "$HOME/.config/systemd/user/cliproxyapi-watchdog.timer"
      systemctl --user daemon-reload 2>/dev/null || true
    fi
    ok "systemd 单元已移除"
  fi

  # 2. 备份保留？默认保留
  if [ -d "$BK_DIR" ]; then
    if [ "$ASSUME_YES" -eq 0 ] && [ -t 0 ]; then
      printf '  保留加密备份 %s？(Y/n) ' "$BK_DIR"
      local k
      read -r k || k=""
      case "$k" in
        n|N|no|NO) rm -rf "$BK_DIR"; ok "备份已删除" ;;
        *) ok "备份保留在 $BK_DIR" ;;
      esac
    else
      ok "备份保留在 $BK_DIR"
    fi
  fi

  # 3. 网关目录
  rm -rf "$GW_DIR"
  ok "已删除 $GW_DIR"

  # 4. 钥匙串
  if [ "$SECRET_BACKEND" = "keychain" ] && security find-generic-password -s "$KEYCHAIN_SVC" -a "$(id -un)" >/dev/null 2>&1; then
    if [ "$ASSUME_YES" -eq 0 ] && [ -t 0 ]; then
      printf '  删除钥匙串里的备份口令？(y/N) '
      local s
      read -r s || s=""
      case "$s" in
        y|Y) security delete-generic-password -s "$KEYCHAIN_SVC" -a "$(id -un)" >/dev/null 2>&1 && ok "口令已删除" ;;
        *) ok "口令保留（备份仍可解密）" ;;
      esac
    fi
  fi

  say ""
  ok "卸载完成，系统里没有残留"
  say ""
}

# ==============================================================================
#  主流程
# ==============================================================================

banner() {
  say ""
  say "${C_B}  ┌─────────────────────────────────────────┐${C_0}"
  say "${C_B}  │${C_0}  ${C_W}Claude Gateway${C_0}  一键安装器 v$SCRIPT_VERSION   ${C_B}│${C_0}"
  say "${C_B}  │${C_0}  订阅额度 → 本地 API 端点               ${C_B}│${C_0}"
  say "${C_B}  └─────────────────────────────────────────┘${C_0}"
  say ""
}

usage() {
  cat <<EOF
Claude Gateway 一键安装器 v$SCRIPT_VERSION

用法：
  bash install.sh              交互安装
  bash install.sh --dry-run    只打印将做什么，不改动系统
  bash install.sh --yes        全默认，不提问
  bash install.sh --no-login   装好但不登录（之后手动跑登录命令）
  bash install.sh --uninstall  卸载
  bash install.sh --help       本帮助

环境变量：
  CG_DIR           网关目录（默认 \$HOME/claude-gateway）
  CG_BACKUP_DIR    备份目录（默认 \$HOME/claude-gateway-backups）
  CG_PORT          端口（默认 8317）
  CG_LABEL         服务标识（默认 com.local.cliproxyapi）
  CG_AGENT_DIR     LaunchAgents 目录（默认 ~/Library/LaunchAgents）
  CG_KEYCHAIN_SVC  钥匙串服务名（默认 claude-gateway-backup）

装完之后：
  bash ~/claude-gateway/cg-status    看状态
  bash ~/claude-gateway/cg-doctor    体检
EOF
}

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --dry-run)   DRY_RUN=1 ;;
      --yes|-y)    ASSUME_YES=1 ;;
      --no-login)  SKIP_LOGIN=1 ;;
      --uninstall) DO_UNINSTALL=1 ;;
      --help|-h)   usage; exit 0 ;;
      --version)   say "$SCRIPT_VERSION"; exit 0 ;;
      *) die "未知参数：$1（用 --help 看用法）" ;;
    esac
    shift
  done
}

pick_test_url() {
  case " $PROVIDERS " in
    *" claude "*) printf 'https://api.anthropic.com/v1/models' ;;
    *" codex "*)  printf 'https://chatgpt.com/backend-api/models' ;;
    *" xai "*)    printf 'https://api.x.ai/v1/models' ;;
    *)            printf 'https://api.anthropic.com/v1/models' ;;
  esac
}

main() {
  # 兼容 curl | bash：把 stdin 接回终端，否则后续交互全失效
  if [ ! -t 0 ] && [ -c /dev/tty ] && { : </dev/tty; } 2>/dev/null; then
    exec </dev/tty
  fi

  parse_args "$@"
  banner

  detect_platform
  detect_tools

  if [ "$DO_UNINSTALL" -eq 1 ]; then
    do_uninstall
    exit 0
  fi

  if [ "$DRY_RUN" -eq 1 ]; then
    say "${C_Y}  DRY-RUN：只打印，不改动系统${C_0}"
  fi

  ask_providers
  ask_network
  ask_devices

  detect_outbound "$(pick_test_url)"

  fetch_latest
  download_release
  install_binary
  gen_config

  if [ "$OS" = "darwin" ]; then
    step "写入 launchd 配置"
    write_launchd_plists
    SVC_MODE="launchd"
  else
    step "写入 systemd 配置"
    write_systemd_units
  fi

  install_watchdog_script
  install_backup_script
  install_helpers
  install_console
  if [ "$DRY_RUN" -eq 0 ]; then
    write_quickstart
  else
    printf '  %s[dry-run]%s 写 %s/QUICKSTART.md\n' "$C_D" "$C_0" "$GW_DIR"
  fi

  # uninstall.sh 一起放好，方便以后卸
  if [ "$DRY_RUN" -eq 0 ] && [ -f "${BASH_SOURCE[0]%/*}/install.sh" ]; then
    cp "${BASH_SOURCE[0]%/*}/install.sh" "$GW_DIR/install.sh" 2>/dev/null || true
    chmod 700 "$GW_DIR/install.sh" 2>/dev/null || true
    cat > "$GW_DIR/uninstall.sh" <<'UNEOF'
#!/usr/bin/env bash
# 卸载 Claude Gateway
exec bash "$(cd "$(dirname "$0")" && pwd)/install.sh" --uninstall "$@"
UNEOF
    chmod 700 "$GW_DIR/uninstall.sh"
  fi

  load_service
  wait_ready || true

  if [ "$DRY_RUN" -eq 0 ] && [ "$SKIP_LOGIN" -eq 0 ]; then
    step "添加账号"
    say ""
    say "  三个平台都用「浏览器里点确认」完成，不用敲命令。"
    say "  面板已经开了，点 ${C_W}+ 添加账号${C_0} 选平台就行。"
    say ""
    open_console
  elif [ "$SKIP_LOGIN" -eq 1 ]; then
    step "添加账号"
    say ""
    say "  已跳过（--no-login）。随时打开面板点「+ 添加账号」："
    say "    http://127.0.0.1:$UI_PORT/"
    say ""
  else
    printf '  %s[dry-run]%s 登录：%s（真实运行时打开面板点「+ 添加账号」）\n' "$C_D" "$C_0" "$PROVIDERS"
  fi

  print_summary
}

main "$@"
