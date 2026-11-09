# Claude Max 订阅 → 多设备反代网关

目标：本机持有 Claude Max 的 OAuth 凭证，跑一个本地网关（`127.0.0.1:8317`），
手机 / 平板 / 另一台电脑用一把本地 Key 走 **Anthropic 原生 `/v1/messages`** 或
**OpenAI 兼容 `/v1/chat/completions`** 调用 Opus。

```
设备 ──Tailscale/局域网──▶ 本机 8317 (CLIProxyAPI) ──OAuth──▶ api.anthropic.com
```

---

## 1. 安装

```bash
cd ~/.dsh/bot/workspace/claude-gateway
bash setup.sh
```

跑完会打印 3 把 `sk-local-...`，一台设备给一把（出事单独吊销）。

## 2. 登录 Claude Max

```bash
cd ~/claude-gateway
./cli-proxy-api -claude-login
```

浏览器弹出 → 选你的 Max 账号授权 → 凭证落到 `~/claude-gateway/auth/`。
之后 access token 到期由网关用 refresh token 自动续，**不需要 Claude Code 常驻**。

## 3. 常驻

```bash
cp com.local.cliproxyapi.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.local.cliproxyapi.plist
# 查看
tail -f ~/claude-gateway/logs/err.log
```

停止：`launchctl unload -w ~/Library/LaunchAgents/com.local.cliproxyapi.plist`

## 4. 自测

```bash
KEY=$(grep -m1 'sk-local' ~/claude-gateway/config.yaml | tr -d ' -"')
curl -s http://127.0.0.1:8317/v1/models -H "Authorization: Bearer $KEY" | jq '.data[].id'

curl -s http://127.0.0.1:8317/v1/messages \
  -H "Authorization: Bearer $KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"opus","max_tokens":64,"messages":[{"role":"user","content":"say ok"}]}' | jq -r '.content[0].text'
```

---

## 5. 网络层（按推荐度）

### A. Tailscale —— 首选，零公网暴露

```bash
# 装：https://tailscale.com/download/mac （独立 pkg，不需要 App Store）
# 装完 CLI 在：
T=/Applications/Tailscale.app/Contents/MacOS/Tailscale
$T up
$T ip -4                      # 100.x.y.z
$T serve --bg 8317            # 可选：给个真 HTTPS，SSE 更稳
```

其他设备装同账号 Tailscale，Base URL 填 `http://100.x.y.z:8317`。
`tailscale serve` 之后填 `https://<machine>.<tailnet>.ts.net`。

### B. 同一 WiFi 直连（最省事，仅限可信家庭网络）

```bash
# config.yaml 里 host 改成 0.0.0.0，重启服务
ipconfig getifaddr en0        # 拿本机局域网 IP，比如 192.168.1.23
```

Base URL：`http://192.168.1.23:8317`。
**前提**：`api-keys` 必须留着；不要把这个端口在路由器上做映射。

### C. Cloudflare Tunnel + Access（要公网域名时）

```bash
# 装 cloudflared（pkg 或直接下二进制）
cloudflared tunnel login
cloudflared tunnel create claude
cloudflared tunnel route dns claude ai.example.com
cloudflared tunnel run --url http://127.0.0.1:8317 claude
```

然后必须在 Zero Trust 给 `ai.example.com` 加 Access 策略（只放你的邮箱）。
**裸 Tunnel 不加 Access = 把带凭证的网关挂公网。**

---

## 6. 各客户端怎么填

| 客户端 | Base URL | Key | Model |
|---|---|---|---|
| **Claude Code**（另一台机器） | `ANTHROPIC_BASE_URL=http://100.x.y.z:8317` | `ANTHROPIC_AUTH_TOKEN=sk-local-...` | `ANTHROPIC_MODEL=opus` |
| Cherry Studio / Chatbox | `http://100.x.y.z:8317/v1` | 同上 | `opus` |
| LobeChat / Open WebUI | `http://100.x.y.z:8317/v1` | 同上 | `opus` |
| Cline / Roo Code / Continue | OpenAI Compatible，`.../v1` | 同上 | `opus` |
| Cursor / Windsurf | Override OpenAI Base URL | 同上 | `opus` |
| iOS / Android（Chatbox、Enchanted、OpenCat） | `http://100.x.y.z:8317/v1` | 同上 | `opus` |

Claude Code 完整写法：

```bash
export ANTHROPIC_BASE_URL="http://100.x.y.z:8317"
export ANTHROPIC_AUTH_TOKEN="sk-local-phone-xxxxxxxx"
export ANTHROPIC_MODEL="opus"
export ANTHROPIC_SMALL_FAST_MODEL="haiku"
claude
```

> ⚠️ 要 Opus 的 **thinking 思考链** 和 **prompt caching**，必须走 Anthropic 原生
> `/v1/messages`。OpenAI 兼容层会丢掉 `thinking`、`cache_control`，缓存命中率归零。
> 能用原生协议就别用 Compat。

---

## 7. 运维要点

1. **网关只 bind 127.0.0.1**，外网入口交给 Tailscale / Tunnel。
2. **每设备一把 Key**，`api-keys` 里增删即可，不用重启其他设备。
3. **并发压到 2–3**：Max 是按「人的使用速率」建模，三台同时流式很容易 429。
   多设备抢额度时，网关日志里会看到 `rate_limit` / `429`，此时降并发或错峰。
4. **超时放宽到 600s**：Opus 长思考首 token 可能 60s+，中间层默认会掐断。
5. **日志脱敏**：请求体里是完整对话内容，别把 `logs/out.log` 传到任何地方。
6. **模型名走别名**：客户端只写 `opus`/`sonnet`，Anthropic 换版本号只改 `config.yaml` 一处。
7. **别把额度卖给别人**：多设备自用没问题；对外分发时 Anthropic 会按 IP / 指纹聚类，
   是最容易直接封号的用法。

## 8. 文件清单

```
claude-gateway/
├── setup.sh                        # 一键装二进制 + 生成 config
├── com.local.cliproxyapi.plist     # launchd 开机自启
└── README.md                       # 就是本文件

~/claude-gateway/                   # setup.sh 运行后产生
├── cli-proxy-api                   # 网关二进制
├── config.yaml                     # 端口 / Key / 模型别名
├── auth/                           # OAuth 凭证（权限 700，别外传）
└── logs/
```
