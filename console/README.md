# Gateway One · 控制台（Console）

一个跑在本机的可视化控制台，把「订阅账号 → OAuth 凭证 → 网关 → 客户端 Key → 各设备」这条链路的每一环都摆在一个页面上，可看、可点、可测。

- **地址**：<http://127.0.0.1:8899/>（只绑本机回环，不对外）
- **实现**：Python 3 标准库后端 + 单文件前端，**零依赖**（不需要 node / npm / 任何 pip 包）
- **常驻**：launchd `com.local.cgconsole`，KeepAlive，崩了 10 秒内自动拉起

---

## 一、它显示什么

### 1. 链路图（页面顶部）

左中右三段，用虚线连起来，任何一段断了都能一眼看出：

| 位置 | 内容 | 数据来源 |
|---|---|---|
| 左 · 订阅 | Claude Max / ChatGPT / xAI Grok，各家邮箱、凭证剩余有效期、文件权限、是否禁用 | `auth/*.json` |
| 中 · 网关 | 监听端口、PID、uptime、内存、二进制版本 | 端口探测 + `launchctl` |
| 右 · 客户端 | 三把 `sk-local-*` Key，归属设备，可一键复制完整值 | `config.yaml` |

**点击左侧任一订阅卡**，会展开该凭证的明细：文件名、上次刷新、到期时刻、是否带 refresh_token、账号 ID、文件大小与权限位。

### 2. 模型矩阵

网关当前能路由的全部模型（实测 **61 个**），按来源分四组：

- `别名` — 你日常填的短名：`opus` `sonnet` `haiku` `gpt` `codex` `grok` `grok-fast`
- `Claude` — claude-opus-5-5 / claude-sonnet-5-5 / claude-fable-5-1 …（19 个原名）
- `Grok` — grok-4.7 / grok-4.6 / grok-imagine-* …（21 个）
- `Codex` — gpt-6-astra / gpt-6.1-sol / gpt-5.6-* / gpt-image-* …（21 个）

**点任意一张卡 = 真发一次请求**（`POST /v1/chat/completions`，`max_tokens: 256`），返回结果直接贴在卡片上：

```
opus        ✓ 2.51s · claude-opus-5-5
codex       ✗ 429 usage_limit_reached
```

搜索框可实时过滤；分组标签可切。

### 3. 额度与故障

从 `logs/error-*.log` 里反推出真实发生过的四类问题，按「类型 + 模型」聚合：

- **额度用尽** — 账号侧 `usage_limit_reached`，带 `reset_seconds` 倒计时
- **模型冷却** — 该模型被整体冷却中
- **限流 429** — 撞到 5 小时窗口或周限额
- **鉴权失败** — 凭证侧 `authentication_error`

> ⚠️ **诚实说明**：Claude / ChatGPT / xAI 三家都**没有**开放「查询余额」的公开接口。这一栏不是"实时余额"，而是**从真实报错反推的已发生事实**——比猜准，但不是预测。想主动看剩余额度只能去各家网页端。

### 4. 事件流

`watchdog.log` + `backup.log` 合并成一条时间线，连续相同的事件自动折叠成 `×18`：

```
15:17:26 · backup     OK: auth-2026-11-02.enc 8.0K
14:13:45 → 14:14:06 · backup   OK: auth-2026-11-02.enc 4.0K  ×2
14:05:50 → 15:22:19 · watchdog OK: 200  ×18
14:05:50 · watchdog   PERM-FIX: auth-dir→700 <...>.json→600
10:51:51 · watchdog   AUTH-FAIL: 401        ← 红点
```

### 5. 运行明细 + 接入客户端

- **运行明细**：监听地址、局域网地址、出口代理、配置版本、重试次数、管理 API 状态、凭证数、调用样本量、成功率、延迟 p50/p95
- **接入客户端**：六种客户端的 Base URL，点「复制」直接拿走，**已按协议标好带不带 `/v1`**

---

## 二、它能做什么（都是真的会执行）

| 按钮 | 实际动作 | 影响 |
|---|---|---|
| 重启网关 | `launchctl kickstart -k gui/501/com.local.cliproxyapi` | 中断 2–3 秒 |
| 停止 | `launchctl bootout` | 所有客户端立刻断连；再点「重启」或手动 bootstrap 才恢复 |
| 备份 | `bash ~/claude-gateway/backup.sh` | 生成 AES-256 加密包（当天幂等，重复点不会刷出一堆） |
| 复制 Key | `POST /api/key {i}` → 本机回明文 | 只经回环，不出网 |
| 点模型卡 | 真发一次对话请求 | **消耗真实额度** |

---

## 三、启动与维护

```bash
# 状态
launchctl list | grep cgconsole
lsof -nP -iTCP:8899 -sTCP:LISTEN

# 重启控制台
launchctl kickstart -k gui/$(id -u)/com.local.cgconsole

# 停止 / 恢复
launchctl bootout   gui/$(id -u)/com.local.cgconsole
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.local.cgconsole.plist

# 日志
tail -f ~/claude-gateway/logs/console.log
```

改端口：把 plist 里的 `CG_UI_PORT` 换掉再 bootstrap。
改网关目录：同理换 `CG_DIR`。

---

## 四、它不能做什么

1. **不能查各家实时余额** —— 原因见上。
2. **不能替你完成 OAuth 登录** —— 设备码/浏览器授权仍需人在场（`-cli-proxy-api -xai-login` 等在终端跑）。
3. **不能改 config.yaml** —— 目前只读。改配置仍用编辑器 + 重启网关。
4. **不监听 0.0.0.0** —— 只有本机能打开。要让手机看，得走 Tailscale 或反向代理，且必须加认证。

---

## 五、故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 页面白屏 / "控制台后端断开" | 8899 没进程 | `lsof -iTCP:8899` → 无则 `kickstart` |
| 模型列表空 + "网关未返回模型" | 8317 没起，或 Key 不对 | 先 `curl -H "Authorization: Bearer sk-local-..." http://127.0.0.1:8317/v1/models` |
| 所有时间显示 "刚刚" | 后端返回的时间没带时区 | 检查 `server.py` 顶部是否被硬设了 `CG_TZ` |
| 点测试一直转 | 请求真的慢（冷启动 20–40 秒） | 等，后端超时是 180 秒 |
| 额度区空但确实没额度了 | 只有**报过错**的才会出现在这里 | 属于设计如此 |

---

## 六、文件清单

```
~/claude-gateway/console/
├── server.py     后端（标准库，约 500 行）
└── index.html    前端（单文件，无外链）

~/Library/LaunchAgents/com.local.cgconsole.plist   常驻配置
~/claude-gateway/logs/console.log                  运行日志
```

后端对外只有这些端点，全部本机：

```
GET  /                 页面
GET  /api/overview     一次性取全部数据（前端 5 秒轮询）
GET  /api/events       事件流
GET  /api/raw          原始日志尾巴
POST /api/test         真发一次模型请求
POST /api/service      重启 / 停止 / 启动网关
POST /api/backup       触发一次加密备份
POST /api/key          取第 i 把客户端 Key 的明文
```

**凭证 token（access/refresh/id_token）在任何响应里都不会出现**，页面只拿到邮箱、到期时间、权限位这些元信息。

---

## 第一批：让它从「能看」变成「能用」

这一批解决的是四个真正会卡住人的地方。

### 1. 客户端一键接入

面板会自动探测本机这三样东西，探到了就给一个按钮：

| 客户端 | 探测依据 | 写入位置 | 可还原 |
|---|---|---|---|
| Claude Code | `~/.claude/settings.json` 存在 | 该文件的 `env` 段（`ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_MODEL` / `ANTHROPIC_SMALL_FAST_MODEL`），并把它自己加进 `NO_PROXY` | ✅ 备份为 `settings.json.go-bak` |
| Codex CLI | `~/.codex/config.toml` 存在 | 追加 `model` / `model_provider` / `[model_providers.gatewayone]` 三段 | ✅ 备份为 `config.toml.go-bak` |
| dsh | `~/.dsh` 存在 | 不改文件，弹窗给六步填法（配置存在 GUI 里，磁盘上没有可安全改的明文位置） | — |

写入后**会回读校验**，不一致会明确告诉你。点「还原」回到接入前的状态（实测与原文件逐字节一致）。

⚠️ Codex 用的是 `env_key`，不把 Key 写进 toml。所以接入后还要设一次环境变量：

```bash
export GATEWAYONE_API_KEY="sk-local-..."
```

面板会把这条命令自动复制到剪贴板。

### 2. 设备 Key 管理

每把 Key 一行：复制 / 改名 / 删除，底部「+ 新增 Key」。

- 备注存在 `config.yaml` 的行尾注释里（`- "sk-local-..." # 本机 / 主人`），**不引入第二份配置**
- 增／删会重写 `access.api-keys` 段并**自动重启网关**（实测 0.7–0.8 秒就绪），然后回读确认 PID 变了才算成功
- 删到只剩最后一把会被拒绝（否则所有客户端立刻 401）
- 上限 20 把

⚠️ 网关日志不记录是哪把 Key 发的请求，所以**做不了「按设备统计用量」**——除非改上游。这里不承诺做不到的事。

### 3. 凭证失效 → 一键重登

订阅卡上的状态灯变成下面三种时，卡片右侧会直接出现按钮：

| 状态 | 显示 | 按钮 |
|---|---|---|
| 正常 | 有效（绿） | 无 |
| 过期 / 需刷新 | 需刷新（黄） | 重新登录 |
| 被禁用 | 已禁用（红） | 重新登录 |
| 没接入 | 未接入 | 接入（高亮） |

点下去直接进图形化登录，和「+ 添加账号」完全一样的流程——不用回终端。

### 4. 设置页

右上角「设置」：

| 项 | 改什么 | 生效方式 |
|---|---|---|
| 访问范围 | `server.host`（`127.0.0.1` 仅本机 / `0.0.0.0` 局域网） | 保存后自动重启网关 |
| 网关端口 | `server.port`（1024–65535） | 同上 |
| 日志调试 | `observability.logs.debug` | 同上 |
| 自动备份 | 只读显示（由 `backup.sh` 是否可执行决定） | — |
| 控制台端口 | 只读显示 | — |

保存后会等**新 PID 出现**才报成功，不会再出现「显示保存成功但其实没重启」这种谎报。

### 5. 首次引导

一个凭证都没有时，链路图左侧不再是空白，而是一张引导卡：「还没有接入任何账号 + 接入第一个账号」。

---

## 新增的接口

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/clients` | 探测三个客户端：是否存在 / 是否已接入 / 当前指向 |
| GET | `/api/settings` | 读设置 |
| GET | `/api/keys` | 列 Key（含备注） |
| POST | `/api/clients/wire` | 写入客户端配置（`{id}`） |
| POST | `/api/clients/unwire` | 从 `.go-bak` 还原（`{id}`） |
| POST | `/api/keys/add` | 新增（`{label}`）→ 重启网关 |
| POST | `/api/keys/del` | 删除（`{key}`）→ 重启网关 |
| POST | `/api/keys/label` | 改备注（`{key,label}`），不重启 |
| POST | `/api/settings` | 改设置（`{host?,port?,log_debug?}`）→ 重启网关 |

安全边界不变：所有接口只绑 `127.0.0.1`；凭证的 `access_token` / `refresh_token` / `id_token` 仍然永不出现在任何响应里。


---

# 第二批：让它像个基础设施

第一批解决「会把人卡住」，第二批解决「不出事没人看，出事要立刻知道」。

## 一键体检

顶栏「体检」。跑一次 5–20 秒（取决于三家模型响应速度），共 13 项：

| # | 检查 | 判据 |
|---|---|---|
| 1 | 网关进程 | 端口有人在听，记下 PID |
| 2 | launchd 托管 | 网关与 watchdog 两个 job 都注册了 |
| 3 | 每份凭证 | 是否禁用、是否过期、剩余多久 |
| 4 | 出口网络（直连） | `curl --noproxy '*' api.ipify.org` 拿真实出口 IP |
| 5 | 出口代理配置 | config 里的 `requests.proxy-url` |
| 6–8 | 三家各真打一发 | Claude 用 `haiku`、Grok 用 `grok`、ChatGPT 用 `gpt-6-sol`；没接入就跳过 |
| 9 | 磁盘余量 | `df` |
| 10 | 日志体积 | 超过阈值提示 |
| 11 | 加密备份 | 最近一份超过 48 小时报提醒 |
| 12 | 结论 | 统计问题数与提醒数 |

**额度用尽不算故障**：`usage_limit_reached` / `model_cooldown` / `rate_limit` 会被归成「提醒」而不是「问题」，并给出「等窗口重置或换号」。这是刻意的——把账号额度问题当成网关故障会让人去查错方向。

底部「复制报告」把整份结果变成纯文本，贴到群里就能让人看懂。

## 主动通知

后台每 5 分钟扫一次异常，**只对没通知过的报一次**，并且两条通知之间至少隔 10 分钟。

- 设置 → 系统通知 → 「发一条测试通知」可以先验一下通道是否通
- 通知走 macOS 通知中心（`osascript display notification`），归属发起的宿主程序
- 如果一条都没收到，去「系统设置 → 通知」里给对应宿主（终端 / 脚本编辑器）打开权限

## 面板加锁

设置 → 访问密码。

- 开启后，从别的设备打开面板要输密码；本机浏览器登录一次记 7 天（`go_sess` cookie，HttpOnly + SameSite=Strict）
- 密码存 `~/claude-gateway/.ui-password`，格式是 `salt$pbkdf2_sha256(200000 轮)`，**不是明文**
- 忘记密码：`rm ~/claude-gateway/.ui-password` 然后 `launchctl kickstart -k gui/$(id -u)/com.local.cgconsole`，不需要改任何配置
- 只绑 `127.0.0.1` 时加锁意义不大；**一旦把访问范围改成局域网，必须先设密码**

## 菜单栏小程序（GatewayOneMenubar.app）

原生 Swift（`swiftc -O -swift-version 5`，113KB），不吃常驻内存，10 秒轮询一次 `/api/overview`：

- 图标颜色：绿=正常 / 黄=有提醒 / 红=网关挂了或控制台连不上
- 菜单里能看到：网关状态与 uptime、三家凭证剩余时间、对话次数与成功率、异常项子菜单
- 动作：打开控制台 / 重启网关 / 暂停监控 / 退出
- 由 `watchdog.sh` 保活（每 5 分钟检查一次，没在跑就 `open -g` 拉起）

**已知限制（重要）**：macOS 会把新加的菜单栏图标折叠进左侧的 `«` 溢出区，尤其是菜单栏图标本来就很满的时候。这是系统行为，没有 API 能强制它常驻可见。

如果看不到，两种办法：
1. 点菜单栏最左边的 `«`，里面能看到 `GW`
2. 按住 `⌘` 把它从 `«` 里拖到菜单栏可见位置（拖一次，系统会记住）

## 第二批新增接口

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/doctor` | 体检结果（含 running / items / verdict） |
| GET | `/api/doctor/text` | 体检报告纯文本 |
| POST | `/api/doctor/start` | 开始体检 |
| POST | `/api/notify/test` | 发一条测试通知 |
| GET | `/api/auth/state` | 密码是否启用 |
| POST | `/api/auth/set` | 设置或修改密码（≥4 位） |
| POST | `/api/auth/off` | 关闭密码（需当前密码） |
| POST | `/api/auth/login` | 登录，成功后下发 7 天 cookie |
| POST | `/api/auth/logout` | 清掉本地会话 |

## 第二批踩到的坑

| # | 问题 | 教训 |
|---|---|---|
| 1 | 残留代码 `const _unused = [...])` 多一个 `)` | 整个 `<script>` 语法错误 → 所有函数 undefined → 页面永远停在「连接中…」。改完必须验证 `new Function(code)` 能过 |
| 2 | 用端口判断重启成功 | 旧进程还占着端口时会立刻误报成功；必须先记旧 PID，轮询到 PID 变化 |
| 3 | 后台线程插进 `try:` 块 | 缩进错一层就 SyntaxError，改完先 `ast.parse` |
| 4 | `/api/auth/off` 不在白名单 | 关闭密码这个动作本身需要已登录 → 死锁；它自己校验密码，应该放行 |
| 5 | launchd 装 GUI 程序报 `Input/output error (5)` | `launchctl bootstrap` 对连 WindowServer 的 GUI app 不稳；改用 `watchdog` 里 `open -g` 保活 |
| 6 | 菜单栏图标看不见 | 不是崩溃——`statusItem.button?.window.frame` 有效、`isVisible=true`，是被系统折叠进 `«` |


---

## 第三批：把「看不见的额度」和「改不了的配置」补上

### 1. 用量窗口（按本网关日志推算）

Claude Max 是 **5 小时滚动窗口**：窗口从你这次窗口内的**第一条对话请求**开始计时，满 5 小时后重置。

面板右栏的「用量窗口」卡做三件事：

- **当前窗口**：`已用 X / 5 小时` + `剩 Y`，窗口走完就写「窗口已走完，下一条请求会开启新窗口」
- **24 小时热力**：每根柱子是一个整点，鼠标悬停显示「时间 · N 次」；蓝色越亮代表请求越密
- **口径声明**：卡片上直接写着「按本网关日志推算，只含经过这里的请求；网页版与官方客户端的量看不到」

> **这一条必须说清楚**：三家都不开放余额查询接口。面板给你的是**本网关实际发生过多少次调用**，不是账户剩余额度。
> 你在 claude.ai 网页版、官方 App、别的机器上用的量，它一概不知道。
> 所以它回答的是「我这个窗口用了多少」，不是「我还剩多少」。

实现上有一个必须踩过的坑：早期日志是 plist 里 `TZ=Asia/Tokyo` 写出来的（比本机快 9 小时），直接解析会得到「未来时间」，窗口起点算错、显示 `elapsed: 0`。
`_log_ts()` 里做了检测：时间戳比现在晚 10 分钟以上，就减 9 小时。

### 2. 模型别名可视化编辑（顶栏「高级」）

以前改别名要编辑 `config.yaml` 的 `model-alias` 段再重启网关。现在：

- 三个平台分组显示现有别名（`claude-opus-5-5 → opus` 这样一行一条）
- 模型名输入框带 `datalist`，从网关实际返回的 61 个模型里选，不会拼错
- 支持加一条、删一条
- 点「保存并重启网关」→ 写 `config.yaml`（先备份 `config.yaml.bak-alias-<时间戳>`）→ 重启 → 提示新 PID

保存时**保留原来的 display-name**。第一版没做这件事，直接把「Claude Opus 5.5」这种中文显示名覆盖成了 `opus`，是 diff 了配置文件才发现的。

### 3. 加密备份恢复（同一弹层）

- 列出 `~/claude-gateway-backups/*.enc`：文件名、大小、时间
- 点「恢复」→ 用钥匙串里的口令解密 → 校验 → 把现凭证另存 `auth.bak-<时间戳>` → 覆盖 → 重启
- 口令可以自动从钥匙串取，也可以手输（换机器时用）

**口令不对怎么判断**：`openssl enc -d` 用 AES-CBC 没有认证，错口令也照样输出一堆垃圾字节、不报错。
唯一可靠的判据是把解密结果喂给 `tar tzf`——错口令必然报 `Unrecognized archive format`。面板用的就是这个。

### 4. 同平台多账号

`auth/` 目录本来就支持同一平台放多个凭证文件，以前面板只显示第一个。现在：

- 卡片上写「共 N 个账号」
- 代表账号取「未禁用且未过期里剩余最久的」
- 点开明细逐条列出：账号 N/M、状态（剩余时长 / 需刷新 / 已禁用）、是否可续期、文件名

### 第三批新增接口

| 接口 | 说明 |
|---|---|
| `GET /api/usage` | 5 小时窗口 + 24 小时分布 |
| `GET /api/aliases` | 三个 channel 的别名 |
| `POST /api/aliases/save` | 整体写回并重启（约 0.8s） |
| `GET /api/backups` | 备份列表 |
| `POST /api/restore` | 解密校验后恢复 |

### 第三批踩到的坑

| 现象 | 根因 | 修法 |
|---|---|---|
| `GET /api/usage` 返 `not found` | 路由写进了 `do_POST`，请求是 GET | 三个 GET 路由挪回 `do_GET` |
| `AttributeError: type object 'datetime.datetime' has no attribute 'timedelta'` | `from datetime import timedelta` 之后又写了 `datetime.timedelta` | `grep timedelta` 才能搜到；`grep datetime.datetime.timedelta` 搜不到 |
| 窗口 `elapsed: 0`、`start` 在未来 | 早期日志按 Asia/Tokyo 写 | `_log_ts()` 检测未来时间减 9 小时 |
| 别名全是空 | `config.yaml` 里是 `claude:` 带冒号，`s in CHANNELS` 匹配不到 | `s.rstrip(":") in CHANNELS` |
| 保存别名后中文显示名变英文 | 前端把 `alias` 当 `display_name` 传 | `r.dataset.disp || alias` |
| `sh()` 传不了环境变量 | 函数签名只有 `(args, timeout)` | 加 `env=None`，内部 `dict(os.environ)` 合并 |

---

## 第四批：第四家平台（Gemini / Antigravity）

### 为什么是 Antigravity 而不是 API Key

Gemini 有三条接入形态，性质和另外三家不一样，必须先分清：

| 路径 | 额度来源 | 怎么登 | 是不是「反代你的订阅」 |
|---|---|---|---|
| **Antigravity** | Antigravity 订阅额度 | Google 账号 OAuth，浏览器点「允许」 | ✅ 是，与 Claude / Codex / Grok 同构 |
| AI Studio API Key | 独立 API 免费配额（有速率限制） | 去 aistudio 生成 Key 粘贴 | ❌ 是 API 配额，不是订阅 |
| Vertex AI | GCP 项目 + 结算账号 | 服务账号 JSON | ❌ 企业通道 |

**默认走 Antigravity。** API Key 只在「不想动 Google 账号、只要能跑就行」时作为备选（设置页里那一行）。

### 登录流程与一个关键坑

Antigravity 是 **Google 回调式授权**（`redirect_uri=http://localhost:51121/oauth-callback`），**没有设备码**。

```
点「+ 添加账号」→ 选 Gemini（Google 账号）
  → 自动生成授权链接并在浏览器打开
  → 选账号 → 点「允许」
  → 回调落到 localhost:51121，网关自动接住
```

**踩到的坑**：第一版复用 `claude/codex/xai` 那条 pty 通道，结果子进程立刻退出（日志只有版本行）。
原因：它不需要 stdin 交互，pty 反而干扰。修法是在 `_login_worker` 里分流：

```python
if platform == "antigravity":
    proc = subprocess.Popen([...], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
    mfd = proc.stdout.fileno()
else:
    mfd, sfd = pty.openpty(); ...   # 另外三家要读设备码、要喂 stdin
```

**通用结论**：在 Web 服务里驱动外部 CLI —— 需要交互（读提示、喂 stdin）才用 pty，纯输出抓取用普通管道更稳。

### 接入后的状态

- 凭证：`~/claude-gateway/auth/antigravity-<email>.json`
- 网关日志：`antigravity: obtained project ID aico...mers` + `auth file changed (CREATE): ..., processing incrementally`
- **热加载，不用重启**：模型数当场从 61 → 70
- Antigravity 的模型名与 API Key 通道**完全不同**，别照抄网上教程：

```
gemini-4-argon-high      ← 最强（实测回显 gemini-4-argon-trial）
gemini-3.8-flash-high    ← 快（实测回显 gemini-3.8-flash-n）
gemini-3-flash
gemini-3.1-flash-image
gemini-3.1-flash-lite
gemini-3.5-flash-lite
```

### 别名

在「高级 → 模型别名」里给 antigravity 分组加两条即可：

```yaml
antigravity:
  - {name: gemini-4-argon-high,   alias: gemini,       display-name: Gemini 4 Argon（最强）, fork: true}
  - {name: gemini-3.8-flash-high, alias: gemini-flash, display-name: Gemini 3.8 Flash（快）, fork: true}
```

实测：`model: gemini` → 回显 `gemini-4-argon-trial`，回复「我是谷歌研发的AI助手Gemini。」

### 四条通道总览

| 通道 | 登录方式 | 有效期 | 额度查询 |
|---|---|---|---|
| Claude Max | 浏览器 OAuth 回调（localhost:54545） | ~8 小时，自动刷新 | ✅ 响应头 `anthropic-ratelimit-unified-*` |
| ChatGPT / Codex | 设备码（15 分钟） | ~10 天 | ✅ `chatgpt.com/backend-api/wham/usage` |
| xAI Grok | 设备码（30 分钟） | ~6 小时，自动刷新 | ❌ 无公开接口 |
| Gemini / Antigravity | Google OAuth 回调（localhost:51121） | ~1 小时，自动刷新 | ❌ 无公开接口 |

### 关于 Cursor（不能一键接入）

CLIProxyAPI 的 8 个 channel 里没有 cursor，本机也没装 Cursor。Cursor 的额度是 IDE 内置订阅，走私有 `api2.cursor.sh` + 本地 token，没有对外 API。

**可行方向是反过来的**：Cursor 支持自定义 OpenAI Base URL —— 让它用 Gateway One 的模型（`http://<网关>:8317/v1` + `sk-local-...`），这样 Cursor 一个订阅能调四家的模型。

---

## 第五批：额度余量与状态球

### 额度是真数据，不是估算

控制台自己直连上游拿，不经过网关（CLIProxyAPI 把 ratelimit 头过滤掉了）：

| 平台 | 数据来源 | 能拿到什么 |
|---|---|---|
| Claude Max | `POST api.anthropic.com/v1/messages`（`max_tokens:1`）的响应头 `anthropic-ratelimit-unified-*` | 5 小时窗口与 7 天窗口的已用比例、状态、重置时间、超额状态 |
| ChatGPT | `GET chatgpt.com/backend-api/wham/usage`（UA 伪装成 codex_cli_rs） | 计划档位、主窗口已用百分比、重置时间、credits 余额、可用的重置次数 |
| xAI Grok | 无公开接口（`/v1/api-key`、`/grok.com/api/subscription` 全 401/403/404） | 只能靠 429 报错反推 |
| Gemini | `POST daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist` | 档位（Gemini Code Assist）、GCP 项目；**用量接口 403 SUBSCRIPTION_REQUIRED**，个人账号没开放 |

每次探测都消耗一次极小请求，所以 `QUOTA_TTL = 600`（10 分钟缓存），且结果持久化到 `~/claude-gateway/.quota.json` —— **控制台重启不用重等 30~40 秒**。

四家并发探测（`ThreadPoolExecutor(max_workers=4)`），总时长 = 最慢那家。实测 25~51 秒。

**网络波动时不覆盖旧值**：这次 `ok=False` 而上次 `ok=True`，就沿用上次结果并标 `stale`，卡片上显示「这次没连上（网络波动），下面是上次取到的」。数据从来不丢。

### 状态球

链路图下方四张球，一眼看清四家的状态。每个球三层信息：

| 层 | 表示什么 |
|---|---|
| 球内液面高度 | 主指标 —— 有额度接口的显示**额度剩余**（Claude 用 7 天窗口、ChatGPT 用主窗口），没有的显示**凭证时效** |
| 球外环绕弧 | 第二维度 —— Claude 用 5 小时窗口已用比例；其余平台无第二维度时不画 |
| 中心大字 | 与液面一致的数值，配一行单位说明 |
| 呼吸光晕 | 只在该平台**有效**时呼吸；凭证过期或未接入时变灰静止 |

配色沿用平台色（Claude 橙 / ChatGPT 绿 / Grok 紫 / Gemini 蓝）。悬停时卡片微抬 3px。

球下方两行小字：什么时候重置、凭证还剩多久；没有额度接口的会明确写「无额度接口 · 靠 429 反推」。


---

## 第四批：界面、额度与通知（让面板"能看懂了"）

### 1. 四家品牌 Logo（纯内联 SVG，零外部依赖）

`index.html` 顶部有 `const LOGO = {claude, codex, xai, antigravity}`，每个是一段 24×24 的 SVG：

| 平台 | 图形 |
|---|---|
| Claude | 放射状 8 芒星（`stroke-linecap="round"`） |
| ChatGPT / Codex | 六瓣环（三条弧线拼花） |
| xAI Grok | X 形双斜线 |
| Gemini | 四角星 |

链路图订阅卡、状态球下方名称前都用它，替换掉之前的字母 C / G / X / G。**不引外链**（离线可用、无隐私外泄）。

### 2. 「凭证」与「额度窗口」是两件事（之前混在一起）

| 说法 | 含义 | 会不会影响使用 |
|---|---|---|
| **登录凭证 · 3小时29分后自动续期** | OAuth access_token 的有效期 | **不会** —— 网关用 refresh_token 自动换新的 |
| **额度 · 9小时41分后重置** | 订阅配额窗口的重置时间 | 会 —— 窗口用尽就得等它重置 |

链路图副行显示前者，状态球 meta 两行分别显示两者。凭证有效期是"600 秒"这类数字的来源，保留在行尾。

### 3. 顶栏导航（六个板块）

`概览 / 模型 / 用量 / 凭证 / 接入 / 运行` —— 点击平滑滚动到对应板块，滚动时自动高亮当前块（`getBoundingClientRect().top <= 120` 判定）。窗口宽度 < 1180px 时导航自动隐藏（`.tabs{display:none}`）。

右侧只留高频三项：**+ 添加账号 / 取额度 / 体检**，其余收进「更多 ▾」：设置 / 高级·别名与备份 / 刷新数据 / 立即备份 / 重启网关 / 停止网关（红字）。

**注意**：这些按钮原来的 `id="btn-set"` 等已被导航栏取代，`data-m` 代理改成了 `data-act` + `MORE_ACT` 函数表 —— 早期版本因为 `getElementById` 找不到元素，整个「更多」菜单点了没反应。

### 4. 通知：为什么之前每 5 分钟弹一次「GPT 额度用尽」

**根因**：`_notify_scan()` 的去重 key 是 `kind|model|reset_sec`，而 **`reset_sec` 是倒计时，每秒都在变** → 每 300 秒扫一次都被当作"新异常" → 每 5 分钟弹一次。

**修法**：
1. key 改成 `kind|model`（去掉会变的部分）
2. `NOTIFY_SEEN_AT = {key: 时间戳}` + `NOTIFY_REPEAT_SEC = 12*3600` —— 同一件事 12 小时只打扰一次
3. **额度类默认不提醒**：`QUOTA_KINDS`（额度用尽 / 模型冷却 / 限流 / usage_limit / cooldown / rate_limit / 429）在「只看故障」模式下直接跳过 —— 账号配额不是网关故障
4. 设置里三档：`全部提醒` / `只看故障`（默认）/ `关闭`，存在 `~/claude-gateway/.notify-level`

**改通知级别不会重启网关**：`settings_set()` 里加了 `need_restart = any(c in ("访问范围","端口","日志调试") for c in changed)`。

**彻底关掉系统通知**：系统设置 → 通知 → 「Gateway One」或「Python」→ 关闭。

### 5. 状态球（立体额度可视化）

四张卡横排，每张一个 108×108 的球：

| 层次 | 表达 | 实现 |
|---|---|---|
| **液面高度** | 主指标（剩余额度 %） | `.orb-fill i{height:var(--fill)}` + `blur(1px)` + 高光条 |
| **环绕弧** | 第二维度（5 小时窗口已用 %） | `conic-gradient` + `mask` 只留一圈细环 |
| **球体立体** | 玻璃质感 | 左上 `radial-gradient` 高光 + 底部内反射 + `inset` 内阴影 + 外发光 + `::after` 模糊椭圆 |
| **呼吸光** | 运行中 | `@keyframes breathe` 4.2s，两层 box-shadow 交替 |
| **颜色** | 品牌识别 | claude `#d97757` / codex `#10a37f` / xai `#a78bfa` / antigravity `#4285f4` |

四家的数据来源不同，所以球里显示的"主指标"也不同：

- **Claude / ChatGPT**：有额度接口 → 液面 = 剩余额度%，环绕弧 = 窗口已用%
- **xAI / Gemini**：没有额度接口 → 液面 = 凭证有效期占比，中心大字 = 剩余时间，第二行注明「无额度接口 · 靠 429 反推」

### 6. 这一批踩到的坑

| 现象 | 根因 | 修法 |
|---|---|---|
| 页面永远「连接中…」，但手动 `refresh()` 能跑 | 我新增的 `function _b(id,fn)` 与**文件里原有的** `_b(id,event,fn)` 重名 → `Identifier '_b' has already been declared` → **整个 `<script>` 解析失败，一行都不执行** | 删掉重复定义。诊断用 `new Function(document.scripts[0].textContent)` 拿精确 message；注意 `typeof refresh` 仍是 `"function"`（函数提升会骗人），要看 `typeof 某个 const` |
| 点了「更多 → 设置」没反应 | 顶栏重排后 `#btn-set` 等 6 个 id 全被删了，`data-m` 代理 `getElementById` 拿到 null | 改 `data-act` + `MORE_ACT` 函数表 |
| 球里全显示「凭证时效」 | `quota_cached()` 缓存为空返回 `None` → `overview.quota = null` → 四家全走 fallback | 空时返回 `{}` |
| 控制台重启后额度要重新等 39 秒 | 缓存只在内存 | 落盘 `~/claude-gateway/.quota.json`（600），启动读回 |
| 设置弹层顶部被切掉 | `.modal-box` 没限高 | `max-height:88vh` + `.modal-body{overflow-y:auto}` |
| 通知级别改了却重启网关 | `settings_set` 无条件调 `restart_gateway()` | 只在 host/port/debug 变化时重启 |

---

# 第五批：平台详情页（点卡片看配置）

## 它解决什么

四个平台（Claude / ChatGPT / Grok / Gemini）各自的配置、凭证、模型、API 地址原来是散在 config.yaml、auth/ 目录、文档里的。现在点一下就都在一个抽屉里。

## 怎么进

- 点链路图左侧的**订阅卡**
- 或点模型卡右边的「**详情**」
- 或按 `Escape` 关闭

## 抽屉里有什么

| 区块 | 内容 |
|---|---|
| 额度 | Claude：7 天窗口 / 5 小时窗口 / 超额状态；ChatGPT：套餐、窗口已用 %、余额、重置时间；Gemini：档位、项目、说明；Grok：无接口（只有凭证时效） |
| 账号与凭证 | 邮箱、凭证剩余时间（自动续期）、文件名、刷新令牌有无、「重新登录」按钮 |
| 模型 N 个 · 别名 M 个 | 每行 id + 别名徽章 + 「详情」「测试」；测试会真发一次请求 |
| API 接入 | Anthropic 协议地址（不带 /v1）、OpenAI 协议地址（带 /v1）、各带复制按钮、模型名填什么、用哪把 Key |
| 配置 | `config.yaml` 里该平台的 `upstream.<channel>` 原文；没有就说明「用网关默认配置」 |
| 最近异常 | 该平台最近的报错（额度用尽 / 冷却 / 鉴权失败） |

## 一个必须知道的事实

`config.yaml` 里**只有 `upstream.claude` 段**。ChatGPT / Grok / Gemini 三家都没有覆写，用的是网关内置默认值。所以抽屉里那三家显示「当前用网关默认配置」是**准确反映**，不是读不到。

要改它们的伪装开关、冷却策略、超时，就在 config.yaml 里自己加一段：

```yaml
upstream:
  xai:
    inject-x-search: false
  codex:
    disable-codex-cloaking: false
```

保存后点「重启网关」生效。

## 后端接口

`GET /api/channel?id=<claude|codex|xai|antigravity>`

返回：`label / color / desc / creds[] / models[] / alias_count / quota / issues / config{upstream,alias} / api{base_anthropic,base_openai,example_model} / usage`

`fetch_models()` 现在一并返回 `owners`（`/v1/models` 的 `owned_by`），用它把模型精确归属到平台：
`anthropic: 22 / openai: 16 / xai: 23 / antigravity: 11`。

## 踩到的坑

| 现象 | 原因 | 修法 |
|---|---|---|
| 抽屉里别名数全是 0 | `aliases_read()` 直接返回 `{claude:[],…}`，没有 `aliases` 外层（那是 `/api/aliases` 路由才包的） | `al.get(ch)` 而不是 `al.get("aliases").get(ch)` |
| 后端启动即 `IndentationError` | 新路由插在 8 空格层，实际在 `try:` 内要 12 空格 | 补缩进，连带修了被挤错的 `/api/usage` |
| 整个页面白屏、所有函数 undefined | 给 `testModel` 前面插 `goPlat` 时把它的 `async` 吃掉了 → `await is only valid in async functions` | 还原 `async function testModel` |
| 余额显示 `[object Object]` | `q.credits` 是对象 `{has_credits,unlimited,balance}` 不是字符串 | 按对象取值，`unlimited` 显示「无限」、否则 `balance + （无）` |

---

# 第六批：事件流挪位 + API 统一管理

## 一、事件流从右栏移走

右栏原来四块（用量窗口 / 额度余量 / 额度与故障 / 事件流），事件流占掉了近一半高度，还把左栏模型区拉得很长。

现在：
- 右栏只剩三块，高度刚好对齐
- 事件流移到**运行明细下方**，默认折叠成一行「最近事件 N 条」
- 顶栏健康状态旁边多一个**红色提醒点**：只在出现 `AUTH-FAIL` / `PERM-FIX` / `FAIL` / `ERROR` 类事件时亮，点它会展开事件列表并滚过去

判据函数 `isAlertEvent(e)`：文本命中告警关键词且**不是** `OK: 200` 才算。

## 二、API 统一管理（原「接入」tab 升级）

tab 从 6 个收敛到 5 个：概览 / 模型 / 用量 / **API** / 运行。

一个页面回答四个问题：

| 区块 | 回答什么 |
|---|---|
| **对外端点** | 别人该填什么地址。Anthropic（不带 /v1）、OpenAI（带 /v1），各带复制；下方状态标签：监听地址 / 局域网是否可达 / **访问密码是否已设** / 模型总数 |
| **访问密钥** | 谁能进来。掩码 + 备注 + 复制/改名/删除，底部「+ 新增 Key」 |
| **接入片段** | 怎么接。选客户端 / 选 Key / 选模型 → 直接生成可复制的代码（`.env` / `config.toml` / Python / curl 四种形态），支持「存成 .env」 |
| **用量与上游健康** | 用了多少 + 上游通不通。今日请求 / 对话 / 成功率 / p50 / p95；四家上游状态标签；「跑一次 API 体检」 |

## 三、必须知道的两个上限

1. **`access.api-keys` 是纯字符串数组**，不支持按 Key 限模型、限次数。所有 Key 权限完全相同，都能调全部 72 个模型。要真正分权只能开第二个网关实例（不同端口 + 不同上游凭证）。
2. **网关日志不记录请求用了哪把 Key**，格式里只有 `时间 / 状态 / 耗时 / IP / 方法 路径`。所以「这把 Key 今天用了 42 次」「这把从没用过」都做不到，界面里也不显示。

## 四、这一批的两个优化

| 项 | 之前 | 现在 |
|---|---|---|
| 删 Key | 同步等网关重启，**10.8 秒** | 先回响应，后台重启，**0.0023 秒**；前端提示「约 1 秒生效」并 1.6 秒后自动刷新 |
| 片段生成器默认模型 | 取模型表第一个（`claude-3-5-haiku-20241022`） | 按优先级选别名：`opus → sonnet → haiku → gpt → codex → grok → gemini` |

## 五、踩到的坑

| 现象 | 原因 | 修法 |
|---|---|---|
| 整页「控制台后端断开」，但 `curl /api/overview` 200 | 渲染函数抛错被 `refresh()` 的 catch 吞掉，误报成后端断开 | 单独 `try { renderApiGen(S.data) }` 拿到真错误 |
| `(d.models || []).slice is not a function` | overview 的 `models` 是 `{ok, models[], owners{}, ms}`，多包了一层 | 统一取 `d.models.models` |
| 片段生成器默认选到冷门模型 | 直接取 `models[0]` | 先按别名优先级排序再取 |

## 六、新增接口

无新增（复用 `/api/overview`、`/api/keys`、`/api/quota`、`/api/doctor`）。

`POST /api/keys/del` 的返回多了一个 `restart.async: true` 字段，表示重启在后台进行。
