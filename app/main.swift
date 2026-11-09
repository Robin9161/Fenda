// Gateway One — macOS 桌面客户端
// 一个原生窗口包住控制台页面；启动时自动确保后端在跑，不需要你开浏览器。

import Cocoa
import WebKit

let UI_PORT = ProcessInfo.processInfo.environment["CG_UI_PORT"] ?? "8899"
let LABEL = ProcessInfo.processInfo.environment["CG_LABEL_UI"] ?? "com.local.cgconsole"
let BASE = "http://127.0.0.1:\(UI_PORT)/"

func probeBackend(_ done: @escaping (Bool) -> Void) {
    guard let url = URL(string: BASE + "api/auth/state") else { return done(false) }
    var req = URLRequest(url: url)
    req.timeoutInterval = 1.5
    req.cachePolicy = .reloadIgnoringLocalCacheData
    URLSession.shared.dataTask(with: req) { _, resp, _ in
        let ok = (resp as? HTTPURLResponse) != nil
        DispatchQueue.main.async { done(ok) }
    }.resume()
}

func kickstartBackend() {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
    p.arguments = ["kickstart", "-k", "gui/\(getuid())/\(LABEL)"]
    try? p.run()
    p.waitUntilExit()
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var splash: NSView!
    var statusLabel: NSTextField!
    var spin: NSProgressIndicator!
    var retryBtn: NSButton!
    var fails = 0

    func applicationDidFinishLaunching(_ note: Notification) {
        buildMenu()
        buildWindow()
        boot()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    // MARK: - 界面

    func buildMenu() {
        let main = NSMenu()

        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "关于 Gateway One", action: #selector(showAbout), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "重新加载", action: #selector(reloadPage), keyEquivalent: "r")
        appMenu.addItem(withTitle: "重启网关", action: #selector(restartGateway), keyEquivalent: "R")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "隐藏", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "退出 Gateway One", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        main.addItem(appItem)

        let editItem = NSMenuItem()
        let edit = NSMenu(title: "编辑")
        edit.addItem(withTitle: "撤销", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "重做", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "拷贝", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        main.addItem(editItem)

        NSApp.mainMenu = main
    }

    func buildWindow() {
        let rect = NSRect(x: 0, y: 0, width: 1180, height: 820)
        window = NSWindow(
            contentRect: rect,
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = ""
        window.titlebarAppearsTransparent = false
        window.titleVisibility = .hidden
        window.minSize = NSSize(width: 900, height: 620)
        window.backgroundColor = NSColor(calibratedRed: 0.04, green: 0.05, blue: 0.07, alpha: 1)
        window.setFrameAutosaveName("GatewayOneMainWindow")
        window.center()

        let cfg = WKWebViewConfiguration()
        cfg.websiteDataStore = .default()          // 保住登录 cookie
        cfg.preferences.setValue(true, forKey: "developerExtrasEnabled")

        web = WKWebView(frame: rect, configuration: cfg)
        web.navigationDelegate = self
        web.setValue(false, forKey: "drawsBackground")
        web.translatesAutoresizingMaskIntoConstraints = false

        let host = NSView(frame: rect)
        host.addSubview(web)
        NSLayoutConstraint.activate([
            web.leadingAnchor.constraint(equalTo: host.leadingAnchor),
            web.trailingAnchor.constraint(equalTo: host.trailingAnchor),
            web.topAnchor.constraint(equalTo: host.topAnchor),
            web.bottomAnchor.constraint(equalTo: host.bottomAnchor),
        ])
        window.contentView = host

        buildSplash(in: host)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) {
            NSRunningApplication.current.activate(options: [.activateAllWindows])
            self.window.makeKeyAndOrderFront(nil)
        }
    }

    func buildSplash(in host: NSView) {
        splash = NSView(frame: host.bounds)
        splash.translatesAutoresizingMaskIntoConstraints = false
        splash.wantsLayer = true
        splash.layer?.backgroundColor = NSColor(calibratedRed: 0.04, green: 0.05, blue: 0.07, alpha: 1).cgColor
        host.addSubview(splash)
        NSLayoutConstraint.activate([
            splash.leadingAnchor.constraint(equalTo: host.leadingAnchor),
            splash.trailingAnchor.constraint(equalTo: host.trailingAnchor),
            splash.topAnchor.constraint(equalTo: host.topAnchor),
            splash.bottomAnchor.constraint(equalTo: host.bottomAnchor),
        ])

        let title = NSTextField(labelWithString: "Gateway One")
        title.font = NSFont.systemFont(ofSize: 30, weight: .semibold)
        title.textColor = NSColor(calibratedWhite: 0.92, alpha: 1)
        title.alignment = .center
        title.translatesAutoresizingMaskIntoConstraints = false
        splash.addSubview(title)

        spin = NSProgressIndicator()
        spin.style = .spinning
        spin.controlSize = .small
        spin.startAnimation(nil)
        spin.translatesAutoresizingMaskIntoConstraints = false
        splash.addSubview(spin)

        statusLabel = NSTextField(labelWithString: "正在连接本地网关…")
        statusLabel.font = NSFont.systemFont(ofSize: 13)
        statusLabel.textColor = NSColor(calibratedWhite: 0.55, alpha: 1)
        statusLabel.alignment = .center
        statusLabel.translatesAutoresizingMaskIntoConstraints = false
        splash.addSubview(statusLabel)

        retryBtn = NSButton(title: "重试", target: self, action: #selector(retryTapped))
        retryBtn.bezelStyle = .rounded
        retryBtn.isHidden = true
        retryBtn.translatesAutoresizingMaskIntoConstraints = false
        splash.addSubview(retryBtn)

        NSLayoutConstraint.activate([
            title.centerXAnchor.constraint(equalTo: splash.centerXAnchor),
            title.centerYAnchor.constraint(equalTo: splash.centerYAnchor, constant: -34),
            spin.centerXAnchor.constraint(equalTo: splash.centerXAnchor, constant: -62),
            spin.centerYAnchor.constraint(equalTo: statusLabel.centerYAnchor),
            statusLabel.centerXAnchor.constraint(equalTo: splash.centerXAnchor, constant: 8),
            statusLabel.topAnchor.constraint(equalTo: title.bottomAnchor, constant: 18),
            retryBtn.centerXAnchor.constraint(equalTo: splash.centerXAnchor),
            retryBtn.topAnchor.constraint(equalTo: statusLabel.bottomAnchor, constant: 16),
        ])
    }

    // MARK: - 启动流程

    func boot() {
        fails = 0
        startBackend(attempt: 0)
    }

    func startBackend(attempt: Int) {
        probeBackend { ok in
            if ok { self.loadPage(); return }
            if attempt == 0 {
                // 后端没在跑：让 launchd 拉一次
                self.statusLabel.stringValue = "网关服务未运行，正在拉起…"
                kickstartBackend()
                DispatchQueue.main.asyncAfter(deadline: .now() + 2.2) {
                    self.startBackend(attempt: 1)
                }
            } else if attempt < 8 {
                self.statusLabel.stringValue = "等待服务就绪…（\(attempt)/8）"
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) {
                    self.startBackend(attempt: attempt + 1)
                }
            } else {
                self.showFailure()
            }
        }
    }

    func loadPage() {
        statusLabel.stringValue = "正在加载界面…"
        web.load(URLRequest(url: URL(string: BASE)!,
                             cachePolicy: .reloadIgnoringLocalCacheData,
                             timeoutInterval: 15))
    }

    func showFailure() {
        spin.stopAnimation(nil)
        spin.isHidden = true
        statusLabel.stringValue = "连不上本地网关（127.0.0.1:\(UI_PORT)）。\n可以试试点「重试」，或在终端跑：\nlaunchctl kickstart -k gui/$(id -u)/\(LABEL)"
        statusLabel.maximumNumberOfLines = 6
        statusLabel.font = NSFont.systemFont(ofSize: 12)
        retryBtn.isHidden = false
    }

    @objc func retryTapped() {
        retryBtn.isHidden = true
        spin.isHidden = false
        spin.startAnimation(nil)
        statusLabel.font = NSFont.systemFont(ofSize: 13)
        statusLabel.maximumNumberOfLines = 1
        statusLabel.stringValue = "正在重新连接…"
        boot()
    }

    @objc func reloadPage() { web.reload() }

    @objc func restartGateway() {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        p.arguments = ["kickstart", "-k", "gui/\(getuid())/com.local.cliproxyapi"]
        try? p.run()
        let a = NSAlert()
        a.messageText = "已请求重启网关"
        a.informativeText = "几秒后页面上的状态会变成新的 PID。"
        a.addButton(withTitle: "好")
        a.runModal()
    }

    @objc func showAbout() {
        let a = NSAlert()
        a.messageText = "Gateway One"
        a.informativeText = "订阅网关控制台（桌面客户端）\n\n后端：127.0.0.1:\(UI_PORT)\n网关：127.0.0.1:8317"
        a.addButton(withTitle: "好")
        a.runModal()
    }

    // MARK: - WKNavigationDelegate

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        splash.isHidden = true
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        handleLoadFail(error)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        handleLoadFail(error)
    }

    func handleLoadFail(_ error: Error) {
        fails += 1
        if fails <= 3 {
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { self.loadPage() }
            return
        }
        splash.isHidden = false
        showFailure()
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
