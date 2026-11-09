# Codex + Grok 接入方案

> 目标：把本地已订阅的 Codex（ChatGPT）和 Grok（xAI）也接到同一个网关，和 Claude Max 并列。
> 日期：2026-11-02 · 基于 CLIProxyAPI v8.0.51 实测

---

## 零、结论先看

**不用装第二个软件。** CLIProxyAPI 本来就是多 provider 网关，Codex 和 xAI 都是它的一等公民。

加完之后：

- 还是一个端口 `8317`
- 还是那三把 `sk-local-*` Key
- 一个 `/v1/models` 列表里同时出现 `claude-*` / `gpt-*` / `grok-*`
- 客户端只改「模型名」一个字段，URL 和 Key 都不用动

---

## 一、实测到的能力（不是查资料推测）

| 能力 | 实测结果 |
|---|---|
| Codex OAuth | `-codex-login`（本地回调 `localhost:1455`）<br>`-codex-device-login`（设备码） |
| xAI OAuth | `-xai-login`，设备码流程 |
| 模型别名 | `oauth.model-alias` 支持 8 个 channel：`vertex` `aistudio` `antigravity` `claude` `codex` `kimi` `xai` `meta` |
| 走 API Key | `api-keys.xai` 支持直填 xAI 官方 Key（`https://api.x.ai/v1`），按量计费 |
| 模型清单 | `/v1/models` 会合并三家，客户端能一次性拉到 |

实测抓到的原文：

**xAI（设备码）**
```
Starting xAI authentication...
To authenticate, please visit:
https://accounts.x.ai/oauth2/device?user_code=X4A9-RX95
Then enter this code: X4A9-RX95
Waiting for authorization...
(This will timeout in 1800 seconds if not authorized)
```

**Codex（设备码）**
```
Starting Codex device authentication...
Codex device URL: https://auth.openai.com/codex/device
Codex device code: VNNB-3ACCW
```

**Codex（回调式，作对照）**
```
Visit the following URL to continue authentication:
https://auth.openai.com/oauth/authorize?client_id=app_EMoamEEZ73f0CkXaXp7hrann&...&redirect_uri=http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback
Waiting for Codex authentication callback...
```
> 它会提示「To authenticate from a remote machine, an SSH tunnel may be required」，还把你的出口 IP `82.26.25.186` 打出来 —— 这是因为它以为你在远程服务器上。本机操作可以忽略这段提示。

---

## 二、你这台机器的现状

| 项 | 实测 |
|---|---|
| Codex 登录态 | `~/.codex/auth.json` 存在，`auth_mode: chatgpt`，账号 `your-email@example.com`，上次刷新 2026-10-27 |
| Codex CLI | `/Applications/ChatGPT.app/.../codex` 版本 `0.164.0-alpha.11.1` |
| Grok 客户端 | 装了 `Grok Bot.app` |
| Grok 浏览器登录态 | Chrome 里有 `.x.ai` / `.grok.com` / `.accounts.x.ai` 的 cookie → **浏览器里是登录状态** |
| 结论 | 两个都能直接走 OAuth，**不需要额外申请 API Key** |

---

## 三、三个关键设计决策

### 决策 1：两个都用设备码，不用回调

`-codex-login` 要求浏览器和网关在同一台机器上（要接 `localhost:1455` 的回调）。设备码流程没有这个约束：在**任何设备**的浏览器输入 8 位码即可，网关这边只是轮询。

好处不止一点：

- 换机器、换网络都不影响
- 不用管 SSH 隧道
- 授权链接可以转发到手机上点

### 决策 2：模型名必须互不撞车

官方在 `config.example.yaml` 里明确警告：

> Because aliases affect the merged /v1 model list and merged request routing, overlapping client-visible names can become ambiguous across providers. For strict backend pinning, use unique aliases/prefixes or avoid overlapping names.

翻译：三家模型合并到同一个列表，**别名重复会让路由不知道发给谁**。

所以定好命名空间：

| 家 | 客户端看到的模型名 | 实际后端 |
|---|---|---|
| Claude | `opus` / `sonnet` / `haiku` | claude-opus-5-5 等（已有） |
| Codex | `codex` / `codex-mini` | gpt-5.x-codex 系列 |
| Grok | `grok` / `grok-mini` | grok-4.x 系列 |

保留 `fork: true`，原始全名（`gpt-5.3-codex`、`grok-4.5`）也照样能调，方便对照。

### 决策 3：额度各算各的

| 家 | 扣谁的额度 | 限额形态 |
|---|---|---|
| Claude | Max 订阅 | 5 小时窗口 + 周限额 |
| Codex | ChatGPT 订阅 | 5 小时窗口 + 周限额 |
| Grok | xAI 订阅 | 按订阅档位 |

**三家互不影响。** Claude 撞限额了，Codex 和 Grok 照样能用 —— 这本身就是多接两家的最大价值。

---

## 四、执行步骤

### 第 1 步：备份（30 秒）

```bash
cd ~/claude-gateway
cp config.yaml config.yaml.bak-$(date +%Y%m%d-%H%M)
ls -la auth/
```

### 第 2 步：登录 Codex（2 分钟）

在一个**普通终端窗口**里跑：

```bash
cd ~/claude-gateway
./cli-proxy-api -config ./config.yaml -codex-device-login
```

终端会打印：

```
Codex device URL: https://auth.openai.com/codex/device
Codex device code: XXXX-XXXXX
```

打开那个 URL，输入这个码，用 `your-email@example.com` 授权。成功后 `auth/` 下会多出一个 `codex-*.json`。

### 第 3 步：登录 Grok（2 分钟）

```bash
cd ~/claude-gateway
./cli-proxy-api -config ./config.yaml -xai-login
```

终端打印：

```
https://accounts.x.ai/oauth2/device?user_code=XXXX-XXXX
Then enter this code: XXXX-XXXX
```

浏览器打开（你 Chrome 里本来就是登录态，大概率直接确认），成功后 `auth/` 下多一个 `xai-*.json`。

> 两个都有 30 分钟窗口，不用抢时间。

### 第 4 步：改 config.yaml 加别名（1 分钟）

在 `oauth.model-alias` 下面加两段。**登录完先看一眼真实模型名**：

```bash
cd ~/claude-gateway
./cli-proxy-api -config ./config.yaml -tui   # 或用下面第 5 步的 /v1/models 看
```

确认名字后这样写：

```yaml
oauth:
  auth-dir: "/Users/a11/claude-gateway/auth"
  model-alias:
    claude:                       # 已有，不动
      - {name: "claude-opus-5-5",   alias: "opus",   display-name: "Opus 5.5",   fork: true}
      - {name: "claude-sonnet-5-5", alias: "sonnet", display-name: "Sonnet 5.5", fork: true}
      - {name: "claude-haiku-5-5",  alias: "haiku",  display-name: "Haiku 5.5",  fork: true}
    codex:                        # 新增
      - {name: "gpt-5.3-codex",     alias: "codex",  display-name: "Codex",       fork: true}
    xai:                          # 新增
      - {name: "grok-4.5",          alias: "grok",   display-name: "Grok 4.5",    fork: true}
```

> 上面 `name:` 里的型号是**占位**，真实值以第 5 步 `/v1/models` 输出为准，写错不生效但不报错。

可选优化（Grok 有联网搜索能力时）：

```yaml
upstream:
  xai:
    inject-x-search: false   # 默认 false；设 true 会在没声明工具时自动注入 x_search
```

### 第 5 步：重启 + 验收（1 分钟）

```bash
launchctl kickstart -k gui/501/com.local.cliproxyapi
sleep 3

# 看模型列表（应该三家都在）
curl -s -H "Authorization: Bearer sk-local-REPLACE_WITH_YOUR_KEY" \
  http://127.0.0.1:8317/v1/models | jq -r '.data[].id' | sort

# 分别打一发
for m in opus codex grok; do
  echo "--- $m ---"
  curl -s -m 90 -H "Authorization: Bearer sk-local-REPLACE_WITH_YOUR_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"$m\",\"max_tokens\":4000,\"messages\":[{\"role\":\"user\",\"content\":\"你是哪个模型？一句话\"}]}" \
    http://127.0.0.1:8317/v1/chat/completions | jq -r '.choices[0].message.content // .error.message'
done
```

验收标准：三条都有正常回答，且自我介绍各不相同。

### 第 6 步：客户端（0 分钟）

**什么都不用改** —— URL 和 Key 全是原来的，只改模型名。

| 客户端 | 填法 |
|---|---|
| Chatbox / dsh / Cline / Trae | 模型填 `codex` 或 `grok` |
| Claude Code | `ANTHROPIC_MODEL=opus` 保持不变（它只会用 Claude） |
| Codex CLI 想用网关 | `~/.codex/config.toml` 加 `[model_providers.maxgw]`，`base_url = "http://127.0.0.1:8317/v1"`，`wire_api = "chat"`，模型填 `codex` 或 `opus` |

---

## 五、需要你先确认两件事

1. **Codex 就用 `your-email@example.com` 这个号？** 还是想换另一个 ChatGPT 账号（多账号可以都加进来，网关会做负载均衡）。
2. **Grok 用的是 SuperGrok 订阅，还是 X（推特）内置的 Grok？** 两者在 xAI 那边是不同的产品线，可能影响 device flow 能不能过。

---

## 六、可能踩的坑（按概率排序）

| 坑 | 表现 | 处理 |
|---|---|---|
| 设备码窗口过期 | 终端报 timeout | 30 分钟内没输入就重跑，无副作用 |
| Grok 订阅档位不够 | 浏览器说 "no eligible subscription" | 换账号，或退到 `api-keys.xai` 用官方 API Key（按量付费） |
| 别名撞车 | 某个模型名调出来是别家的 | 只用一个命名空间（`codex` / `grok`），别和 `opus` 混 |
| Codex 与桌面版同时在线 | 桌面 Codex 掉登录 / 额度异常 | 一般不会；真有冲突就让桌面版用官方，网关用另一个号 |
| Grok 的 max_tokens 行为不同 | 回答被截断 | Grok 不像 Opus 那样必先思考，2000 通常够；截断就调到 4000 |
| 别名 `name` 写错 | 别名不生效但 API 不报错 | 以第 5 步 `/v1/models` 实际输出为准回填 |

---

## 七、回滚（10 秒）

```bash
cd ~/claude-gateway
cp config.yaml.bak-<时间戳> config.yaml
rm -f auth/codex-*.json auth/xai-*.json
launchctl kickstart -k gui/501/com.local.cliproxyapi
```

删掉对应 auth 文件即下线该 provider，Claude 那边完全不受影响。
