# Gateway One

把 Claude Max / ChatGPT / xAI Grok / Google Gemini 四家订阅,通过一个本地网关反代成标准 API,给你的所有设备用。

**不需要 API Key,用的是你自己的订阅。**

```
你的设备 ──Tailscale / 局域网──▶ 这台 Mac (127.0.0.1:8317) ──▶ 各家上游
                                    │
                                    ├─ 校验本地 Key
                                    ├─ 注入各家凭证
                                    └─ 伪装成官方客户端
```

## 它解决什么问题

各家订阅的额度只能在官方客户端里用。想在 dsh、Claude Code、Chatbox、Cline、Codex CLI 里用,只能买 API Key 另外付费。

这个网关让订阅额度变成 OpenAI / Anthropic 兼容的端点,一个地址接所有工具。

## 三个部件

| 部件 | 是什么 | 位置 |
|---|---|---|
| **网关** | CLIProxyAPI 二进制,监听 8317 | `installer/` 负责装 |
| **控制台** | Python 单文件 Web 面板,监听 8899 | `console/` |
| **客户端** | 原生 macOS app(Swift + WKWebView) | `app/` |
| 菜单栏 | 状态图标(Swift) | `menubar/` |

## 快速开始

```bash
# 需要:macOS 或 Linux、curl、tar、openssl
cd installer
bash install.sh
```

装完之后:

1. 打开面板 `http://127.0.0.1:8899`
2. 点「+ 添加账号」,选平台,按提示在浏览器里授权
3. 复制面板给你的 Base URL 和 Key,填进任意客户端

跨网络访问装 [Tailscale](https://tailscale.com),两端登录同一账号即可。

## 四家的接入方式

| 平台 | 授权方式 | 额度来源 |
|---|---|---|
| Claude Max | OAuth(浏览器回调) | 订阅的 5 小时 / 7 天窗口 |
| ChatGPT | 设备码 或 复用 `~/.codex/auth.json` | Plus/Pro 套餐 |
| xAI Grok | 设备码 | SuperGrok 订阅 |
| Google Gemini | Google OAuth(Antigravity) | Antigravity 额度 |

## 关键约束(实测)

- **`/v1` 的位置搞反必 404**:Anthropic 协议不带 `/v1`,OpenAI 协议带 `/v1`
- **`max_tokens` 必须 ≥2000**:Opus 每轮先思考,思考计入 max_tokens,给少了正文为空
- **模型名跨平台会串**:`claude-opus-5-5-high` 同时存在于 anthropic 与 antigravity 两个通道,需要别名抢占(见 `docs/`)
- **Windows 必须设 `NO_PROXY`**:否则系统代理会拦 Tailscale 的 `100.64.0.0/10` 网段
- **日志含完整对话内容**:分享或备份前先清理

## 文档

- [`docs/USER-GUIDE.md`](docs/USER-GUIDE.md) — 手把手接入指南(九个客户端)
- [`docs/README.md`](docs/README.md) — 运维手册
- [`docs/PLAN-v2.md`](docs/PLAN-v2.md) — 稳定性设计
- [`docs/PLAN-product.md`](docs/PLAN-product.md) — 产品化路线
- [`console/README.md`](console/README.md) — 控制台接口与排障
- [`installer/README.md`](installer/README.md) — 安装器细节

## 安全

- 凭证只留本机,其他设备只拿可吊销的 `sk-local-*` Key,一台设备一把
- 面板可设访问密码(跨设备访问前必开)
- 全链路走 Tailscale 的 WireGuard,不暴露公网
- **`.gitignore` 已排除凭证、日志、配置**;提交前务必再扫一遍

## 声明

自用工具。各家订阅的服务条款请自行确认;本仓库不分发任何凭证,也不代管他人账号。
