# Claude Max 订阅反代方案（完整版）

> 版本 v1 · 目标机器：macOS 27 / Apple Silicon / 用户 a11
> 目标：把本机 Claude Max 订阅的 Opus 额度，变成局域网/Tailnet 内多设备可用的 API 端点

---

## 0. 结论速览

| 项 | 结论 |
|---|---|
| 方案 | **CLIProxyAPI** 单二进制 + launchd 常驻 + Tailscale（或局域网） |
| 为什么 | 本机无 brew/go/node/docker；它是静态二进制，且自带 Claude OAuth 登录，不需要装 Claude Code |
| 端点 | `http://127.0.0.1:8317`（本机）、`http://<tailnet-ip>:8317`（其他设备） |
| 协议 | Anthropic 原生 `/v1/messages`（推荐）+ OpenAI 兼容 `/v1/chat/completions` |
| 鉴权 | 网关本地 API Key（`sk-local-*`），一台设备一把 |
| 系统改动 | 1 个后台进程、1 个 LaunchAgent、1 个端口、约 50 MB 磁盘 |
| 卸载 | 3 条命令，无残留 |
| 最大局限 | 单点依赖本机开机；所有设备共享同一个 Max 五小时窗口限额 |
| 最大风险 | 给**外部人**用会被 Anthropic 按 IP/指纹聚类，是封号最快的方式；家人/自己几台设备基本没事 |

---

## 1. 目标与边界

### 要做
- 本机持有 OAuth 凭证，其他设备**不接触凭证**，只拿一把可吊销的本地 Key
- 客户端可以用任意支持自定义 Base URL 的工具（Chatbox / Cline / Claude Code / Cursor）
- 有明确的开机自启、日志、吊销、卸载路径

### 不做
- 不做公网裸奔（不映射端口到公网、不用无鉴权的 Tunnel）
- 不做账号池 / 拼车售卖
- 不改 Anthropic 客户端、不逆向其二进制

---

## 2. 现状盘点与选型

实测本机：

| 项 | 状态 | 影响 |
|---|---|---|
| OS | macOS 27.0 / arm64 | 用 darwin_arm64 资产 |
| brew | 无 | 不能用 `brew install` |
| go / node / docker | 全无 | 排除需要编译或用 Node/Docker 的方案 |
| claude CLI | 未安装 | 排除依赖 Claude Code 刷新 token 的方案 |
| python3 | 有（`/usr/local/bin/python3`） | 备选方案可用 |
| jq | 有 | 脚本里解析 GitHub API 用 |
| 8317 端口 | 空闲 | 可直接用 |

**选型结论：CLIProxyAPI**

| 方案 | 依赖 | 能否本机跑 | 结论 |
|---|---|---|---|
| CLIProxyAPI | 无（静态二进制） | ✅ | **采用** |
| claude-code-proxy | python + uv | ⚠️ 需装 uv，且要 claude CLI 保活 token | 备选 |
| claude-relay-service | node + redis + postgres | ❌ | 排除 |
| LiteLLM | docker 或 python + 大量依赖 | ❌/⚠️ | 排除（API Key 场景才优选） |

关键理由：CLIProxyAPI 用自己的 OAuth 流程登录、自己用 refresh token 续期，**机器上不需要 Claude Code**。而 `claude-code-proxy` 这类方案依赖 `~/.claude/.credentials.json`，必须有 Claude Code 在跑才能刷新，8 小时就会 401。

---

## 3. 架构

```
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│ 手机 Chatbox │  │ 另一台 Mac   │  │ VS Code      │
│ Enchanted    │  │ Claude Code  │  │ Cline/Roo    │
└──────┬───────┘  └──────┬───────┘  └──────┬───────┘
       │  sk-local-phone │  sk-local-mac   │ sk-local-vsc
       └─────────────────┼─────────────────┘
                         │  Tailscale 100.x / 局域网 192.168.x
                         ▼
        ┌────────────────────────────────────┐
        │ 本机 a11  · 127.0.0.1:8317         │
        │ CLIProxyAPI                        │
        │  ├ 校验本地 Key                     │
        │  ├ 模型别名 opus→claude-opus-4-5   │
        │  ├ 注入 Claude Code 请求头 + OAuth  │
        │  └ refresh token 自动续期           │
        └────────────────┬───────────────────┘
                         │ HTTPS
                         ▼
                 api.anthropic.com
```

### 请求头改造（为什么必须用这个网关）

直连 `api.anthropic.com/v1/messages` 带 OAuth token 会被拒。Claude Code 实际发的是：

```
Authorization: Bearer sk-ant-oat01-...
anthropic-beta: oauth-2025-04-20,claude-code-20250219,...
user-agent: claude-cli/2.x.x (external, cli)
x-app: cli
anthropic-dangerous-direct-browser-access: true
system: "You are Claude Code, Anthropic's official CLI for Claude."  ← 必须作为 system 第一段
```

网关负责把普通 API 请求「套」成上面这个形状。所以**任何改动这条链路的上游升级，都可能需要跟着升级网关**——这是本方案最主要的技术脆弱点。

### 协议两条路

| 路径 | 保留 thinking | 保留 prompt cache | 推荐度 |
|---|---|---|---|
| `/v1/messages`（原生） | ✅ | ✅ | ★★★★★ |
| `/v1/chat/completions`（兼容） | ❌ | ❌ | ★★（只为兼容老客户端） |

---

## 4. 执行步骤（分 6 阶段，每阶段可独立回滚）

### Stage 0 · 准备（1 分钟，无副作用）

```bash
cd ~/.dsh/bot/workspace/claude-gateway
bash -n setup.sh            # 语法自检
cat config.yaml 2>/dev/null # 还没有，setup.sh 会生成
```

需要你先确定一个问题：**要不要跨网络访问？**
- 只在同一 WiFi 用 → 走 Stage 4B，不装任何东西
- 手机用 4G 也要能用 → 走 Stage 4A，装 Tailscale

### Stage 1 · 安装（约 30 秒）

```bash
bash setup.sh
```

实际动作：
1. `curl` GitHub Releases API 取最新 tag
2. 下载 `*darwin_arm64*` 资产到 `~/claude-gateway/`
3. 解包 → 定位可执行文件 → `mv` 成 `cli-proxy-api` → `chmod +x`
4. `xattr -d com.apple.quarantine` 去掉 Gatekeeper 隔离（否则首次运行会被拦）
5. 生成 `config.yaml`（3 把随机 Key + 随机管理密钥 + 模型别名）
6. 打印 3 把 Key

产物：
```
~/claude-gateway/
├── cli-proxy-api      # 静态二进制，约 20–30 MB
├── config.yaml
├── auth/              # 登录后才出现，权限应 700
└── logs/
```

失败处理：
- 找不到 darwin_arm64 资产 → 手动去 Releases 页下载，放到该目录再跑 `bash setup.sh`
- 下载被墙 → 挂代理后重跑，或手动下载
- `chmod` 后仍报「无法验证开发者」→ 系统设置 → 隐私与安全性 → 仍要打开

### Stage 2 · 登录（1 分钟，需浏览器）

```bash
cd ~/claude-gateway
./cli-proxy-api -claude-login
```

流程：浏览器打开 `claude.ai` 授权页 → 选你的 **Max** 账号 → 同意 → 回调 → 凭证落到 `~/claude-gateway/auth/*.json`。

注意：
- 这一步**新增一台「设备」**到你的 Claude 账号下（和你手机 App、浏览器并列）。账号设置里能看到。
- 如果参数报错，跑 `./cli-proxy-api -h` 看当前版本是 `-login` 还是 `-claude-login`。
- 登录后 `auth/` 里有 refresh token，**等同于账号凭证**，权限设 700，永不上传。

### Stage 3 · 常驻（30 秒）

```bash
cp ~/.dsh/bot/workspace/claude-gateway/com.local.cliproxyapi.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.local.cliproxyapi.plist

sleep 2
tail -20 ~/claude-gateway/logs/err.log
lsof -nP -iTCP:8317 -sTCP:LISTEN
```

预期：`lsof` 显示 `cli-proxy-api` 在 8317 上 LISTEN。

手动前台跑（排障用）：`./cli-proxy-api -config ~/claude-gateway/config.yaml`

### Stage 4 · 网络（二选一）

#### 4A · Tailscale（跨网络，推荐）

```bash
# 装：https://tailscale.com/download/mac（独立 pkg，无需 App Store）
T=/Applications/Tailscale.app/Contents/MacOS/Tailscale
$T up
$T ip -4                    # 记下 100.x.y.z
$T serve --bg 8317          # 可选：https://<machine>.<tailnet>.ts.net
```

其他设备装同账号 Tailscale → 直接访问 `http://100.x.y.z:8317`。

改动：新增一个系统网络扩展 + 一个后台守护进程 + 一个 100.x 网卡。
卸载：`$T down` + 卸载 App，网卡随之消失。

#### 4B · 局域网直连（零安装）

```bash
# 改 ~/claude-gateway/config.yaml：host: "0.0.0.0"
launchctl unload ~/Library/LaunchAgents/com.local.cliproxyapi.plist
launchctl load   ~/Library/LaunchAgents/com.local.cliproxyapi.plist

ipconfig getifaddr en0      # 例 192.168.1.23
```

其他设备访问 `http://192.168.1.23:8317`。

风险：同一 WiFi 内任何设备只要拿到 Key 就能用。家里网络可以接受；咖啡厅/公司网络不要开 `0.0.0.0`。

#### 4C · Cloudflare Tunnel（要公网域名时，不推荐给个人）

需要装 cloudflared + 在 Zero Trust 配 Access 策略。**裸 Tunnel 等于把网关挂公网**，只有在你明确需要「给不在 Tailnet 里的人临时用」时才考虑，且必须挂 Access Service Token。

### Stage 5 · 客户端接入（见 §8）

### Stage 6 · 验收（5 分钟）

```bash
KEY=$(grep -m1 'sk-local' ~/claude-gateway/config.yaml | tr -d ' -"')

# 1) 模型列表
curl -s http://127.0.0.1:8317/v1/models -H "Authorization: Bearer $KEY" | jq '.data[].id'

# 2) 原生协议 + 别名
curl -s http://127.0.0.1:8317/v1/messages \
  -H "Authorization: Bearer $KEY" -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"opus","max_tokens":64,"messages":[{"role":"user","content":"say ok"}]}' \
  | jq -r '.content[0].text'

# 3) 跨设备可达（在另一台设备上跑）
curl -s http://100.x.y.z:8317/v1/models -H "Authorization: Bearer $KEY" -m 5 | jq '.data|length'

# 4) 开机自启
sudo reboot   # 或 launchctl kickstart -k gui/$(id -u)/com.local.cliproxyapi
```

全绿 = 上线完成。

---

## 5. 影响分析

### 5.1 对本机的改动清单

| 类型 | 具体 | 可逆性 |
|---|---|---|
| 新增目录 | `~/claude-gateway/`（约 30–50 MB） | `rm -rf` |
| 新增进程 | `cli-proxy-api`（常驻，内存约 20–60 MB） | `launchctl unload` |
| 新增开机项 | `~/Library/LaunchAgents/com.local.cliproxyapi.plist` | 删文件 |
| 新增监听端口 | `127.0.0.1:8317`（或 `0.0.0.0:8317`） | 停进程 |
| 新增日志 | `~/claude-gateway/logs/{out,err}.log`，无限增长 | 需自己配 logrotate 或定期清 |
| 系统权限 | 无（不装内核扩展、不改 SIP、不要 sudo） | — |
| 电池/续航 | 常驻进程 + 每次请求的网络唤醒，笔记本会有轻微影响 | — |

**没有**改动：系统设置、Shell 配置、`/usr/local`、PATH、任何已有软件的配置。

### 5.2 对 Claude 账号的影响

| 影响 | 说明 |
|---|---|
| 设备列表 +1 | 账号设置里会多一条登录设备，和你手机/浏览器并列 |
| 限额共享 | **不是增加额度**。所有设备共用你 Max 的那一个五小时窗口 + 周限额 |
| 速率模型变化 | 从「一个人敲键盘」变成「多设备并发流式」，更容易撞限流 |
| 可见性 | Anthropic 能看到同一订阅来自多个 IP/UA。同城家庭 IP 正常；短时间跨洲 IP 跳跃会触发风控验证 |
| token 生命周期 | access token 短期有效，靠 refresh token 续；改密码 / 在账号页「退出所有设备」会让它失效，需重新 Stage 2 |

### 5.3 对安全的影响

- **凭证集中在本机**：`~/claude-gateway/auth/` 是账号级凭证。本机被入侵 = Claude 账号被拿走。确保 FileVault 开启、该目录 700。
- **日志里有对话内容**：网关会把请求/响应写日志。任何拿到日志的人能读到你的完整对话。要么关日志，要么定期清，要么把 `logs/` 设 700。
- **局域网直连模式下，Key 是唯一防线**。Key 泄露 = 你的额度被别人跑。所以一台设备一把，方便定点吊销。

### 5.4 对费用的影响

订阅制，**不按 token 计费**，所以反代本身不加钱。但：
- 撞限额会导致你和家人自己的正常使用被限流
- 走 `/v1/chat/completions` 会丢 prompt cache，等效消耗更多 token → 更快撞限额（不是加钱，是加消耗）

---

## 6. 局限（必须接受的 7 条）

| # | 局限 | 影响面 | 缓解 |
|---|---|---|---|
| 1 | **非官方通道**：Anthropic 改客户端校验头，网关就失效 | 全链路 | 关注上游 Release；失效时升级二进制即可，配置不用动 |
| 2 | **共享同一份限额**：不是"额度变多"，是多设备分同一份额度 | 用量 | 网关侧限并发 2–3；重度任务错峰 |
| 3 | **单点故障**：Mac 睡眠/关机 = 全设备不可用 | 可用性 | 系统设置→电池→「防止自动睡眠」；或换常开的 Mac mini/NAS |
| 4 | **OAuth 可能被踢**：改密码、异地登录、风控都会让 refresh token 失效 | 可用性 | 失效后只需重跑 Stage 2，1 分钟恢复 |
| 5 | **无法多租户隔离**：你给别人的 Key 无法限制"只能问什么"，日志里也有他们和你的对话 | 隐私 | 只给可信的人；不同人不同 Key；定期轮换 |
| 6 | **OpenAI 兼容层功能残缺**：丢 thinking、丢 cache_control、丢 1M context beta | 客户端兼容 | 优先选支持自定义 Anthropic 端点的客户端 |
| 7 | **无 SLA**：429 / 5xx 直接透传给客户端，没有重试队列 | 稳定性 | 客户端侧开重试；网关侧不要叠高并发 |

---

## 7. 风险分级（自用 vs 分享）

| 场景 | 风险 | 建议 |
|---|---|---|
| 自己 2–3 台设备（手机+平板+另一台电脑），同一城市 | **低** | 直接做 |
| 家人共用（同一住址、同一 WiFi） | **低-中** | 可以，各发一把 Key，限并发 |
| 异地朋友共用（不同城市/国家） | **中-高** | Anthropic 按 IP 聚类，容易触发验证甚至限制；不建议长期 |
| 对外分发/售卖 | **极高** | 这是最快被封的路径，不要做 |

---

## 8. 其他人如何使用

### 8.1 Key 发放

每把 Key 对应一个人/一台设备，命名写进 config 便于识别：

```yaml
api-keys:
  - "sk-local-phone-a11"       # 我的 iPhone
  - "sk-local-mac-air-a11"     # 我的 MacBook Air
  - "sk-local-dad-ipad"        # 家人 iPad
```

改完 `launchctl kickstart -k gui/$(id -u)/com.local.cliproxyapi` 生效。
吊销 = 删那一行 + 重启，其他设备不受影响。

### 8.2 三种交付方式（按安全度）

**① 同 Tailnet（最安全，推荐给家人/同事）**
对方装 Tailscale，你把他拉进同一个 tailnet（或发 node share 邀请）。他访问 `http://100.x.y.z:8317`。
→ 网络层先过一道设备鉴权，Key 只是第二道。

**② 你自己的 Tailnet + ACL 限制**
Tailscale 后台 → Access Controls，只允许特定 tag 访问 8317：

```json
{
  "acls": [
    { "action": "accept", "src": ["tag:family"], "dst": ["tag:gateway:8317"] }
  ],
  "tagOwners": { "tag:family": ["autogroup:admin"], "tag:gateway": ["autogroup:admin"] }
}
```

**③ Cloudflare Tunnel + Access（对方什么都不能装）**
给他一个邮箱，Access 策略只放这个邮箱，他通过 `https://ai.example.com` 访问，走邮箱 OTP 或 Service Token。**不要**用无 Access 的公网 Tunnel。

### 8.3 对方要填什么（三种客户端形态）

**A. Claude Code（另一台机器）**

```bash
export ANTHROPIC_BASE_URL="http://100.x.y.z:8317"
export ANTHROPIC_AUTH_TOKEN="sk-local-dad-ipad"
export ANTHROPIC_MODEL="opus"
export ANTHROPIC_SMALL_FAST_MODEL="haiku"
claude
```
写成 `~/.zshrc` 或项目 `.envrc` 即可常驻。

**B. 桌面 GUI（Chatbox / Cherry Studio / LobeChat / Open WebUI）**

| 字段 | 值 |
|---|---|
| Provider 类型 | OpenAI 兼容（或 Anthropic，若客户端支持） |
| Base URL | `http://100.x.y.z:8317/v1` |
| API Key | `sk-local-xxx` |
| Model | `opus` |

> 如果客户端支持「Anthropic / Claude」原生类型，就选它，Base URL 去掉 `/v1` 后缀填 `http://100.x.y.z:8317`，这样能保留 thinking。

**C. 编程插件（Cline / Roo Code / Continue / Cursor）**

- Provider: `OpenAI Compatible`
- Base URL: `http://100.x.y.z:8317/v1`
- API Key: `sk-local-xxx`
- Model ID: `opus`

**D. 手机（iOS/Android）**

Chatbox / Enchanted / OpenCat / NextChat 都支持自定义 Base URL。同样的四项。手机需先连上 Tailscale（或同一 WiFi）。

### 8.4 给对方的一句话说明（可直接转发）

```
端点：http://100.x.y.z:8317/v1
Key：  sk-local-xxx
模型：opus
协议：OpenAI 兼容；你的工具如果支持 Anthropic 原生，Base 填 http://100.x.y.z:8317（不带 /v1）效果更好
注意：不要并发大量请求，会触发限流；不要在公网分享这个地址
```

---

## 9. 运维手册

### 日常检查

```bash
# 进程在不在
lsof -nP -iTCP:8317 -sTCP:LISTEN

# 最近错误
tail -50 ~/claude-gateway/logs/err.log

# 限额状态（看是否 429）
grep -c 'rate_limit\|429' ~/claude-gateway/logs/out.log

# 凭证有效期
ls -l ~/claude-gateway/auth/
```

### 故障排查表

| 症状 | 原因 | 处理 |
|---|---|---|
| `401 authentication_error` | OAuth 过期/被踢 | 重跑 `./cli-proxy-api -claude-login` |
| `429 rate_limit` | 并发太高 / 撞窗口限额 | 降并发、错峰；等窗口重置 |
| 连接被拒 | 进程没起 / 端口占用 | 看 err.log；`lsof` 查冲突 |
| 其他设备连不上，本机正常 | host 还是 127.0.0.1 / 防火墙 | 改 host、系统设置→网络→防火墙放行 |
| 流式一顿一顿 | 中间层缓冲（nginx/Tunnel） | `proxy_buffering off`、`proxy_read_timeout 600s` |
| 首次响应 60s 就断 | 客户端/中间层超时 | 全链路超时提到 600s |
| 升级后全挂 | 上游改了校验头 | 升级 CLIProxyAPI 到最新版 |

### 升级

```bash
cd ~/.dsh/bot/workspace/claude-gateway && bash setup.sh   # config 已存在会跳过，不覆盖你的 Key
launchctl kickstart -k gui/$(id -u)/com.local.cliproxyapi
```

### 日志轮转（可选，防止日志撑爆磁盘）

```bash
cat > ~/Library/LaunchAgents/com.local.cliproxyapi.logrotate.plist <<'PLIST'
... （或简单加一条 cron）
PLIST
# 简易版：每周清空
0 4 * * 0  : > ~/claude-gateway/logs/out.log && : > ~/claude-gateway/logs/err.log
```

---

## 10. 验收清单

- [ ] `lsof -nP -iTCP:8317` 有 LISTEN
- [ ] `/v1/models` 返回含 `opus`
- [ ] `/v1/messages` 用 `opus` 能拿到文本
- [ ] 第二台设备（Tailscale/局域网）能连
- [ ] 重启后自动起来
- [ ] `~/claude-gateway/auth/` 权限 700
- [ ] 知道怎么吊销单把 Key
- [ ] 知道怎么彻底卸载（见 §11）

---

## 11. 回滚 / 卸载

```bash
# 停服务 + 取消开机自启
launchctl unload -w ~/Library/LaunchAgents/com.local.cliproxyapi.plist
rm -f ~/Library/LaunchAgents/com.local.cliproxyapi.plist

# 删程序与凭证（auth/ 里是账号凭证，删了需重新登录）
rm -rf ~/claude-gateway

# Tailscale（如果装了）
/Applications/Tailscale.app/Contents/MacOS/Tailscale down
# 再卸载 App

# 账号侧（可选）：claude.ai → Settings → 安全 → 移除该设备授权
```

三步，无残留：没有任何系统级改动需要恢复。

---

## 12. 附录：config.yaml 字段说明

| 字段 | 作用 | 建议 |
|---|---|---|
| `host` | 监听地址 | 本机用 `127.0.0.1`；局域网直连改 `0.0.0.0` |
| `port` | 端口 | 8317；被占用换 18317 |
| `auth-dir` | OAuth 凭证与状态目录 | 绝对路径，权限 700 |
| `api-keys` | 客户端鉴权 Key 列表 | 一台设备一把 |
| `remote-management.allow-remote` | 是否允许远程调管理 API | **永远 false** |
| `remote-management.secret-key` | 管理 API 密钥 | 随机 48 hex |
| `oauth-model-alias.claude[].name` | 上游真实模型 ID | Anthropic 发新版时改这里 |
| `oauth-model-alias.claude[].alias` | 客户端看到的别名 | 固定 `opus`/`sonnet`/`haiku` |
| `fork` | 是否保留原名同时暴露别名 | `true`，兼容老客户端 |

---

## 13. 一页流程图（谁在什么时候做什么）

```
你                          系统                        其他设备
│
├─ bash setup.sh ─────────▶ 下二进制 / 生成 config
│
├─ -claude-login ─────────▶ 开浏览器 / 落 auth/
│
├─ launchctl load ────────▶ 常驻 8317 ────────────────▶ 可用
│
├─ 装 Tailscale ──────────▶ 100.x 网卡 ──────────────▶ 跨网络可达
│
├─ 发 Key ───────────────────────────────────────────▶ 填 Base URL + Key + opus
│
└─ 想停 ── launchctl unload + rm -rf ──▶ 完全恢复
```
