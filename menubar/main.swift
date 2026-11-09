// Gateway One 菜单栏小程序
// 编译：swiftc -O -swift-version 5 -o gatewayone-menubar main.swift
import Cocoa

let API_BASE = "http://127.0.0.1:8899"
let REFRESH_SEC: Double = 10

// MARK: - HTTP

func httpGet(_ path: String, timeout: Double = 5) -> [String: Any]? {
    guard let url = URL(string: API_BASE + path) else { return nil }
    var req = URLRequest(url: url)
    req.timeoutInterval = timeout
    let sem = DispatchSemaphore(value: 0)
    var out: [String: Any]?
    URLSession.shared.dataTask(with: req) { data, _, _ in
        if let d = data,
           let obj = try? JSONSerialization.jsonObject(with: d),
           let dict = obj as? [String: Any] {
            out = dict
        }
        sem.signal()
    }.resume()
    _ = sem.wait(timeout: .now() + timeout + 1)
    return out
}

@discardableResult
func httpPost(_ path: String, _ body: [String: Any] = [:], timeout: Double = 60) -> [String: Any]? {
    guard let url = URL(string: API_BASE + path) else { return nil }
    var req = URLRequest(url: url)
    req.httpMethod = "POST"
    req.timeoutInterval = timeout
    req.setValue("application/json", forHTTPHeaderField: "Content-Type")
    req.httpBody = try? JSONSerialization.data(withJSONObject: body)
    let sem = DispatchSemaphore(value: 0)
    var out: [String: Any]?
    URLSession.shared.dataTask(with: req) { data, _, _ in
        if let d = data,
           let obj = try? JSONSerialization.jsonObject(with: d),
           let dict = obj as? [String: Any] {
            out = dict
        }
        sem.signal()
    }.resume()
    _ = sem.wait(timeout: .now() + timeout + 2)
    return out
}

// MARK: - 格式化

func fmtDur(_ sec: Double) -> String {
    if sec <= 0 { return "已过期" }
    let total = Int(sec)
    let d = total / 86400
    let h = (total % 86400) / 3600
    let m = (total % 3600) / 60
    if d > 0 { return "剩 \(d)天\(h)小时" }
    if h > 0 { return "剩 \(h)小时\(m)分" }
    return "剩 \(m)分"
}

func shortType(_ t: String) -> String {
    switch t {
    case "claude": return "Claude Max"
    case "codex": return "ChatGPT"
    case "xai": return "xAI Grok"
    default: return t
    }
}

// MARK: - App

final class AppDelegate: NSObject, NSApplicationDelegate {
    var statusItem: NSStatusItem!
    var timer: Timer?
    var paused = false
    var lastOK = false
    var lastIssues = 0

    func applicationDidFinishLaunching(_ note: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            button.imagePosition = .imageLeading
            button.image = NSImage(systemSymbolName: "circle.fill", accessibilityDescription: "Gateway One")
            button.image?.isTemplate = true
            button.image?.size = NSSize(width: 9, height: 9)
            button.title = "GW"
            button.font = NSFont.systemFont(ofSize: 11, weight: .medium)
            button.contentTintColor = .systemGray
        }
        // 启动自检：把状态项位置写进日志，便于确认它是否真的挂上了菜单栏
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
            if let w = self.statusItem.button?.window {
                FileHandle.standardError.write("statusitem frame=\(w.frame) visible=\(self.statusItem.isVisible)\n".data(using: .utf8)!)
            } else {
                FileHandle.standardError.write("statusitem window is nil\n".data(using: .utf8)!)
            }
        }
        rebuildMenu(nil)
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: REFRESH_SEC, repeats: true) { [weak self] _ in
            guard let self = self, !self.paused else { return }
            self.refresh()
        }
    }

    func refresh() {
        let d = httpGet("/api/overview")
        if d == nil {
            lastOK = false
            setTint(.systemRed, tip: "控制台连不上")
            rebuildMenu(nil)
            return
        }
        let alive = ((d?["gateway"] as? [String: Any])?["alive"] as? Bool) ?? false
        let issues = (d?["issues"] as? [[String: Any]]) ?? []
        lastOK = alive
        lastIssues = issues.count
        if !alive {
            setTint(.systemRed, tip: "网关已停止")
        } else if !issues.isEmpty {
            setTint(.systemYellow, tip: "有 \(issues.count) 项提醒")
        } else {
            setTint(.systemGreen, tip: "一切正常")
        }
        rebuildMenu(d)
    }

    func setTint(_ color: NSColor, tip: String) {
        if let button = statusItem.button {
            button.contentTintColor = color
            button.toolTip = "Gateway One — " + tip
        }
    }

    func rebuildMenu(_ d: [String: Any]?) {
        let menu = NSMenu()

        // 标题行
        var head = "Gateway One"
        if let d = d, let g = d["gateway"] as? [String: Any] {
            let port = g["port"] as? Int ?? 0
            let alive = g["alive"] as? Bool ?? false
            let up = g["uptime"] as? String ?? "—"
            head = alive ? "运行中 · :\(port) · ↑\(up)" : "已停止 · :\(port)"
        } else {
            head = "控制台连不上"
        }
        let headItem = NSMenuItem(title: head, action: nil, keyEquivalent: "")
        headItem.isEnabled = false
        menu.addItem(headItem)
        menu.addItem(.separator())

        // 三家凭证
        if let auth = d?["auth"] as? [[String: Any]], !auth.isEmpty {
            for a in auth {
                let t = shortType(a["type"] as? String ?? "?")
                let email = a["email"] as? String ?? ""
                let disabled = a["disabled"] as? Bool ?? false
                let remain = (a["remain_sec"] as? NSNumber)?.doubleValue ?? 0
                var right: String
                if disabled { right = "● 已禁用" }
                else if remain <= 0 { right = "● 已过期" }
                else { right = fmtDur(remain) }
                let it = NSMenuItem(title: "\(t)  \(right)", action: nil, keyEquivalent: "")
                it.isEnabled = false
                if !email.isEmpty { it.toolTip = email }
                if disabled || remain <= 0 { it.attributedTitle = redTitle(it.title) }
                menu.addItem(it)
            }
        } else {
            let it = NSMenuItem(title: "还没有接入任何账号", action: nil, keyEquivalent: "")
            it.isEnabled = false
            menu.addItem(it)
        }
        menu.addItem(.separator())

        // 用量
        if let logs = d?["logs"] as? [String: Any] {
            let calls = logs["chat_calls"] as? Int ?? 0
            let rate = logs["ok_rate"] as? Int ?? 0
            let p50 = (logs["p50"] as? NSNumber)?.doubleValue ?? 0
            let line = "对话 \(calls) 次 · 成功率 \(rate)% · p50 \(String(format: "%.1f", p50))s"
            let it = NSMenuItem(title: line, action: nil, keyEquivalent: "")
            it.isEnabled = false
            menu.addItem(it)
        }

        // 异常
        if let issues = d?["issues"] as? [[String: Any]], !issues.isEmpty {
            let sub = NSMenuItem(title: "异常 \(issues.count) 项", action: nil, keyEquivalent: "")
            let sm = NSMenu()
            var seen = Set<String>()
            for i in issues {
                let kind = i["kind"] as? String ?? "?"
                let model = i["model"] as? String ?? (i["provider"] as? String ?? "")
                let key = "\(kind)|\(model)"
                if seen.contains(key) { continue }
                seen.insert(key)
                var line = "\(kind) · \(model)"
                if let rs = (i["reset_sec"] as? NSNumber)?.doubleValue, rs > 0 {
                    line += " · 重置 \(fmtDur(rs))"
                }
                let si = NSMenuItem(title: line, action: nil, keyEquivalent: "")
                si.isEnabled = false
                sm.addItem(si)
            }
            sub.submenu = sm
            menu.addItem(sub)
        }
        menu.addItem(.separator())

        // 动作
        let open = NSMenuItem(title: "打开控制台", action: #selector(openConsole), keyEquivalent: "o")
        open.target = self
        menu.addItem(open)

        let restart = NSMenuItem(title: "重启网关", action: #selector(restartGateway), keyEquivalent: "r")
        restart.target = self
        menu.addItem(restart)

        let pauseTitle = paused ? "恢复监控" : "暂停监控"
        let pause = NSMenuItem(title: pauseTitle, action: #selector(togglePause), keyEquivalent: "p")
        pause.target = self
        menu.addItem(pause)

        menu.addItem(.separator())
        let quit = NSMenuItem(title: "退出", action: #selector(quit), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)

        statusItem.menu = menu
    }

    func redTitle(_ s: String) -> NSAttributedString {
        NSAttributedString(string: s, attributes: [
            .foregroundColor: NSColor.systemRed,
            .font: NSFont.menuFont(ofSize: 13),
        ])
    }

    @objc func openConsole() {
        NSWorkspace.shared.open(URL(string: API_BASE + "/")!)
    }

    @objc func restartGateway() {
        _ = httpPost("/api/service", ["action": "restart"], timeout: 60)
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { self.refresh() }
    }

    @objc func togglePause() {
        paused.toggle()
        if paused {
            timer?.invalidate()
            setTint(.systemGray, tip: "监控已暂停")
        } else {
            timer = Timer.scheduledTimer(withTimeInterval: REFRESH_SEC, repeats: true) { [weak self] _ in
                guard let self = self, !self.paused else { return }
                self.refresh()
            }
            refresh()
        }
        rebuildMenu(httpGet("/api/overview"))
    }

    @objc func quit() {
        NSApp.terminate(nil)
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
