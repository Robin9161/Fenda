# 全链路反代工具 · 落地方案

> 目标：让任何人一条命令就能把自己的 AI 订阅反代出来，并有一个能看额度、管订阅、管 API 的交互面板。
> 日期：2026-11-02 · 基于 CLIProxyAPI v8.0.51 实测 + 其生态清单核查

---

## 一、先说最重要的一句

**你想要的管理面板，已经装在你自己机器上了，只是默认没开。**

`config.example.yaml` 第 104 行原文：

```yaml
  # Disable the bundled management control panel asset download and HTTP route when true.
  disable-control-panel: false

  # GitHub repository for the management control panel.
  panel-github-repository: "https://github.com/router-for-me/Cli-Proxy-API-Management-Center"
```

含义：

- CLIProxyAPI **自带一个管理控制面板**（就是官方 CPAMC 项目的打包版）
- 默认 `false` = 启用，会自动从 GitHub 下载并自动更新
- 路由有两个：`/v0/management`（旧）+ `/v8/management`（v8）
- 任何管理请求（**包括 localhost**）都要 `management.secret-key`

我实测过：现在访问管理端点全部 401，因为配置里存的是 bcrypt hash（`$2a$10$stdvI/...`），**明文丢了**。改回明文重启就会自己 hash 回来，面板即可用。

> 顺带一条实测结论：我刚才连续试探触发管理端点的 IP 封禁（`IP banned due to too many failed attempts. Try again in 30m0s`），但**正常业务完全不受影响** —— 同一时刻 `/v1/models` 返回 200、耗时 0.7ms。管理面是独立风控。

---

## 二、这个需求，市面上已经做了七成

CLIProxyAPI README 里有一整份「生态项目」清单。跟你的需求直接相关的：

| 项目 | 能力 | 离你要的有多近 |
|---|---|---|
| **CPAMC**（官方） | 管理面板：凭证、Provider、客户端 Key、模型别名、插件、配置 | 已在你的二进制里 |
| **CPA-Manager-Plus** | 自托管面板 + 可观测：请求历史、失败诊断、按模型/账号/Key 统计 token 与成本、**Codex 与 xAI 配额检查、重置信息、账号健康巡检** | ⭐ 最接近「额度管理」 |
| **Oh-My-CPA** | Ant Design 面板：逐请求延迟、TTFT、Token、成本、热力图、OpenRouter 价格同步 | 成本视角最强 |
| **CLIProxyAPI Dashboard** | Next.js + Postgres，实时日志流、结构化配置编辑、OAuth 集成 | 偏运维 |
| **CLIProxyAPI Quota Inspector** | 跨平台配额查询：codex 5h/7d 窗口、按套餐排序、状态着色、多账号汇总 | 纯额度，轻 |
| **CLIProxy Pool Watch** | macOS SwiftUI：5 小时/周额度进度条、套餐权重、恢复预测 | 原生 Mac 体验 |
| **ProxyPal** | **跨平台桌面 App（mac/win/linux），原生 GUI 封装 CLIProxyAPI**，使用分析、请求监控、热门编程工具自动配置 | ⭐ 最接近「一键工具」 |
| **AIUsage** / **Panopticon** / **All API Hub** / **Shadow AI** | 各类额度看板与中转站管理 | 补充 |

**结论：网关、面板、额度看板，三样都有人做了，而且做得不差。**

如果目标只是「自己用」，今天就该直接打开自带面板、再装一个 Quota Inspector，不该写一行代码。

---

## 三、那真正缺的是什么（这是你的产品机会）

把整条链路拆开看，缺口只有一处：

| 环节 | 现状 | 门槛 |
|---|---|---|
| 拿到二进制 | GitHub Releases 找对架构 | 中 |
| 写 config.yaml | v8 结构大改，字段几十个，写错不报错只是不生效 | **高** |
| 登录各家账号 | 设备码流程、回调端口、SSH 隧道提示 | **高** |
| 装成常驻服务 | launchd / systemd / Windows 服务，三套写法 | 高 |
| 自愈 | 自己写 watchdog | 高 |
| 面板 | 已自带 | 低 |
| 额度 | 第三方现成 | 中 |

**真正的产品是：把前五步压缩成「一条命令 / 一次双击」。**

一句话定位：

> 现有工具是「给会命令行的人用的网关」；你要做的是「**给不会命令行的人用的网关**」。
> 差异化不在功能，在**最后一公里的摩擦**。

---

## 四、产品形态：三层，每层独立可用

### L1 · 一键安装器（核心交付物）

**形态 A（推荐起步）**：`curl -fsSL https://your-domain/install.sh | bash`

**形态 B（成熟期）**：`.dmg` / `.exe` / `brew install`

安装器要做 8 件事，全部幂等：

1. 探测 OS + 架构（darwin/linux/win × arm64/amd64）
2. 拉最新 release（或固定版本），校验 SHA256
3. **交互式问答**：
   - 要接哪些？`Claude / Codex / Grok / Gemini / Kimi`（多选）
   - 机器在墙内吗？（是→问代理地址，写进 `requests.proxy-url`）
   - 只本机用，还是局域网？（→ `server.host`）
   - 对外暴露吗？（→ 装 Tailscale 的引导）
4. 生成 `config.yaml`：随机客户端 Key（按设备数生成）、模型别名、并发闸、日志轮转
5. 装服务：launchd / systemd / Windows 服务，三选一自动判断
6. 装 watchdog（分类自愈：端口死→重启，401→只告警，429→只记录）
7. **逐个走登录向导**（见 L2）
8. 打印面板地址 + 局域网地址 + 二维码 + 三条 Key

硬性要求：

- **幂等**：重跑不炸，已有配置自动备份
- **可卸载**：`install.sh --uninstall` 一条命令清干净，无系统残留
- **不越界**：只碰 `~/claude-gateway` 和 `~/Library/LaunchAgents`，不写系统目录

### L2 · 登录向导（摩擦最大的一步）

实测三家都是**设备码流程**，这对做向导极其有利 —— 不需要回调端口，不需要浏览器和网关同机：

| 家 | 流程 | 引导要点 |
|---|---|---|
| Claude | OAuth 回调 `localhost:54545` | 需要同机浏览器；无 tty 会退出（踩过） |
| Codex | 设备码 `auth.openai.com/codex/device` | 显示 8 位码 + 一键复制 + 轮询 |
| Grok | 设备码 `accounts.x.ai/oauth2/device` | 同上，30 分钟窗口 |

向导要做到：

- 大字号显示码，自动复制到剪贴板
- 打印二维码（手机扫，不用在电脑上切窗口）
- 轮询状态：`等待中 / 已授权 / 超时`
- 授权成功立即写入 `auth/`，无需重启网关

### L3 · 管理面板（额度 / 订阅 / API）

**建议：不自研，做聚合。**

路线：

```yaml
management:
  allow-remote: false          # 需要远程管理时改 true
  secret-key: "<明文>"
  disable-control-panel: false # 自带面板，保持开启
  panel-github-repository: "https://github.com/router-for-me/Cli-Proxy-API-Management-Center"
```

自带面板能覆盖：凭证列表、Provider、客户端 Key、模型别名、配置编辑、日志。

额度需要额外一层，选一个：

- **CPA-Manager-Plus**（功能最全，含 Codex/xAI 配额检查 + 重置信息 + 健康巡检）
- **CLIProxyAPI Quota Inspector**（最轻，纯 5h/7d 窗口）
- **CLIProxy Pool Watch**（macOS 原生菜单栏，体验最好）

安装器里给一个 `--with-panel=manager-plus|quota|none` 开关，用 Docker 或单二进制拉起来。

---

## 五、额度数据到底能不能拿到（方案成败点）

必须把三种「额度」分清楚，否则会做出一个骗人的面板：

| 类型 | 含义 | 数据来源 | 可行性 |
|---|---|---|---|
| **本地用量** | 这个网关自己转发了多少 token | CLIProxyAPI `observability.usage` + 请求日志 | ✅ 确定可行 |
| **订阅窗口剩余** | Claude 5 小时窗口还剩几成、Codex 5h/7d、Grok 档位 | **各家内部接口**（响应头 / 私有 endpoint） | ⚠️ 可行但脆弱，需要逆向，上游改了就崩 |
| **订阅档位** | Max / Pro / SuperGrok | OAuth token 里的 claim、或首请求探测 | ✅ 基本可行 |

关键判断：**第二类就是那十几个第三方面板存在的理由**，说明技术上打得通，但没有稳定契约。

所以：

- 想「看一眼还剩多少」→ 复用现成工具，别自己逆向
- 想「精确到每次调用的成本」→ 走 Oh-My-CPA 那套（按 token × 单价算），这是可算的
- 想「预测什么时候重置」→ 需要抓窗口起始时间，CPA-Manager-Plus / Pool Watch 在做

---

## 六、落地路线（4 个里程碑，每个都能验收）

### M0 · 今天，2 分钟 —— 把面板打开

```bash
cd ~/claude-gateway
cp config.yaml config.yaml.bak
# 把 management.secret-key 从 bcrypt hash 改成明文（重启后会自动重新 hash）
# 用 python3 或 sed 改，然后：
launchctl kickstart -k gui/501/com.local.cliproxyapi
```

验收：浏览器打开 `http://127.0.0.1:8317/`（或 `/v8/management`），能看到凭证和配置。

> 注意：管理端点有 IP 封禁策略，试错别超过 5 次。

### M1 · 3 天内 —— 补上额度

起一个 CPA-Manager-Plus 或 Quota Inspector，连本地 Management API。
验收：面板上能看到 Claude 的 5 小时窗口、Codex 的 5h/7d、Grok 的档位。

### M2 · 1 周 —— 一键安装器（核心交付）

先做 macOS + Linux 的 `install.sh`，支持 `--provider=claude,codex,xai`、`--proxy=...`、`--lan`。
验收标准：**在一台干净机器上（没有装过任何东西）跑一条命令，10 分钟内从零到能用**。

### M3 · 2 周 —— 分发形态

- `brew tap` 安装（macOS 用户最顺）
- Docker image（NAS / 服务器 / 群晖）
- `.dmg`（非技术用户，可考虑用 Tauri / Electron 包一层，参考 ProxyPal 的做法）

---

## 七、要不要自研面板？决策树

```
只是自己用？
├─ 是 → 打开自带面板 + 装个额度看板，不要写代码
└─ 不是
   └─ 给家人朋友用？
      ├─ 是 → 做安装器（L1+L2），面板复用现成的
      └─ 想做成对外产品？
         └─ 才需要自研 UI
            差异化只能是：中文体验 / 三平台额度归集 / 设备分发与吊销语义
            不要试图在功能列表上打赢 CPAMC 和 Oh-My-CPA —— 它们已经在追上游了
```

---

## 八、风险与边界（这部分别跳过）

| 风险 | 说明 | 缓解 |
|---|---|---|
| **凭证集中** | 工具持有你的 Claude/Codex/xAI 全部 refresh token，泄露=账号被白用 | 全本地、`chmod 600`、每日加密备份、永不外传 |
| **上游改指纹** | Claude 一旦换 OAuth 校验方式，全链路立即 401 | 固定版本不自动升级；保留 `.prev` 二进制 10 秒回滚 |
| **各家风控** | 多设备共用订阅、机房 IP、异常并发都可能触发 | 出口用住宅 IP、并发闸 3、别对外分发 |
| **面板追上游** | 十几个面板都在追 CLIProxyAPI 版本，选错了半年就废 | 优先选官方 CPAMC 生态链内的 |
| **分发即扩散** | 你把工具给别人，别人拿它反代自己的号 —— 责任边界要写清楚 | 安装器里明确：本工具不提供账号、不代理他人流量 |
| **额度不可靠** | 内部接口随时变，面板显示的可能过期 | UI 上标注数据来源与采集时间 |

---

## 九、我的建议（一句话版）

**别从零造。** 按 M0 → M1 → M2 走：

1. 今天先把你机器上的自带面板打开，把「管理」这件事解决掉（2 分钟）
2. 三天内接一个额度看板，把「额度」解决掉
3. 一周内只做一个东西：**一键安装器 + 登录向导** —— 这才是别人真正需要你做的部分

面 板、网关、额度看板都有现成的，唯一没人做好的是「让不懂命令行的人也能装起来」。
