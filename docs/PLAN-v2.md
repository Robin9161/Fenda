# Claude Max 反代 · 最优方案 v2

> 判断标准变了：v1 在回答「在你这台机器上怎么跑起来」，v2 回答「怎样才算稳」。
> 结论：**方案不为一台笔记本设计，为一台常开主机设计；网关卡在家庭住宅 IP 上，出口走 Tailscale，一台二进制在 macOS/Linux 上同构部署，迁移 5 分钟。**

---

## 1. 先重新定义「稳」

稳定性不看架构多花哨，看真实的失效顺序。按实际事故频率排序：

| 排序 | 真实失效原因 | 占比体感 | v1 有没有解决 |
|---|---|---|---|
| 1 | **笔记本合盖 / 睡眠 / 断网** → 全设备 500 | 最高 | ❌ 最大漏洞 |
| 2 | **OAuth token 被踢**（改密码、异地、风控） | 高 | 部分（靠自动续） |
| 3 | **撞 5 小时窗口限额** → 429 | 高 | ❌ 没管 |
| 4 | 上游改了客户端校验头 → 全挂 | 中 | 靠升级 |
| 5 | 网络路径（自建 nginx 缓冲 SSE） | 中 | 已注明 |
| 6 | 进程崩溃没自愈 | 低 | KeepAlive 部分覆盖 |

**所以 v2 的全部改造，就是逐条干掉前 4 个。** 架构不变（CLIProxyAPI），变的是**部署形态、常开保证、限额管理、自愈与告警**。

---

## 2. 最优架构

```
        ┌───────────── 家庭网络（住宅 IP）─────────────┐
        │                                            │
        │   ┌──────────────────────────────────┐     │
 手机 ──┼──▶│ 常开主机（Mac mini / N100 / NAS） │     │
 平板 ──┼──▶│                                  │     │
 电脑 ──┼──▶│  Tailscale (100.x) ──┐            │     │
 IDE  ──┼──▶│  LAN 192.168.x ──────┤            │     │
        │   │                      ▼            │     │
        │   │        127.0.0.1:8317             │     │
        │   │        CLIProxyAPI                 │     │
        │   │         ├ 本地 Key 校验            │     │
        │   │         ├ 并发闸 / 排队            │     │
        │   │         ├ 模型别名 opus            │     │
        │   │         └ OAuth 注入 + 自动续期    │     │
        │   └──────────────┬───────────────────┘     │
        └──────────────────┼─────────────────────────┘
                           ▼
                    api.anthropic.com
```

四个设计决定，和 v1 的差别在括号里：

1. **网关跑在常开主机上，不是笔记本**（v1：跑在笔记本）
2. **唯一出口是家庭住宅 IP**（v1：卧室笔记本，其实也是 —— 但会随人移动，这是隐性风险）
3. **不租 VPS**（机房 IP 是风控高危段，且多一跳延迟）
4. **一份静态二进制，macOS 用 launchd、Linux 用 systemd，同构部署**（v1：只写了 launchd）

---

## 3. 主机选型（这一条决定 80% 的稳定性）

| 主机 | 一次性成本 | 常开可靠性 | IP 风控 | 结论 |
|---|---|---|---|---|
| 现有 MacBook | 0 | ★★（合盖即死） | ✅ 住宅 | **过渡用，方案设计成随时可搬** |
| **Mac mini（M1/M4）** | 二手 ~1500 / 新 4499 | ★★★★★ | ✅ 住宅 | **首选**，功耗 5–20W，可 headless |
| **N100 小主机** | 600–900 | ★★★★★ | ✅ 住宅 | **首选（性价比）**，Linux 原生 |
| 树莓派 5 / 旧笔记本 | 400–800 | ★★★★ | ✅ 住宅 | 够用，注意 SD 卡寿命用 SSD |
| 群晖 / 威联通 NAS（已有） | 0 | ★★★★★ | ✅ 住宅 | **零成本最优**，跑 Docker |
| 云 VPS | $5/月 | ★★★★★ | ❌ **机房 IP，最易触发验证** | **不推荐** |

**结论：别为这个方案去租 VPS。** 家庭住宅 IP 的稳定性收益远大于 VPS 的在线率收益——因为账号风控看 IP，而 VPS 的 ASN 天然可疑。

如果你现在只有笔记本：**照 v2 部署，什么都不用改**。等有常开主机时，迁移动作为「拷目录 + 起服务 + 重新登录」，5 分钟。这就是为什么方案要做成主机无关。

---

## 4. 「稳」落地的 8 项工程

### ① 防睡眠（笔记本形态必做，常开主机可跳过）

```bash
# 永不休眠、磁盘不休眠、断电恢复后自动开机
sudo pmset -a sleep 0 disksleep 0 womp 1 autorestart 1

# 合盖也不睡（笔记本专属）
sudo pmset -b disablesleep 1
```

验证：`pmset -g` 里 `sleep 0`、`disablesleep 1`。

### ② 进程自愈

- macOS：`launchd` + `KeepAlive true` + `RunAtLoad true` + `ThrottleInterval 10`
- Linux：`systemd` + `Restart=always` + `RestartSec=5`

两份配置见 `deploy/`。崩溃后 10 秒内自动拉回。

### ③ 健康检查 + 告警（v1 完全没有，这是最大的稳定性增量）

`deploy/watchdog.sh`，由 launchd/systemd timer 每 5 分钟触发：

- 端口没监听 → 直接 `kickstart` 重启服务
- `/v1/models` 返回非 200 → 记录，连续 3 次失败 → 重启 + 桌面通知
- 返回 **401 → 不重启**（重启没用），直接告警「需要重新登录」
- 返回 429 → 记录为限额事件，不重启（重启只会更糟）
- 每小时最多告警 1 次，避免刷屏

### ④ 日志轮转（v1 遗漏，会撑爆磁盘）

macOS `/etc/newsyslog.d/cliproxyapi.conf`：

```
# logfilename                                    [owner:group]  mode  count  size  when  flags
/Users/a11/claude-gateway/logs/out.log           a11:staff      600   5      10240 *     J
/Users/a11/claude-gateway/logs/err.log           a11:staff      600   5      10240 *     J
```

`J` = bzip2 压缩归档。Linux 用 logrotate，`size 10M rotate 5`。

**日志里有完整对话内容**，所以 mode 600 + 只留 5 份。

### ⑤ 凭证备份（防「重新登录」摩擦）

`~/claude-gateway/auth/` 里是 refresh token，丢了要重走浏览器授权。每天备份：

```bash
# 加密打包到 iCloud（或任意同步目录）
tar czf - -C ~/claude-gateway auth | \
  openssl enc -aes-256-cbc -pbkdf2 -out ~/Library/Mobile\ Documents/com~apple~CloudDocs/cg-auth-$(date +%F).enc
```

恢复：解密解包回 `~/claude-gateway/auth/`，重启即可，不用重新授权。

### ⑥ 并发闸（防撞限额，v1 只提了一句）

Max 是按「人的使用速率」建模的。多设备同时流式 = 必然 429。config 里限死：

```yaml
# 并发上限：宁可排队，不要 429
max-concurrent-requests: 3
request-timeout: 600
```

客户端侧配合：
- 长任务用 sonnet，只有硬需求才 `opus`
- 多个设备不要同时跑 Claude Code 大任务

### ⑦ 版本 pin + 秒级回滚

**不要自动升级。** 上游改的是非官方通道，新版本可能引入新问题。

```bash
# 升级流程
cp ~/claude-gateway/cli-proxy-api ~/claude-gateway/cli-proxy-api.prev   # 留一份旧的
bash setup.sh                                                          # 下新版
launchctl kickstart -k gui/$(id -u)/com.local.cliproxyapi
# 出问题 10 秒回滚
cp ~/claude-gateway/cli-proxy-api.prev ~/claude-gateway/cli-proxy-api
launchctl kickstart -k gui/$(id -u)/com.local.cliproxyapi
```

### ⑧ 双入口（Tailscale 主 + LAN 备）

- **主入口**：`tailscale serve --bg 8317` → `https://<host>.<tailnet>.ts.net`
  - 自动证书、自动穿透 NAT、**不做应用层缓冲**（SSE 天然正常，省掉 nginx 那堆 `proxy_buffering off` 的坑）
- **备入口**：`host: "0.0.0.0"` + `http://192.168.x.x:8317`，Tailscale 挂了还能在家里用
- 两个入口共用同一份 `api-keys`

为什么不推荐自建 nginx：`proxy_buffering`、`proxy_read_timeout`、HTTP/1.1 升级，三个坑任一个没配好就是「流式一顿一顿」或「60 秒断流」。Tailscale serve 把这些全绕过了。

---

## 5. 一套二进制，两种服务管理器

迁移成本 = 拷目录 + 装服务 + 重新登录（或还原 auth/）。

| | macOS | Linux |
|---|---|---|
| 二进制 | `cli-proxy-api`（darwin_arm64） | `cli-proxy-api`（linux_amd64/arm64） |
| 服务 | `~/Library/LaunchAgents/com.local.cliproxyapi.plist` | `/etc/systemd/system/cliproxyapi.service` |
| 自启 | `launchctl load -w` | `systemctl enable --now` |
| 日志 | `~/claude-gateway/logs/` | `journalctl -u cliproxyapi` |
| 配置 | `~/claude-gateway/config.yaml` | `/opt/cliproxyapi/config.yaml` |

**config.yaml 完全一致**，只有路径不同。换机器不用改任何逻辑。

### 迁移演练（5 分钟）

```bash
# 旧机
launchctl unload -w ~/Library/LaunchAgents/com.local.cliproxyapi.plist
scp -r ~/claude-gateway newhost:/opt/cliproxyapi     # 含 auth/ 和 config.yaml
# 新机（Linux）
sudo systemctl enable --now cliproxyapi
# 客户端不用改任何东西 —— Tailscale IP 变的话用 MagicDNS 名字就永远不变
```

> **用 MagicDNS 名字而不是 IP**：`http://gateway.<tailnet>.ts.net:8317`。换主机、换 IP，客户端零改动。这是「稳」里最省心的一个小设计。

---

## 6. 交付与使用（保持 v1 的结构，补两条）

Key 发放、客户端四项配置、三种交付方式（Tailnet / Tailnet+ACL / CF Tunnel）都沿用 v1 §8，补两条稳定性相关：

**① 客户端必须配超时和重试**

| 客户端 | 设置 |
|---|---|
| Claude Code | `ANTHROPIC_TIMEOUT=600`，重试靠自身 |
| Chatbox / Cherry | 请求超时拉到 300–600s |
| Cline / Roo | 同上，开 automatic retry |

**② 给对方的话里加一句**：

```
如果报 429，别立刻重试，等 5 分钟或换 sonnet。这是订阅窗口限额，重试只会更糟。
```

---

## 7. 与 v1 的差异对照

| 项 | v1 | v2 | 为什么 |
|---|---|---|---|
| 目标主机 | 现有 MacBook | 常开主机，笔记本仅过渡 | 睡眠是最大失效源 |
| 网络层 | Tailscale 或局域网 | Tailscale serve 主 + LAN 备 | 免 nginx 缓冲坑 + 有备份路径 |
| 出口 IP | 笔记本（会移动） | 固定家庭住宅 IP | 风控看 IP 一致性 |
| VPS | 未评价 | 明确不推荐 | 机房 ASN 高危 |
| 并发 | 提了一句 | config 硬限 + 客户端策略 | 429 是第二高失效 |
| 自愈 | KeepAlive | KeepAlive + watchdog（401/429 分类处理） | 崩溃与失效要分开处理 |
| 日志 | 未管 | newsyslog/logrotate + 600 权限 | 会撑爆磁盘且含敏感内容 |
| 凭证 | 未管 | 每日加密备份 | 降低重新授权摩擦 |
| 升级 | 直接跑 setup.sh | pin 版本 + `.prev` 秒回滚 | 非官方通道，必须可回退 |
| 客户端地址 | IP | MagicDNS 名字 | 换主机零改动 |
| 跨 OS | 只 macOS | macOS + Linux 同构 | 迁移不受限 |

---

## 8. 最终验收清单

- [ ] 主机防睡眠已设（`pmset -g` 核对）
- [ ] 服务开机自启（重启验证）
- [ ] `launchctl kickstart -k` 后 3 秒内恢复
- [ ] `kill -9` 进程后 30 秒内自动回来
- [ ] watchdog 手动跑一次，日志有记录
- [ ] 桌面通知能弹出（断网模拟一次）
- [ ] `/v1/models` + `/v1/messages` 均 200
- [ ] 第二台设备经 Tailscale MagicDNS 可达
- [ ] Tailscale 断开后 LAN 入口仍可用
- [ ] `auth/` 权限 700，备份文件已生成
- [ ] 日志轮转配置已装（`newsyslog -nv` 检查）
- [ ] `cli-proxy-api.prev` 存在
- [ ] 3 把 Key 已分配并记录归属

全部打勾 = 这个方案「稳」了。
