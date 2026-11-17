#!/usr/bin/env bash
# 健康检查 + 自愈 + 告警（macOS launchd 或 Linux systemd timer 每 5 分钟触发）
# 设计原则：能重启就重启，重启没用（401/429）就只告警，绝不乱重启把事情弄更糟。
set -uo pipefail

PORT="${PORT:-8317}"
BASE="http://127.0.0.1:${PORT}"
GW_DIR="${GW_DIR:-$HOME/claude-gateway}"
STATE_DIR="$GW_DIR/.state"
LOG="$GW_DIR/logs/watchdog.log"
SERVICE_LABEL="com.local.cliproxyapi"
FAIL_FILE="$STATE_DIR/fails"
LAST_ALERT="$STATE_DIR/last_alert"

mkdir -p "$STATE_DIR" "$(dirname "$LOG")"
umask 077

ts() { date '+%F %T'; }
say() { echo "[$(ts)] $*" >> "$LOG"; }

# 1 小时内最多弹一次通知
notify() {
  local msg="$1"
  local now last=0
  now=$(date +%s)
  [ -f "$LAST_ALERT" ] && last=$(cat "$LAST_ALERT" 2>/dev/null || echo 0)
  if [ $((now - last)) -lt 3600 ]; then
    say "SUPPRESS-NOTIFY ($msg)"
    return
  fi
  echo "$now" > "$LAST_ALERT"
  say "NOTIFY: $msg"
  if command -v osascript >/dev/null 2>&1; then
    osascript -e "display notification \"${msg//\"/}\" with title \"Claude 网关\"" >/dev/null 2>&1 || true
  elif command -v notify-send >/dev/null 2>&1; then
    notify-send "Claude 网关" "$msg" >/dev/null 2>&1 || true
  fi
}

restart_service() {
  say "ACTION: restart $SERVICE_LABEL"
  if [ "$(uname -s)" = "Darwin" ]; then
    launchctl kickstart -k "gui/$(id -u)/$SERVICE_LABEL" >/dev/null 2>&1 \
      || launchctl kickstart -k "system/$SERVICE_LABEL" >/dev/null 2>&1 \
      || say "ERROR: kickstart failed"
  else
    systemctl restart cliproxyapi >/dev/null 2>&1 || say "ERROR: systemctl restart failed"
  fi
  sleep 6
}

# ---------- 0. 服务是否加载 ----------
if [ "$(uname -s)" = "Darwin" ]; then
  if ! launchctl print "gui/$(id -u)/$SERVICE_LABEL" >/dev/null 2>&1; then
    say "WARN: launchd job not loaded, trying to load"
    launchctl load -w "$HOME/Library/LaunchAgents/$SERVICE_LABEL.plist" >/dev/null 2>&1 \
      && sleep 4 || { notify "网关未加载，自动加载失败，检查 plist"; exit 1; }
  fi
fi

# ---------- 1. 端口监听 ----------
if ! lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  say "DOWN: port $PORT not listening"
  restart_service
  if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    say "RECOVER: listening again"
  else
    notify "网关端口 $PORT 未恢复，需要人工检查"
    exit 1
  fi
fi

# ---------- 2. HTTP 健康 ----------
KEY=$(grep -m1 'sk-local' "$GW_DIR/config.yaml" 2>/dev/null | sed -E 's/.*"(sk-local-[^"]*)".*/\1/' | head -1)
if [ -z "$KEY" ]; then
  say "WARN: config.yaml 里没找到 api-keys，跳过 HTTP 检查"
  exit 0
fi

CODE=$(curl -s --noproxy '*' -o /dev/null -w '%{http_code}' -m 15 \
        -H "Authorization: Bearer $KEY" "$BASE/v1/models" 2>/dev/null)

case "$CODE" in
  200)
    [ -f "$FAIL_FILE" ] && rm -f "$FAIL_FILE"
    say "OK: 200"
    ;;
  401|403)
    # 重启没用：OAuth 被踢，必须人工重新登录
    say "AUTH-FAIL: $CODE"
    notify "OAuth 已失效（${CODE}），需重新执行 ./cli-proxy-api -claude-login"
    exit 0
    ;;
  429)
    # 限额，重启只会更糟
    say "RATE-LIMIT: 429（窗口限额，不重启）"
    ;;
  000|"")
    say "TIMEOUT: 无响应"
    echo $(( $(cat "$FAIL_FILE" 2>/dev/null || echo 0) + 1 )) > "$FAIL_FILE"
    ;;
  *)
    say "BAD: HTTP $CODE"
    echo $(( $(cat "$FAIL_FILE" 2>/dev/null || echo 0) + 1 )) > "$FAIL_FILE"
    ;;
esac

# ---------- 3. 连续 3 次异常才重启 ----------
FAILS=$(cat "$FAIL_FILE" 2>/dev/null || echo 0)
if [ "$FAILS" -ge 3 ]; then
  say "UNHEALTHY x$FAILS, restarting"
  restart_service
  rm -f "$FAIL_FILE"
  CODE2=$(curl -s --noproxy '*' -o /dev/null -w '%{http_code}' -m 15 \
          -H "Authorization: Bearer $KEY" "$BASE/v1/models" 2>/dev/null)
  if [ "$CODE2" = "200" ]; then
    say "RECOVER: 200 after restart"
  else
    notify "网关连续异常且重启无效（HTTP ${CODE2}）"
  fi
fi
