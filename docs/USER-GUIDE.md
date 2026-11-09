# Gateway One 接入指南

> 这份文档是给**使用者的**，不是给搭建者的。
> 你手上应该已经拿到了：一个 **Key**、一台已经装好网关的机器地址、和一句「去装 Tailscale」。

---

## 目录

- [0. 你拿到的是什么](#0-你拿到的是什么)
- [1. 第一步：装 Tailscale（必做，不装连不上）](#1-第一步装-tailscale必做不装连不上)
- [2. 第二步：拿你的 Key](#2-第二步拿你的-key)
- [3. 第三步：30 秒验证连得通](#3-第三步30-秒验证连得通)
- [4. 各客户端接入（挑你用的那个看）](#4-各客户端接入挑你用的那个看)
- [5. 三条铁律](#5-三条铁律)
- [6. 出问题了查这里](#6-出问题了查这里)
- [7. 常见问题](#7-常见问题)

---

## 0. 你拿到的是什么

一台**一直开着的 Mac** 在跑一个网关，它背后接了几个 AI 订阅账号。你通过这个网关用它们的额度，**不需要你自己有账号、不需要付订阅费**。

| 你能用的模型 | 填这个名字 | 背后是什么 |
|---|---|---|
| `opus` | `opus` | Claude 最强的模型（Claude Opus 5.5） |
| `sonnet` | `sonnet` | Claude 均衡型号，快一些 |
| `haiku` | `haiku` | Claude 最快最便宜的，适合小任务 |
| `gpt` | `gpt` | GPT-6 Astra（推理强） |
| `codex` | `codex` | GPT-6.1 Sol（偏编程） |
| `grok` | `grok` | Grok 4.7（最新） |
| `grok-fast` | `grok-fast` | Grok 4.5（快） |
| `gemini` | `gemini` | Gemini（Antigravity 通道） |
| `gemini-flash` | `gemini-flash` | Gemini 轻量版 |

**记住 9 个名字就够了**，不用管后面还有 63 个真实模型名。默认用 `opus`，要快就用 `haiku`。

---

## 1. 第一步：装 Tailscale（必做，不装连不上）

### 为什么必须先装这个

服务器上的端口**只对本机和 Tailscale 内部开放**，同一 WiFi 也连不进去。这是故意的——不然任何连上同一个网络的人都能摸到它。

Tailscale 是一个虚拟局域网工具，装了它之后，你的设备会拿到一个 `100.x.x.x` 的地址，就像和服务器插在同一台交换机上。

### 情况 A：这是你自己的设备（手机 / iPad / 另一台电脑）

1. 打开 App Store / 应用商店，搜 **Tailscale**，装
   - iPhone/iPad：[App Store 搜 Tailscale](https://apps.apple.com/app/tailscale/id1470499037)
   - Android：Play 商店搜 Tailscale
   - Mac：`https://tailscale.com/download/mac`
   - Windows：`https://tailscale.com/download/windows`
2. 打开 App，点 **Log in**
3. **用服务器主人的账号登录**（地址和账号找他要，这里不写明文）
4. 登录后它会显示「Connected」，设备列表里能看到服务器那台机器

### 情况 B：你是家人/朋友，不想用自己的账号

让服务器主人在 Tailscale 后台把你的设备**共享**给他：

1. 他登录 `https://login.tailscale.com/admin/machines`
2. 找到他那台机器，右边 `⋯` → **Share**
3. 输入你的邮箱，发邀请
4. 你点邮件里的链接，用**你自己的** Tailscale 账号接受
5. 你的设备就能看到那一台机器（看不到他其他的设备）

### 装完确认一下

打开 Tailscale App，应该能看到类似这样的列表：

```
100.x.y.z   1macbook-pro-2   macOS   ● Connected
你自己的设备       ...
```

只要 `100.x.y.z` 前面是绿点，就通了。

> ⚠️ **别用域名**。有些人的电脑上装了 Clash 之类的代理，会把 `xxx.ts.net` 这种域名解析成假地址。**只用 `100.x.y.z` 这个数字 IP**，它在任何网络下都不变。

---

## 2. 第二步：拿你的 Key

Key 长这样，`sk-local-` 开头，40 个字符：

```
sk-local-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

**每个人一把，不要互相传**。谁泄露了，管理员可以只吊销那一把，不影响别人。

找服务器主人要，他会告诉你哪把是你的。

---

## 3. 第三步：30 秒验证连得通

装好 Tailscale、拿到 Key 之后，先别急着配客户端，**先验证一下**。

### 有终端（Mac / Linux / Windows PowerShell）

复制下面整段，把 `你的KEY` 换成真 Key，粘进终端回车：

```bash
curl -s -H "Authorization: Bearer 你的KEY" http://100.x.y.z:8317/v1/models
```

**看到一长串 JSON（开头是 `{"data":[{"id":"claude-..."`）就是通了。**

再试一次真实对话：

```bash
curl -s -X POST http://100.x.y.z:8317/v1/chat/completions \
  -H "Authorization: Bearer 你的KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"haiku","messages":[{"role":"user","content":"说\"收到\""}],"max_tokens":2000}'
```

看到 `"content":"收到"` 就完全没问题了。

### 没终端（手机上）

用手机浏览器打开：

```
http://100.x.y.z:8317/v1/models
```

会看到 `{"error":{"message":"...","type":"invalid_request_error"}}` 之类的报错——**这是好事**，说明网络通了，只是浏览器没带 Key。真正要测对话，直接用第 4 章的客户端。

### 三种失败的样子，对号入座

| 你看到的 | 意思 | 怎么办 |
|---|---|---|
| 一直转圈，最后「无法连接」 | Tailscale 没连上 | 打开 Tailscale App 看是不是 Connected |
| `401` / `unauthorized` | Key 不对或没带 | 检查 Key 有没有复制全（40 个字符，别多空格） |
| `404 not found` | 地址少了 `/v1` | OpenAI 协议必须带 `/v1`，见第 5 章 |

---

## 4. 各客户端接入（挑你用的那个看）

**通用规则**：所有客户端都填同一个地址，区别只在于「用 Anthropic 协议还是 OpenAI 协议」——决定要不要带 `/v1`。

```
Anthropic 协议（Claude 原生）  http://100.x.y.z:8317        不带 /v1
OpenAI 协议（通用兼容）        http://100.x.y.z:8317/v1     带 /v1
```

填反了就是 404。

---

### 4.1 Claude Code（命令行）

**适合**：在终端里写代码、跑 Claude Code 的人。

**第 1 步**：打开（没有就新建）这个文件：

```
~/.claude/settings.json
```

Mac 上的完整路径是 `/Users/你的用户名/.claude/settings.json`。

**第 2 步**：把 `env` 这一段填成下面这样（如果文件里已经有 `env`，把下面这几行加进去）：

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://100.x.y.z:8317",
    "ANTHROPIC_AUTH_TOKEN": "你的KEY",
    "ANTHROPIC_MODEL": "opus",
    "ANTHROPIC_SMALL_FAST_MODEL": "haiku",
    "NO_PROXY": "100.x.y.z"
  }
}
```

**第 3 步**：如果你电脑上开着代理（Clash / Surge 之类），`NO_PROXY` 那行必须加，不然流量会被代理拦走。

**第 4 步**：重开终端，跑 `claude`，随便问一句。能回答就成。

> 注意 `ANTHROPIC_BASE_URL` **不带 `/v1`**，Claude Code 自己会拼。

---

### 4.2 Codex CLI（命令行）

**第 1 步**：打开 `~/.codex/config.toml`（没有就新建）。

**第 2 步**：在文件**最上面**加这两行：

```toml
model = "opus"
model_provider = "gatewayone"
```

**第 3 步**：在文件**末尾**追加这一段：

```toml
[model_providers.gatewayone]
name = "Gateway One"
base_url = "http://100.x.y.z:8317/v1"
env_key = "GATEWAYONE_API_KEY"
wire_api = "chat"
```

**第 4 步**：设置环境变量（把 Key 放环境变量里，不写进配置文件）：

```bash
# Mac / Linux：加到 ~/.zshrc 或 ~/.bash_profile 里，然后重开终端
export GATEWAYONE_API_KEY="你的KEY"
```

```powershell
# Windows PowerShell（永久生效）
[Environment]::SetEnvironmentVariable("GATEWAYONE_API_KEY","你的KEY","User")
```

**第 5 步**：跑 `codex` 试试。

> 注意这里 `base_url` **要带 `/v1`**。

---

### 4.3 dsh（DeepSeek Harness）

**第 1 步**：打开 dsh → 设置 → 模型 → 添加提供方 → **自定义模型 API**

**第 2 步**：按这个表填：

| 字段 | 填什么 |
|---|---|
| 标识 | `gatewayone`（小写字母开头，随便起） |
| 协议 | **Anthropic Messages** |
| 地址 | `http://100.x.y.z:8317`（不带 /v1） |
| API Key | 你的 Key |
| 模型 | `opus`（想加更多就继续填 `sonnet`、`haiku`、`gpt`…） |

**第 3 步**：保存，选一个 `opus` 开个新会话。能回话就成了。

> 如果你在 dsh 里选的是「OpenAI Chat Completions」协议，地址就要改成 `http://100.x.y.z:8317/v1`。

---

### 4.4 Chatbox（手机 + 电脑都能用，最推荐给非程序员）

**第 1 步**：装 Chatbox
- iPhone/iPad：App Store 搜 **Chatbox**
- Android：Play 商店搜 **Chatbox**
- Mac / Windows：[chatboxai.app/zh/download](https://chatboxai.app/zh/download)

**第 2 步**：打开 App → 设置 → 模型提供方 → **添加自定义提供方**

**第 3 步**：按这个表填（**这张表最容易填错，仔细看**）：

| 字段 | 填什么 | 容易错的地方 |
|---|---|---|
| 名称 | `Claude` | 随便 |
| API 模式 | **OpenAI API 兼容** | 不要选 Anthropic |
| API 密钥 | 你的 Key | 40 个字符，别多空格 |
| **API 主机** | `http://100.x.y.z:8317/v1` | **必须带 `/v1`** |
| API 路径 | 保持默认 `/chat/completions` | 不要改 |
| 改善网络兼容性 | **关闭** | 开了会变慢 |

**第 4 步**：看下面那行「预览」，应该是：

```
http://100.x.y.z:8317/v1/chat/completions
```

**如果不是这个，说明 `/v1` 位置错了**——主机那里补上 `/v1`，路径保持 `/chat/completions`。

**第 5 步**：点「检查」（如果有），应该显示成功。然后点「获取」拉模型列表，会看到 72 个。**选 `opus`**。

拉不到就点「+ 新建」手动填模型名 `opus`，一样能用。

**第 6 步（重要）**：进设置 → 找到「最大回复长度 / max tokens」→ **改成 4000**。

> 默认值通常是 1000 或 2000。这个模型每次回答前会先"思考"，思考也占 token。设小了会出现**回答是空的**或者**说到一半断掉**。

---

### 4.5 Cherry Studio

**第 1 步**：打开 Cherry Studio → 设置 → 模型服务 → 左下角 **添加**

**第 2 步**：

| 字段 | 填什么 |
|---|---|
| 名称 | `Gateway One` |
| 类型 | **OpenAI** |
| API 地址 | `http://100.x.y.z:8317/v1` |
| API 密钥 | 你的 Key |

**第 3 步**：点「管理」→ 手动添加模型 `opus`、`sonnet`、`haiku`（或者点「获取模型列表」）

**第 4 步**：回到聊天界面，左上角选 `opus`，开聊。

---

### 4.6 Cline / Roo Code（VS Code 插件）

这两个插件配置一模一样。

**第 1 步**：VS Code 里装 Cline 或 Roo Code，打开侧边栏

**第 2 步**：点齿轮（设置）

**第 3 步**：

| 字段 | 填什么 |
|---|---|
| API Provider | **OpenAI Compatible** |
| Base URL | `http://100.x.y.z:8317/v1` |
| API Key | 你的 Key |
| Model ID | `opus` |

**第 4 步**：保存，随便让它做点事。

---

### 4.7 Cursor / Trae

**Cursor**：Settings → Models → OpenAI API Key 那里展开 → **Override OpenAI Base URL** 填 `http://100.x.y.z:8317/v1`，Key 填你的，然后 Add model 填 `opus`。

> Cursor 的自定义模型功能对第三方端点支持不稳定，如果 Cursor 里不好使，是 Cursor 的问题，不是网关的——用 Claude Code 或 Cline 更稳。

**Trae**：设置 → 模型 → 添加模型 → 选 **OpenAI 兼容** → Base URL `http://100.x.y.z:8317/v1` → 模型名 `opus`。

---

### 4.8 Python 代码里用

**用 OpenAI SDK**（推荐，大部分库都支持）：

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://100.x.y.z:8317/v1",
    api_key="你的KEY",
)

r = client.chat.completions.create(
    model="opus",
    messages=[{"role": "user", "content": "你好"}],
    max_tokens=4000,          # 一定要给够，见第 5 章
)
print(r.choices[0].message.content)
```

**用 Anthropic SDK**（能拿到 thinking 块）：

```python
from anthropic import Anthropic

client = Anthropic(
    base_url="http://100.x.y.z:8317",   # 注意：不带 /v1
    api_key="你的KEY",
)

r = client.messages.create(
    model="opus",
    max_tokens=4000,
    messages=[{"role": "user", "content": "你好"}],
)
print(r.content)
```

---

### 4.9 别的工具（Open WebUI / Dify / n8n / FastGPT…）

都是同一套：

```
Base URL:  http://100.x.y.z:8317/v1
API Key:   你的KEY
Model:     opus
```

任何支持「OpenAI 兼容接口」的工具都能接。

---

## 5. 三条铁律

违反任何一条都会出问题，而且报错信息不会告诉你原因。

### 铁律一：`/v1` 的位置不能错

| 协议 | 地址 | 例子 |
|---|---|---|
| Anthropic 原生 | **不带** `/v1` | `http://100.x.y.z:8317` |
| OpenAI 兼容 | **必须带** `/v1` | `http://100.x.y.z:8317/v1` |

- 该带没带 → `404 not found`
- 不该带却带了 → `/v1/v1/messages` → `404`

### 铁律二：`max_tokens` 至少给 2000，建议 4000

这个模型每次回答前会先"思考"，**思考消耗的 token 也算在 max_tokens 里**。给太少会这样：

| max_tokens | 结果 |
|---|---|
| 30 | **正文一个字都没有**（全被思考吃掉了） |
| 60 | 说到一半断掉：`我是 Claude Code，Anth` |
| 2000 | 正常 |
| 4000 | 安全 |

在客户端设置里找「最大回复长度」「Max Tokens」「max_tokens」之类的项，改成 4000。

### 铁律三：模型名填短的那个

填 `opus`，不要填 `claude-opus-5-5`。

两个都认，但短名是**固定指向当前最强的**，以后升级不用改配置。真实模型名会变，别名不会。

---

## 6. 出问题了查这里

| 现象 | 最可能的原因 | 怎么修 |
|---|---|---|
| 一直转圈 / 无法连接 | Tailscale 没连上 | 打开 Tailscale App，确认显示 Connected 且能看到 `100.x.y.z` |
| `401 Unauthorized` | Key 错了 | 重新复制，确认是 40 个字符、没有前后空格 |
| `404 not found` | `/v1` 位置错 | 看第 5 章铁律一 |
| 回答是空的 | max_tokens 太小 | 改成 4000 |
| 回答说到一半断 | 同上 | 同上 |
| 很慢（15-30 秒） | 正常，冷启动 | 第二次会快很多（1-2 秒） |
| 第一次特别慢后面快 | 连接复用生效了 | 正常，不用管 |
| `429 Too Many Requests` | 短时间内请求太多，撞了额度窗口 | 等几分钟；这是共享额度，不是故障 |
| `usage_limit_reached` | 那个平台的账号这个周期额度用完了 | 换别的模型（比如 `grok`）先顶着，等重置 |
| 模型说自己是「Claude Code」 | 正常现象 | 网关照实说是伪装成官方 CLI 才连得上，不影响能力 |
| 之前能用现在不能 | 你的 Key 可能被删了 | 找管理员要新的 |
| 换了个 WiFi 就连不上 | 正常，Tailscale 会自动重连 | 等 10 秒，或重启 Tailscale |

---

## 7. 常见问题

**Q：这个要不要花钱？**
A：你不需要付。额度是服务器主人自己的订阅，大家共享。

**Q：会不会把我的聊天内容传出去？**
A：会经过服务器中转，服务器上**有请求日志**（包含完整对话内容）。所以：**不要在这里聊密码、身份证号、银行卡这类东西**。

**Q：为什么有时候特别慢？**
A：两个原因：① 第一次连接要握手，15-30 秒正常，之后变快；② 服务器在国外，链路上有波动。

**Q：额度用完了怎么办？**
A：换一个模型。四家平台的额度是分开的，`opus` 用完了可以先用 `sonnet` 或 `grok`。

**Q：能不能给更多人用？**
A：要新的 Key 找管理员，一台设备一把。

**Q：我能在公司电脑上用吗？**
A：能，只要公司网络允许装 Tailscale。有些公司的网络会拦这类工具，那就没办法了。

**Q：服务器关机了怎么办？**
A：等管理员开机。这台机器是常开的，但断电、系统更新后需要有人处理。

**Q：手机上能用官方的 Claude App 吗？**
A：不能。官方 App 不允许改服务器地址。手机请用 **Chatbox**（第 4.4 节），体验接近。

**Q：我想看自己用了多少额度？**
A：浏览器打开 `http://100.x.y.z:8899`，输入管理员给你的面板密码。里面有实时用量、四家平台的剩余额度、每次请求的延迟。

---

## 附：一页速查

```
地址（Anthropic 协议）  http://100.x.y.z:8317
地址（OpenAI 协议）     http://100.x.y.z:8317/v1
控制台                  http://100.x.y.z:8899

模型名（挑一个填）：opus  sonnet  haiku  gpt  codex  grok  grok-fast  gemini  gemini-flash
默认推荐：opus
要快：haiku
Claude 用完了：grok 或 gpt

Key：sk-local- 开头 40 字符，找管理员要
max_tokens：设 4000
前缀：装了 Tailscale 才能连
```

---

*如果卡住了，把「现象 + 你填的地址 + 报错原文」发给管理员，不要只说「连不上」。*
