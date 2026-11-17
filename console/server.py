#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Claude Gateway Console —— 本地可视化控制台（后端）

零依赖：只用 Python 标准库。只绑 127.0.0.1，不对外。
所有数据都来自真实来源：launchd / 端口 / auth 目录 / config.yaml / 网关 API / 日志。
凭证 token 永不出现在任何响应里。
"""
from __future__ import annotations

import base64
import glob
import json
import os
import pty
import re
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener, urlopen

BASE = os.path.dirname(os.path.abspath(__file__))
GW = os.path.expanduser(os.environ.get("CG_DIR", "~/claude-gateway"))
CONFIG = os.path.join(GW, "config.yaml")
AUTH_DIR = os.path.join(GW, "auth")
LOGS = os.path.join(GW, "logs")
UI_PORT = int(os.environ.get("CG_UI_PORT", "8899"))
LABEL = os.environ.get("CG_LABEL", "com.local.cliproxyapi")
LABEL_WD = os.environ.get("CG_LABEL_WD", "com.local.cgwatchdog")
BIN = os.path.join(GW, "cli-proxy-api")

# 默认跟随系统时区；只有显式设了 CG_TZ 才覆盖（三处必须同一个基准）。
if os.environ.get("CG_TZ"):
    os.environ["TZ"] = os.environ["CG_TZ"]
    try:
        time.tzset()
    except Exception:
        pass

_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()
_KEY_LOCKS: dict = {}
_DIRECT_OPENER = build_opener(ProxyHandler({}))


def _cached_call(key, ttl, fn):
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        klock = _KEY_LOCKS.setdefault(key, threading.Lock())
    with klock:
        now2 = time.time()
        with _CACHE_LOCK:
            hit2 = _CACHE.get(key)
            if hit2 and now2 - hit2[0] < ttl:
                return hit2[1]
        val = fn()
        with _CACHE_LOCK:
            _CACHE[key] = (time.time(), val)
        return val


def _invalidate_cache(*keys):
    with _CACHE_LOCK:
        if not keys:
            _CACHE.clear()
        else:
            for k in keys:
                _CACHE.pop(k, None)


# ---------------------------------------------------------------- 基础工具
def sh(args, timeout=8, env=None):
    try:
        e = None
        if env:
            e = dict(os.environ)
            e.update({k: str(v) for k, v in env.items()})
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=e)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:  # noqa: BLE001
        return 1, "", str(e)


def read_text(path, limit=None):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            s = f.read()
        return s[-limit:] if limit else s
    except Exception:
        return ""


def _first(pattern, text, default=None, flags=re.M):
    m = re.search(pattern, text, flags)
    return m.group(1) if m else default


# ---------------------------------------------------------------- 配置解析
def cfg_section(text, name):
    """取某个顶层键下面的缩进块。

    CLIProxyAPI 首次启动会用 YAML 库重写配置，注释会被移到顶格，
    所以「顶格注释」仍算块内；只有真正的顶层键才终止。
    """
    out = []
    inside = False
    for ln in text.splitlines():
        stripped = ln.strip()
        if not inside:
            if re.match(rf"^{re.escape(name)}:\s*$", ln):
                inside = True
            continue
        if stripped == "":
            out.append("")
            continue
        if ln[:1] in (" ", "\t"):
            out.append(ln)
            continue
        if stripped.startswith("#"):
            out.append(ln)
            continue
        break
    return "\n".join(out)


def parse_config():
    t = read_text(CONFIG)
    srv = cfg_section(t, "server")
    req = cfg_section(t, "requests")
    oauth = cfg_section(t, "oauth")
    mgmt = cfg_section(t, "management")

    keys = re.findall(r'-\s*"(sk-[^"]+)"', t) or re.findall(r"-\s*'(sk-[^']+)'", t)
    aliases = re.findall(r'^\s*alias:\s*"?([A-Za-z0-9._\-]+)"?\s*$', t, re.M)
    al_map = aliases_read()
    alias_by_channel = {ch: [x["alias"] for x in items if x.get("alias")]
                        for ch, items in al_map.items() if items}

    return {
        "config_version": _first(r"^config-version:\s*(\S+)", t, "?"),
        "host": _first(r'^\s*host:\s*"?([^"\n]*)"?', srv, "?").strip(),
        "port": int(_first(r"^\s*port:\s*(\d+)", srv, "8317")),
        "keys": keys,
        "proxy_url": _first(r'^\s*proxy-url:\s*"?([^"\n]*)"?', req, "").strip(),
        "retry": _first(r"^\s*request-retry:\s*(\d+)", t, "?"),
        "auth_dir": _first(r'^\s*auth-dir:\s*"?([^"\n]*)"?', oauth, AUTH_DIR).strip(),
        "aliases": aliases,
        "alias_by_channel": alias_by_channel,
        "mgmt_remote": _first(r"^\s*allow-remote:\s*(\S+)", mgmt, "false").strip(),
        "mgmt_key_set": bool(re.search(r"^\s*secret-key:\s*\S+", mgmt, re.M)),
    }


def mask_key(k):
    return f"{k[:11]}…{k[-4:]}" if len(k) > 18 else k


# ---------------------------------------------------------------- 进程/端口
def port_pid(port, hint_pid=None):
    if hint_pid:
        rc, out, _ = sh(["lsof", "-nP", "-a", "-p", str(hint_pid), f"-iTCP:{port}", "-sTCP:LISTEN"], timeout=4)
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) > 1 and parts[1].isdigit():
                return int(parts[1])
    rc, out, _ = sh(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"])
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) > 1 and parts[1].isdigit():
            return int(parts[1])
    return None


def _tcp_listening(port: int):
    """进程内毫秒级检测 127.0.0.1:port 是否监听；沙箱受限时返回 None 走 lsof 兜底。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.25)
    try:
        s.connect(("127.0.0.1", int(port)))
        return True
    except (ConnectionRefusedError, TimeoutError):
        return False
    except OSError as e:
        if e.errno in (1, 13, 22):
            return None
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _launch_list_map(labels):
    """一次 launchctl list 批量解析多个 label，避免重复拉起子进程。"""
    res = {lb: {"loaded": False, "pid": None, "last_exit": None} for lb in labels}
    _, out, _ = sh(["launchctl", "list"], timeout=4)
    for line in out.splitlines():
        f = line.split("\t")
        if len(f) >= 3:
            lb = f[2].strip()
            if lb in res:
                pid = int(f[0]) if f[0].strip().isdigit() else None
                res[lb] = {"loaded": True, "pid": pid, "last_exit": f[1].strip()}
    return res


def launch_status(label):
    return _launch_list_map((label,))[label]


def proc_etime(pid):
    if not pid:
        return ""
    _, out, _ = sh(["ps", "-o", "etime=", "-p", str(pid)], timeout=3)
    return out.strip()


def proc_rss(pid):
    if not pid:
        return None
    _, out, _ = sh(["ps", "-o", "rss=", "-p", str(pid)], timeout=3)
    try:
        return round(int(out.strip()) / 1024, 1)
    except Exception:
        return None


def _proc_stats(pid):
    """单次 ps 同时获取 etime 与 rss_mb。"""
    if not pid:
        return "", None
    _, out, _ = sh(["ps", "-o", "etime=,rss=", "-p", str(pid)], timeout=3)
    parts = out.strip().split()
    if not parts:
        return "", None
    etime = parts[0]
    rss_mb = None
    if len(parts) > 1:
        try:
            rss_mb = round(int(parts[1]) / 1024, 1)
        except Exception:
            rss_mb = None
    return etime, rss_mb


def _runtime_snapshot(port):
    """合并 launchctl + 进程内端口探活 + 单进程 ps 为短缓存快照（TTL 4.0s）。"""
    def _collect():
        lm = _launch_list_map((LABEL, LABEL_WD))
        la, wd = lm[LABEL], lm[LABEL_WD]
        listening = _tcp_listening(port)
        if listening is True and la["pid"]:
            pid = la["pid"]
        elif listening is False:
            pid = None
        else:
            pid = port_pid(port, hint_pid=la["pid"])
        target_pid = pid or la["pid"]
        uptime, rss_mb = _proc_stats(target_pid)
        return {"pid": pid, "la": la, "wd": wd, "uptime": uptime, "rss_mb": rss_mb}
    return _cached_call(f"rt:{port}", 4.0, _collect)


def gateway_version():
    if "ver" in _CACHE and time.time() - _CACHE["ver"][0] < 300:
        return _CACHE["ver"][1]
    rc, out, err = sh([os.path.join(GW, "cli-proxy-api"), "--version"], timeout=3)
    txt = (out or "") + (err or "")
    v = _first(r"Version:\s*([\w.\-]+)", txt, "?")
    _CACHE["ver"] = (time.time(), v)
    return v


# ---------------------------------------------------------------- 凭证
def read_auth_files():
    items = []
    for path in sorted(glob.glob(os.path.join(AUTH_DIR, "*.json"))):
        name = os.path.basename(path)
        try:
            st = os.stat(path)
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except Exception as e:  # noqa: BLE001
            items.append({"file": name, "type": "?", "error": str(e),
                          "perm": "-", "size": 0})
            continue
        expired = d.get("expired", "") or ""
        remain = None
        if expired:
            try:
                dt = datetime.fromisoformat(expired.replace("Z", "+00:00"))
                remain = int((dt - datetime.now(timezone.utc)).total_seconds())
            except Exception:
                pass
        items.append({
            "file": name,
            "type": (d.get("type") or "?").lower(),
            "email": d.get("email", "") or "",
            "disabled": bool(d.get("disabled", False)),
            "expired_at": expired,
            "remain_sec": remain,
            "has_refresh": bool(d.get("refresh_token")),
            "last_refresh": d.get("last_refresh", "") or "",
            "account": (d.get("account_id") or d.get("account_uuid") or "")[:8],
            "perm": oct(st.st_mode & 0o777)[2:],
            "size": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(timespec="seconds"),
        })
    return items


CODEX_AUTH_PATH = os.path.expanduser("~/.codex/auth.json")


def _jwt_claims(tok: str) -> dict:
    """解码 JWT payload（不验证签名，仅提取非敏感元信息）。"""
    if not tok or "." not in str(tok):
        return {}
    parts = str(tok).split(".")
    if len(parts) < 2 or not parts[1]:
        return {}
    try:
        pad = "=" * (-len(parts[1]) % 4)
        raw = base64.urlsafe_b64decode(parts[1] + pad)
        obj = json.loads(raw.decode("utf-8", "replace"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def local_codex_status() -> dict:
    """探测本机 ~/.codex/auth.json 状态（不暴露任何 token 内容）。"""
    def _collect():
        p = os.path.expanduser(CODEX_AUTH_PATH)
        if not os.path.exists(p):
            return {"available": False}
        try:
            a = json.loads(read_text(p) or "{}")
        except Exception as e:  # noqa: BLE001
            return {"available": False, "error": f"解析失败：{e}"}
        if not isinstance(a, dict):
            return {"available": False, "error": "格式不是 JSON 对象"}
        t = a.get("tokens") if isinstance(a.get("tokens"), dict) else a
        acc = (t.get("access_token") or "") if isinstance(t, dict) else ""
        if not acc:
            return {"available": False, "error": "缺少 access_token"}
        id_c = _jwt_claims(t.get("id_token") or "")
        acc_c = _jwt_claims(acc)
        acc_auth = acc_c.get("https://api.openai.com/auth") or {}
        id_auth = id_c.get("https://api.openai.com/auth") or {}
        acc_prof = acc_c.get("https://api.openai.com/profile") or {}
        email = (id_c.get("email") or acc_prof.get("email") or t.get("email") or a.get("email") or "")
        plan = (acc_auth.get("chatgpt_plan_type") or id_auth.get("chatgpt_plan_type") or "")
        exp = acc_c.get("exp")
        remain_sec = None
        if isinstance(exp, (int, float)):
            remain_sec = int(exp - time.time())
        has_refresh = bool(t.get("refresh_token"))
        imported = any(x.get("type") == "codex" and not x.get("disabled") for x in read_auth_files())
        return {
            "available": True,
            "source": "~/.codex/auth.json",
            "email": str(email or ""),
            "plan": str(plan or ""),
            "remain_sec": remain_sec,
            "expired": bool(remain_sec is not None and remain_sec <= 0),
            "has_refresh": has_refresh,
            "imported": imported,
        }
    return _cached_call("local_codex", 10, _collect)


# ---------------------------------------------------------------- 网关 API
def gw_call(path, payload=None, timeout=150):
    cfg = parse_config()
    port = cfg["port"]
    key = cfg["keys"][0] if cfg["keys"] else ""
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Authorization": f"Bearer {key}"}
    if data:
        headers["Content-Type"] = "application/json"
    t0 = time.time()
    req = Request(url, data=data, headers=headers, method="POST" if data else "GET")
    with _DIRECT_OPENER.open(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "replace")
    return json.loads(body), round((time.time() - t0) * 1000)


def fetch_models():
    def _fetch():
        try:
            d, ms = gw_call("/v1/models", timeout=3)
            ids = sorted({m.get("id") for m in d.get("data", []) if m.get("id")})
            owners = {m.get("id"): m.get("owned_by") for m in d.get("data", []) if m.get("id")}
            return {"ok": True, "models": ids, "owners": owners, "ms": ms}
        except Exception:  # noqa: BLE001
            cfg = parse_config()
            key = cfg["keys"][0] if cfg["keys"] else ""
            url = f"http://127.0.0.1:{cfg['port']}/v1/models"
            try:
                t0 = time.time()
                req = Request(url, headers={"x-api-key": key})
                with _DIRECT_OPENER.open(req, timeout=3) as r:
                    d = json.loads(r.read().decode("utf-8", "replace"))
                ids = sorted({m.get("id") for m in d.get("data", []) if m.get("id")})
                return {"ok": True, "models": ids, "ms": round((time.time() - t0) * 1000)}
            except Exception as e2:  # noqa: BLE001
                return {"ok": False, "models": [], "error": f"{type(e2).__name__}: {e2}"}
    return _cached_call("models", 60, _fetch)


def probe_model(model, prompt="说一句「链路正常」。", max_tokens=256):
    cfg = parse_config()
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    t0 = time.time()
    try:
        d, _ = gw_call("/v1/chat/completions", payload, timeout=180)
        ms = round((time.time() - t0) * 1000)
        ch = (d.get("choices") or [{}])[0]
        msg = ch.get("message") or {}
        text = (msg.get("content") or "").strip()
        reasoning = (msg.get("reasoning_content") or "").strip()
        usage = d.get("usage") or {}
        return {
            "ok": True,
            "model": d.get("model", model),
            "ms": ms,
            "text": text[:800],
            "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
            "tokens": {"in": usage.get("prompt_tokens"), "out": usage.get("completion_tokens")},
            "finish": ch.get("finish_reason"),
            "had_reasoning": bool(reasoning),
        }
    except HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:600]
        except Exception:
            pass
        return {"ok": False, "ms": round((time.time() - t0) * 1000),
                "status": e.code, "error": body or str(e)}
    except URLError as e:
        return {"ok": False, "ms": round((time.time() - t0) * 1000), "error": str(e.reason)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "ms": round((time.time() - t0) * 1000), "error": str(e)}


# ---------------------------------------------------------------- 日志与事件
def tail_lines(path, n=80):
    s = read_text(path, limit=200_000)
    return s.splitlines()[-n:]


def parse_events(n=40):
    """读 watchdog / backup 日志，按时间排序后连续重复文本合并计数。"""
    raw = []
    seq = 0
    for line in tail_lines(os.path.join(LOGS, "watchdog.log"), 400):
        if not line.strip():
            continue
        m = re.match(r"^\[([^\]]+)\]\s*(.*)$", line)
        ts, msg = (m.group(1), m.group(2)) if m else ("", line)
        low = msg.lower()
        kind = "ok"
        if "perm-fix" in low or "restart" in low or "kickstart" in low or "429" in low:
            kind = "warn"
        if "auth-fail" in low or "401" in low or "403" in low:
            kind = "bad"
        if "warn" in low or "fail" in low:
            kind = "warn" if kind == "ok" else kind
        seq += 1
        raw.append((ts or "", seq, {"t": ts, "src": "watchdog", "kind": kind, "text": msg}))
    for line in tail_lines(os.path.join(LOGS, "backup.log"), 60):
        if not line.strip():
            continue
        m = re.match(r"^\[([^\]]+)\]\s*(.*)$", line)
        ts, msg = (m.group(1), m.group(2)) if m else ("", line)
        seq += 1
        raw.append((ts or "", seq, {"t": ts, "src": "backup",
                                    "kind": "ok" if "OK" in msg else "warn", "text": msg}))

    # 按时间升序排列再合并连续相同事件，避免早前的 backup 事件压在最新 watchdog 前面
    raw.sort(key=lambda item: (item[0], item[1]))

    # 合并连续相同（同 src + 同 text）的记录
    merged = []
    for _, _, e in raw:
        last = merged[-1] if merged else None
        if last and last["src"] == e["src"] and last["text"] == e["text"]:
            last["n"] = last.get("n", 1) + 1
            last["t_last"] = e["t"]
            continue
        merged.append(dict(e, n=1, t_last=e["t"]))
    merged.reverse()
    return merged[:n]


def scan_errors(limit=40):
    """扫最近的 error-*.log，按 (类型, 模型) 聚合额度/限流/鉴权事实。"""
    files = sorted(glob.glob(os.path.join(LOGS, "error-*.log")),
                   key=os.path.getmtime, reverse=True)[:limit]
    agg = {}
    for f in files:
        t = read_text(f)
        kind = None
        if "usage_limit_reached" in t:
            kind = "额度用尽"
        elif "model_cooldown" in t:
            kind = "模型冷却"
        elif re.search(r"rate_limit_error|429", t):
            kind = "限流 429"
        elif "authentication_error" in t:
            kind = "鉴权失败"
        if not kind:
            continue
        model = _first(r'"model"\s*:\s*"([^"]+)"', t, "")
        prov = _first(r'"provider"\s*:\s*"([^"]+)"', t, "")
        reset_s = _first(r'"reset_seconds"\s*:\s*(\d+)', t)
        k = (kind, model)
        cur = agg.get(k)
        at = datetime.fromtimestamp(os.path.getmtime(f)).astimezone().isoformat(timespec="seconds")
        if cur is None:
            agg[k] = {"kind": kind, "model": model, "provider": prov,
                      "reset_sec": int(reset_s) if reset_s else None,
                      "at": at, "n": 1}
        else:
            cur["n"] += 1
            if at > cur["at"]:
                cur["at"] = at
            if reset_s and not cur["reset_sec"]:
                cur["reset_sec"] = int(reset_s)
    out = sorted(agg.values(), key=lambda x: (x["kind"] != "额度用尽", x["at"]), reverse=False)
    out.sort(key=lambda x: x["at"], reverse=True)
    return out


LOG_RE = re.compile(
    r'\[([\d\-: ]+)\]\s+\[[^\]]*\]\s+\[[^\]]*\]\s+\[[^\]]*\]\s+'
    r'(\d{3})\s*\|\s*([\d.a-zA-Zµμ]+)\s*\|\s*(\S+)\s*\|\s*(\w+)\s+"([^"]*)"')

CHAT_PATHS = ("/v1/messages", "/v1/chat/completions", "/v1/responses")


def _parse_dur_sec(raw: str) -> float:
    """解析 Gin 日志耗时（支持 ns / µs / μs / us / ms / s / 1m2.3s）。"""
    s = (raw or "").strip()
    if not s:
        return 0.0
    m_min = re.fullmatch(r'(?:(\d+)h)?(?:(\d+)m)?([\d.]+)s', s)
    if m_min and (m_min.group(1) or m_min.group(2)):
        hrs = int(m_min.group(1) or 0)
        mins = int(m_min.group(2) or 0)
        return round(hrs * 3600 + mins * 60 + float(m_min.group(3)), 6)
    for suf, scale in (("ms", 1e-3), ("µs", 1e-6), ("μs", 1e-6), ("us", 1e-6), ("ns", 1e-9), ("s", 1.0)):
        if s.endswith(suf):
            try:
                return round(float(s[:-len(suf)]) * scale, 6)
            except ValueError:
                return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0



# ---------------------------------------------------------------- 用量窗口 / 别名 / 备份

def _log_ts(raw):
    """日志时间戳转本地 datetime。
    早期日志是 plist 里 TZ=Asia/Tokyo 时写的（比本地快 9 小时），
    解析出来会落在未来 —— 检测到就减回去，避免窗口推算错位。"""
    try:
        t = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    if t > datetime.now() + timedelta(minutes=10):
        t = t - timedelta(hours=9)
    return t


def window_usage():
    """Claude Max 的额度按 5 小时窗口结算，窗口从该窗口内第一条对话请求开始计时。
    只能基于本网关自己的日志推算 —— 你在 claude.ai 网页或别的客户端上用的量看不到。"""
    W = timedelta(hours=5)
    hits = []
    for line in tail_lines(os.path.join(LOGS, "main.log"), 5000):
        m = LOG_RE.match(line)
        if not m or m.group(6) not in CHAT_PATHS:
            continue
        t = _log_ts(m.group(1))
        if t:
            hits.append(t)
    if not hits:
        return {"calls": 0, "in_window": False, "idle": True,
                "start": None, "elapsed": None, "remain": None, "note": "日志里还没有对话请求"}
    now = datetime.now()
    last = hits[-1]
    if (now - last).total_seconds() > W.total_seconds():
        return {"calls": 0, "in_window": False, "idle": True, "start": None,
                "elapsed": None, "remain": 0,
                "note": "距上次请求已超过 5 小时，窗口已重置"}
    start = last
    for i in range(len(hits) - 2, -1, -1):
        if hits[i + 1] - hits[i] >= W:
            break
        start = hits[i]
    elapsed = max(0.0, (now - start).total_seconds())
    inw = elapsed < W.total_seconds()
    return {
        "calls": sum(1 for h in hits if h >= start),
        "in_window": inw,
        "idle": False,
        "start": start.isoformat(timespec="seconds"),
        "elapsed": int(elapsed),
        "remain": int(max(0, W.total_seconds() - elapsed)) if inw else 0,
        "note": "" if inw else "窗口已走完，下一条请求会开启新窗口",
    }


def hour_hist(hours=24):
    """最近 N 个整点槽的对话请求数，用于看使用节奏。"""
    now = datetime.now()
    slots = [0] * hours
    edge = now.replace(minute=0, second=0, microsecond=0)
    oldest = edge - timedelta(hours=hours - 1)
    for line in tail_lines(os.path.join(LOGS, "main.log"), 5000):
        m = LOG_RE.match(line)
        if not m or m.group(6) not in CHAT_PATHS:
            continue
        t = _log_ts(m.group(1))
        if not t:
            continue
        if t < oldest:
            continue
        idx = int((t.replace(minute=0, second=0, microsecond=0) - oldest).total_seconds() // 3600)
        if 0 <= idx < hours:
            slots[idx] += 1
    return {"hours": hours, "slots": slots, "oldest": oldest.isoformat(timespec="seconds")}


ALIAS_CHANNELS = ("claude", "codex", "xai", "antigravity")


def aliases_read():
    """读 model-alias 三个 channel 的条目。"""
    text = read_text(CFG_PATH) or ""
    lines = text.split("\n")
    out = {c: [] for c in ALIAS_CHANNELS}
    cur = None
    i = 0
    while i < len(lines):
        ln = lines[i]
        s = ln.strip()
        if s.rstrip(":") in ALIAS_CHANNELS and ln.startswith("    ") and not ln.startswith("      "):
            cur = s.rstrip(":")
            i += 1
            continue
        if cur and s.startswith("- name:") or (cur and s.startswith("name:")):
            item = {}
            # 同一 item 的四个字段
            for k in range(4):
                if i + k >= len(lines):
                    break
                kv = lines[i + k].strip().lstrip("- ").split(":", 1)
                if len(kv) != 2:
                    break
                item[kv[0].strip()] = kv[1].strip().strip('"')
            if item.get("name"):
                out[cur].append({
                    "name": item.get("name", ""),
                    "alias": item.get("alias", ""),
                    "display_name": item.get("display-name", ""),
                    "fork": str(item.get("fork", "true")).lower() == "true",
                })
            i += 4
            continue
        if cur and ln and not ln.startswith(" "):
            cur = None
        i += 1
    return out


def _alias_block(items):
    lines = []
    for it in items:
        name = str(it.get("name", "")).strip()
        alias = str(it.get("alias", "")).strip()
        if not name or not alias:
            continue
        disp = str(it.get("display_name") or alias).strip()
        lines.append('      - name: "%s"' % name)
        lines.append('        alias: "%s"' % alias)
        lines.append('        display-name: "%s"' % disp.replace('"', "'"))
        lines.append('        fork: %s' % ("true" if it.get("fork", True) else "false"))
    return lines


def aliases_write(data):
    """整体重写 model-alias 段，写前备份，写后重启网关。"""
    text = read_text(CFG_PATH) or ""
    lines = text.split("\n")
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == "model-alias:" and l.startswith("  "))
    except StopIteration:
        return {"ok": False, "error": "config.yaml 里找不到 model-alias 段"}
    end = len(lines)
    for j in range(start + 1, len(lines)):
        l = lines[j]
        if l and not l.startswith(" "):
            end = j
            break
    new = [lines[start]]
    for ch in ALIAS_CHANNELS:
        items = (data.get(ch) or [])
        new.append("    %s:" % ch)
        if not items:
            new.append("      []")
        else:
            new.extend(_alias_block(items))
    out = "\n".join(lines[:start] + new + lines[end:])
    if out.strip() == text.strip():
        return {"ok": True, "changed": False, "message": "没有变化"}
    try:
        open(CFG_PATH + ".bak-alias-" + datetime.now().strftime("%Y%m%d%H%M%S"), "w", encoding="utf-8").write(text)
        _write_cfg_raw(out)
    except Exception as e:
        return {"ok": False, "error": "写入失败：%s" % e}
    _invalidate_cache("models")
    r = restart_gateway(30)
    return {"ok": True, "changed": True, "restart": r}


DEFAULT_CHANNEL_ALIASES = {
    "claude": [
        {"name": "claude-opus-5-5", "alias": "opus", "display_name": "Opus（最强）", "fork": True},
        {"name": "claude-sonnet-5-5", "alias": "sonnet", "display_name": "Sonnet（均衡）", "fork": True},
        {"name": "claude-haiku-5-5", "alias": "haiku", "display_name": "Haiku（最快最省）", "fork": True},
    ],
    "codex": [
        {"name": "gpt-5.3-codex", "alias": "codex", "display_name": "Codex（OpenAI）", "fork": True},
    ],
    "xai": [
        {"name": "grok-4.5", "alias": "grok", "display_name": "Grok（xAI）", "fork": True},
    ],
    "antigravity": [
        {"name": "gemini-2.5-pro", "alias": "gemini", "display_name": "Gemini Pro", "fork": True},
    ],
}


def _pick_preferred_model(platform: str, fallback: str, prefer_models=None) -> str:
    models = [str(m) for m in (prefer_models or []) if m]
    if not models:
        return fallback
    if platform == "codex":
        for cand in ("gpt-5.3-codex", "gpt-5.2-codex", "gpt-5.1-codex", "gpt-5-codex"):
            if cand in models:
                return cand
        for m in models:
            if "codex" in m:
                return m
        for m in models:
            if m.startswith("gpt-"):
                return m
    elif platform == "xai":
        for cand in ("grok-4.5", "grok-4"):
            if cand in models:
                return cand
        for m in models:
            if m.startswith("grok-"):
                return m
    elif platform == "antigravity":
        for cand in ("gemini-2.5-pro", "gemini-3-pro-preview"):
            if cand in models:
                return cand
        for m in models:
            if m.startswith("gemini-"):
                return m
    return fallback


def ensure_channel_defaults(platform: str, prefer_models=None) -> dict:
    """为首次接入的平台自动补齐默认 model-alias 与 upstream 子段（绝不覆盖已有条目）。"""
    if platform not in DEFAULT_CHANNEL_ALIASES:
        return {"ok": True, "changed": False}
    text = read_text(CFG_PATH) or ""
    if not text.strip():
        return {"ok": False, "changed": False, "error": "config.yaml 为空"}

    cur_aliases = aliases_read()
    changed_parts = []

    if not cur_aliases.get(platform):
        items = []
        for tpl in DEFAULT_CHANNEL_ALIASES[platform]:
            it = dict(tpl)
            it["name"] = _pick_preferred_model(platform, tpl["name"], prefer_models)
            items.append(it)
        cur_aliases[platform] = items

        lines = text.split("\n")
        start = next((i for i, l in enumerate(lines) if l.strip() == "model-alias:" and l.startswith("  ")), None)
        if start is not None:
            end = len(lines)
            for j in range(start + 1, len(lines)):
                l = lines[j]
                if l and not l.startswith(" ") and not l.lstrip().startswith("#"):
                    end = j
                    break
                if l.startswith("  ") and not l.startswith("   ") and not l.strip().startswith("#"):
                    end = j
                    break
            new_blk = [lines[start]]
            for ch in ALIAS_CHANNELS:
                ch_items = cur_aliases.get(ch) or []
                new_blk.append("    %s:" % ch)
                if not ch_items:
                    new_blk.append("      []")
                else:
                    new_blk.extend(_alias_block(ch_items))
            text = "\n".join(lines[:start] + new_blk + lines[end:])
            changed_parts.append("model-alias")
        else:
            oauth_idx = next((i for i, l in enumerate(lines) if re.match(r"^oauth:\s*$", l)), None)
            if oauth_idx is not None:
                ins = len(lines)
                for j in range(oauth_idx + 1, len(lines)):
                    l = lines[j]
                    if l and not l.startswith(" ") and not l.lstrip().startswith("#"):
                        ins = j
                        break
                new_blk = ["  model-alias:"]
                for ch in ALIAS_CHANNELS:
                    ch_items = cur_aliases.get(ch) or []
                    new_blk.append("    %s:" % ch)
                    if not ch_items:
                        new_blk.append("      []")
                    else:
                        new_blk.extend(_alias_block(ch_items))
                text = "\n".join(lines[:ins] + new_blk + lines[ins:])
                changed_parts.append("model-alias")

    # 按需补充 upstream.<platform> 默认子段
    upstream_tpl = {
        "codex": ["  codex:", "    disable-codex-cloaking: false"],
        "xai": ["  xai:", "    inject-x-search: false"],
    }.get(platform)
    if upstream_tpl:
        # 从待写入的 text 检查是否已有 upstream.<platform>
        u_lines = text.split("\n")
        u_start = next((i for i, l in enumerate(u_lines) if re.match(r"^upstream:\s*$", l)), None)
        if u_start is not None:
            u_end = len(u_lines)
            has_sub = False
            for j in range(u_start + 1, len(u_lines)):
                l = u_lines[j]
                if l and not l.startswith(" ") and not l.lstrip().startswith("#"):
                    u_end = j
                    break
                if l.startswith("  " + platform + ":"):
                    has_sub = True
            if not has_sub:
                # 插在尾部空行之前
                ins = u_end
                while ins > u_start + 1 and not u_lines[ins - 1].strip():
                    ins -= 1
                text = "\n".join(u_lines[:ins] + upstream_tpl + u_lines[ins:])
                changed_parts.append("upstream." + platform)
        else:
            obs_idx = next((i for i, l in enumerate(u_lines) if re.match(r"^observability:\s*$", l)), len(u_lines))
            add_blk = ["upstream:"] + upstream_tpl + [""]
            text = "\n".join(u_lines[:obs_idx] + add_blk + u_lines[obs_idx:])
            changed_parts.append("upstream." + platform)

    if not changed_parts:
        return {"ok": True, "changed": False}

    orig = read_text(CFG_PATH) or ""
    try:
        bak_path = CFG_PATH + ".bak-auto-" + datetime.now().strftime("%Y%m%d%H%M%S")
        with open(bak_path, "w", encoding="utf-8") as bf:
            bf.write(orig)
        os.chmod(bak_path, 0o600)
        _write_cfg_raw(text)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "changed": False, "error": f"写入配置失败：{e}"}
    _invalidate_cache("models")
    return {"ok": True, "changed": True, "parts": changed_parts}


BACKUP_DIR = os.path.expanduser("~/claude-gateway-backups")
KEYCHAIN_SVC = "claude-gateway-backup"


def backups_list():
    out = []
    try:
        for f in sorted(os.listdir(BACKUP_DIR), reverse=True):
            if not f.endswith(".enc"):
                continue
            p = os.path.join(BACKUP_DIR, f)
            st = os.stat(p)
            out.append({"file": f, "size": st.st_size,
                        "mtime": datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(timespec="seconds")})
    except FileNotFoundError:
        pass
    return out


def keychain_password():
    rc, out, _ = sh(["security", "find-generic-password", "-s", KEYCHAIN_SVC,
                     "-a", os.path.basename(os.path.expanduser("~")), "-w"], 6)
    return out.strip() if rc == 0 else ""


def backup_restore(name, password=""):
    """从加密备份恢复 auth 目录：先校验 tar 能否解开，再备份现有凭证，最后覆盖。"""
    if not name or "/" in name:
        return {"ok": False, "error": "文件名不合法"}
    path = os.path.join(BACKUP_DIR, name)
    if not os.path.exists(path):
        return {"ok": False, "error": "备份不存在"}
    pw = (password or "").strip() or keychain_password()
    if not pw:
        return {"ok": False, "need_password": True,
                "error": "没有可用口令：钥匙串里没找到，也没有手动输入"}
    tmp = tempfile.mkdtemp(prefix="restore-")
    try:
        rc, out, err = sh(["bash", "-c",
            "printf '%%s\\n' \"$PW\" | openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 "
            "-pass stdin -in %s -out %s" % (shlex.quote(path), shlex.quote(os.path.join(tmp, "a.tar.gz")))],
            60, env={"PW": pw})
        if rc != 0:
            return {"ok": False, "bad_password": True,
                    "error": "解密失败（口令不对或文件损坏）"}
        rc2, out2, err2 = sh(["tar", "tzf", os.path.join(tmp, "a.tar.gz")], 30)
        if rc2 != 0:
            return {"ok": False, "bad_password": True,
                    "error": "解密出来的不是有效归档 —— 口令不对"}
        rc3, _, err3 = sh(["tar", "xzf", os.path.join(tmp, "a.tar.gz"), "-C", tmp], 30)
        if rc3 != 0:
            return {"ok": False, "error": "解包失败：%s" % err3.strip()[:80]}
        src = os.path.join(tmp, "auth")
        if not os.path.isdir(src):
            return {"ok": False, "error": "备份里没有 auth 目录"}
        if os.path.isdir(AUTH_DIR):
            stake = AUTH_DIR + ".bak-" + datetime.now().strftime("%Y%m%d%H%M%S")
            shutil.copytree(AUTH_DIR, stake)
        else:
            os.makedirs(AUTH_DIR, exist_ok=True)
        n = 0
        for f in os.listdir(src):
            if f.endswith(".json"):
                shutil.copy2(os.path.join(src, f), os.path.join(AUTH_DIR, f))
                os.chmod(os.path.join(AUTH_DIR, f), 0o600)
                n += 1
        os.chmod(AUTH_DIR, 0o700)
        _invalidate_cache()
        r = restart_gateway(30)
        return {"ok": True, "restored": n, "restart": r}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def log_stats(max_lines=1500):
    def _compute():
        rows = []
        for line in tail_lines(os.path.join(LOGS, "main.log"), max_lines):
            m = LOG_RE.search(line)
            if not m:
                continue
            rows.append({"t": m.group(1), "code": m.group(2), "sec": _parse_dur_sec(m.group(3)),
                         "ip": m.group(4), "method": m.group(5), "path": m.group(6)})
        codes, paths, durs, chat_ok = {}, {}, [], 0
        for r in rows:
            codes[r["code"]] = codes.get(r["code"], 0) + 1
            paths[r["path"]] = paths.get(r["path"], 0) + 1
            if r["path"] in CHAT_PATHS:
                durs.append(r["sec"])
                if r["code"].startswith("2"):
                    chat_ok += 1
        durs.sort()

        def pct(p):
            if not durs:
                return None
            return round(durs[min(len(durs) - 1, int(len(durs) * p))], 2)

        total = len(rows)
        okn = sum(v for k, v in codes.items() if k.startswith("2"))
        return {
            "sampled": total,
            "codes": dict(sorted(codes.items())),
            "paths": dict(sorted(paths.items(), key=lambda kv: -kv[1])[:6]),
            "chat_calls": len(durs),
            "chat_ok_rate": round(chat_ok / len(durs) * 100) if durs else None,
            "p50": pct(0.5),
            "p95": pct(0.95),
            "max": round(durs[-1], 2) if durs else None,
            "ok_rate": round(okn / total * 100) if total else None,
            "last_at": rows[-1]["t"] if rows else "",
        }

    if max_lines == 1500:
        return _cached_call("logstats", 5.0, _compute)
    return _compute()


# ---------------------------------------------------------------- 汇总

# ==================== 额度余量（真实上游数据） ====================
# 两家都能拿到真数据：
#   Claude —— 响应头 anthropic-ratelimit-unified-*（直连 api.anthropic.com，带 Claude Code 指纹）
#   Codex  —— chatgpt.com/backend-api/wham/usage（必须直连，走代理会超时）
#   xAI    —— 未开放额度接口，只能标注不可得

QUOTA = {"at": 0.0, "data": None, "busy": False}
QUOTA_FILE = os.path.join(GW, ".quota.json")


def _quota_load():
    """控制台重启后把上次结果读回来，避免每次都要等 30-40 秒重取。"""
    try:
        with open(QUOTA_FILE, encoding="utf-8") as f:
            j = json.load(f)
        if isinstance(j, dict) and j.get("data"):
            QUOTA["data"] = j["data"]
            QUOTA["at"] = j.get("at") or 0.0
    except Exception:
        pass


def _quota_save():
    try:
        tmp = QUOTA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"at": QUOTA["at"], "data": QUOTA["data"]}, f, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, QUOTA_FILE)
    except Exception:
        pass


_quota_load()
QUOTA_TTL = 600  # 10 分钟；Claude 每次探测会消耗一次极小请求，不宜频繁

CLAUDE_UA = "claude-cli/2.1.280 (external, cli)"


def _iso_ts(ts):
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone().isoformat(timespec="seconds")
    except Exception:
        return None


def _hdr_dict(raw):
    d = {}
    for line in (raw or "").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            d[k.strip().lower()] = v.strip()
    return d


def quota_claude():
    files = sorted(glob.glob(os.path.join(AUTH_DIR, "claude-*.json")))
    if not files:
        return {"ok": False, "note": "还没有接入 Claude 账号"}
    try:
        d = json.loads(read_text(files[0]))
    except Exception as e:
        return {"ok": False, "note": "凭证读取失败：%s" % e}
    tok = d.get("access_token")
    if not tok:
        return {"ok": False, "note": "凭证里没有 access_token"}
    req = cfg_section(read_text(CONFIG), "requests")
    base = (re.search(r"^\s+proxy-url:\s*\"([^\"]*)\"", req, re.M) or [None, ""])[1] if req else ""
    args = ["curl", "-s", "-D", "-", "-o", "/dev/null", "-m", "45",
            "-X", "POST", "https://api.anthropic.com/v1/messages",
            "-H", "Authorization: Bearer " + tok,
            "-H", "anthropic-version: 2023-06-01",
            "-H", "anthropic-beta: oauth-2025-04-20,claude-code-20250219",
            "-H", "user-agent: " + CLAUDE_UA,
            "-H", "x-app: cli",
            "-H", "Content-Type: application/json",
            "--data-binary",
            json.dumps({"model": "claude-haiku-4-5-20251001", "max_tokens": 1,
                        "system": [{"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."}],
                        "messages": [{"role": "user", "content": "hi"}]})]
    if base.startswith("http"):
        args[1:1] = ["-x", base]
    else:
        args[1:1] = ["--noproxy", "*"]
    rc, out, err = None, "", ""
    h = {}
    for _try in range(2):
        rc, out, err = sh(args, timeout=25)
        h = _hdr_dict(out)
        if "anthropic-ratelimit-unified-7d-utilization" in h:
            break
        time.sleep(2)
    if "anthropic-ratelimit-unified-7d-utilization" not in h:
        code = ""
        for line in (out or "").splitlines():
            if line.startswith("HTTP/"):
                code = line.strip()
        prev = (QUOTA.get("data") or {}).get("claude") or {}
        if prev.get("ok"):
            p2 = dict(prev)
            p2["stale"] = True
            p2["note_stale"] = "这次没连上（网络波动），显示的是上次结果"
            return p2
        return {"ok": False, "note": "这次没连上 api.anthropic.com（%s），点「取额度」重试" % (code or "无响应")}
    def f(k):
        try:
            return float(h.get(k))
        except Exception:
            return None
    res = {
        "ok": True,
        "plan": h.get("anthropic-ratelimit-unified-representative-claim"),
        "status": h.get("anthropic-ratelimit-unified-status"),
        "h5": {
            "used": f("anthropic-ratelimit-unified-5h-utilization"),
            "status": h.get("anthropic-ratelimit-unified-5h-status"),
            "reset_at": _iso_ts(h.get("anthropic-ratelimit-unified-5h-reset")),
        },
        "d7": {
            "used": f("anthropic-ratelimit-unified-7d-utilization"),
            "status": h.get("anthropic-ratelimit-unified-7d-status"),
            "reset_at": _iso_ts(h.get("anthropic-ratelimit-unified-7d-reset")),
            "threshold": f("anthropic-ratelimit-unified-7d-surpassed-threshold"),
        },
        "overage": {
            "status": h.get("anthropic-ratelimit-unified-overage-status"),
            "reason": h.get("anthropic-ratelimit-unified-overage-disabled-reason"),
        },
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    return res


def quota_codex():
    p = os.path.expanduser("~/.codex/auth.json")
    if not os.path.exists(p):
        return {"ok": False, "note": "本机没有 Codex 凭证（~/.codex/auth.json）"}
    try:
        a = json.loads(read_text(p))
    except Exception as e:
        return {"ok": False, "note": "auth.json 解析失败：%s" % e}
    t = a.get("tokens") or a
    tok = t.get("access_token")
    if not tok:
        return {"ok": False, "note": "auth.json 里没有 access_token"}
    # chatgpt.com 直连时通时不通（实测 1.8s 成功 / 16s SSL EOF），
    # 所以前两次直连、第三次走网关同一个代理再试一次。
    req = cfg_section(read_text(CONFIG), "requests")
    px = (re.search(r'^\s+proxy-url:\s*"?([^"\n]*)"?', req, re.M) or [None, ""])[1] if req else ""
    out, err, d = "", "", None
    for _try, use_px in enumerate((False, False, bool(px))):
        vias = ["-x", px] if use_px else ["--noproxy", "*"]
        rc, out, err = sh(["curl", "-s", "-m", "18"] + vias +
                          ["-H", "Authorization: Bearer " + tok,
                           "-H", "User-Agent: codex_cli_rs/0.114.0 (Mac OS 26.3.0; arm64) vscode",
                           "-H", "Accept: application/json",
                           "https://chatgpt.com/backend-api/wham/usage"], timeout=26)
        try:
            d = json.loads(out)
            break
        except Exception:
            d = None
            time.sleep(1)
    if d is None:
        prev = (QUOTA.get("data") or {}).get("codex") or {}
        if prev.get("ok"):
            p2 = dict(prev)
            p2["stale"] = True
            p2["note_stale"] = "这次没连上（网络波动），显示的是上次结果"
            return p2
        return {"ok": False, "note": "这次没连上 chatgpt.com（网络波动），点「取额度」重试"}
    rl = d.get("rate_limit") or {}
    pw = rl.get("primary_window") or {}
    sw = rl.get("secondary_window") or {}
    cr = d.get("credits") or {}
    rc2 = d.get("rate_limit_reset_credits") or {}
    return {
        "ok": True,
        "email": d.get("email"),
        "plan": d.get("plan_type"),
        "allowed": rl.get("allowed"),
        "limit_reached": rl.get("limit_reached"),
        "primary": {
            "used": (pw.get("used_percent") or 0) / 100.0 if pw else None,
            "window_seconds": pw.get("limit_window_seconds"),
            "reset_at": _iso_ts(pw.get("reset_at")),
            "reset_after": pw.get("reset_after_seconds"),
        },
        "secondary": {
            "used": (sw.get("used_percent") or 0) / 100.0 if sw else None,
            "window_seconds": sw.get("limit_window_seconds"),
            "reset_at": _iso_ts(sw.get("reset_at")),
        } if sw else None,
        "credits": {"has": cr.get("has_credits"), "balance": cr.get("balance"), "unlimited": cr.get("unlimited")},
        "reset_credits": rc2.get("available_count"),
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def quota_xai():
    return {"ok": False, "note": "xAI 未开放额度查询接口（社区普遍如此），只能靠 429 报错反推"}


def quota_cached():
    """只读缓存，绝不触发探测 —— overview 用它。"""
    if not QUOTA["data"]:
        return {}
    d = dict(QUOTA["data"])
    d["cached"] = True
    d["age"] = int(time.time() - QUOTA["at"])
    return d


def quota_gemini():
    """Antigravity（Gemini）额度。

    实测结论：Google 没有给个人账号开用量查询接口。
      - POST daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist → 200
        能拿到 allowedTiers / ineligibleTiers（判断档位），但**没有用量字段**
      - POST .../v1internal:retrieveUserQuota → 403 SUBSCRIPTION_REQUIRED
        （那是 Gemini Code Assist 企业版才有的接口）
    所以这里只能给：档位 + 本网关今天的调用次数 + 429 反推的冷却信息。
    """
    out = {"ok": False, "provider": "gemini", "tier": None, "note": ""}
    f = None
    for name in sorted(os.listdir(AUTH_DIR)):
        if name.startswith("antigravity-") and name.endswith(".json"):
            f = os.path.join(AUTH_DIR, name); break
    if not f:
        out["note"] = "还没有接入 Gemini（Antigravity）账号"
        return out
    try:
        d = json.loads(read_text(f) or "{}")
    except Exception:
        out["note"] = "凭证文件读不出来"
        return out
    tok = d.get("access_token")
    proj = d.get("project_id") or ""
    out["email"] = d.get("email")
    out["expired"] = d.get("expired")
    if not tok:
        out["note"] = "凭证里没有 access_token"
        return out

    # 1) 档位（唯一能拿到的真实上游信息）
    rc, body, _ = sh(["curl", "-s", "-m", "20", "--noproxy", "*", "-X", "POST",
                      "https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
                      "-H", "Authorization: Bearer " + tok,
                      "-H", "Content-Type: application/json", "-d", "{}"], 25)
    tier = None
    if body:
        try:
            j = json.loads(body)
            tiers = j.get("allowedTiers") or []
            if tiers:
                tier = tiers[0].get("name") or tiers[0].get("id")
            bad = j.get("ineligibleTiers") or []
            if bad and bad[0].get("tierName"):
                out["ineligible"] = bad[0].get("tierName")
        except Exception:
            pass
    out["tier"] = tier
    out["project"] = proj

    # 2) 本网关今天（5 小时窗口内）用过几次 —— 这是可靠的本地口径
    try:
        w = window_usage()
        out["local_calls"] = w.get("calls")
        out["local_window_start"] = w.get("start")
    except Exception:
        pass

    out["ok"] = bool(tier)
    if tier:
        out["note"] = ("档位：" + tier + "。Google 未对个人账号开放 Antigravity 用量查询接口"
                       "（retrieveUserQuota 返 403 SUBSCRIPTION_REQUIRED），"
                       "下面的次数只统计经过本网关的请求。")
    else:
        out["note"] = "连不上 Google 的档位接口（可能是网络波动），稍后重试"
    return out


def quota_all(force=False):
    now = time.time()
    if not force and QUOTA["data"] and now - QUOTA["at"] < QUOTA_TTL:
        d = dict(QUOTA["data"])
        d["cached"] = True
        d["age"] = int(now - QUOTA["at"])
        return d
    if QUOTA["busy"]:
        return QUOTA["data"] or {"cached": True, "claude": None, "codex": None}
    QUOTA["busy"] = True
    try:
        prev = QUOTA["data"] or {}
        # 四家并发探测：串行时最坏 4×20 秒，并发后总时长 = 最慢那家
        from concurrent.futures import ThreadPoolExecutor
        fns = {"claude": quota_claude, "codex": quota_codex,
               "xai": quota_xai, "gemini": quota_gemini}
        out = {}
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {k: ex.submit(fn) for k, fn in fns.items()}
            for k, f in futs.items():
                try:
                    out[k] = f.result(timeout=75)
                except Exception as e:
                    out[k] = {"ok": False, "note": "探测超时或异常：%s" % str(e)[:80]}
        out["at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        out["cached"] = False
        out["age"] = 0
        # 某一家这次没连上时，绝不覆盖上一次成功的结果（这台机器到上游时通时不通）
        for k in ("claude", "codex", "gemini"):
            if not (out.get(k) or {}).get("ok"):
                old = prev.get(k) or {}
                if old.get("ok"):
                    p2 = dict(old)
                    p2["stale"] = True
                    p2["note_stale"] = "这次没连上（网络波动），下面是上次取到的"
                    out[k] = p2
                # 从来没成功过：保留这次的失败说明，不做特殊处理
        QUOTA["data"] = out
        QUOTA["at"] = now
        _quota_save()
        return out
    finally:
        QUOTA["busy"] = False



# ==================== Gemini（AI Studio API Key 通道） ====================
# CLIProxyAPI 的 gemini 走 api-keys.gemini（上游形态），与 access.api-keys（客户端 Key）是两套。

def gemini_keys():
    """从 config 里读出已配置的 Gemini key（只回掩码）。"""
    t = read_text(CONFIG)
    m = re.search(r"^api-keys:\s*$", t, re.M)
    if not m:
        return []
    seg = t[m.end():]
    gm = re.search(r"^  gemini:\s*$", seg, re.M)
    if not gm:
        return []
    body = seg[gm.end():]
    body = re.split(r"^  [a-z-]+:\s*$", body, 1, re.M)[0]
    return [{"mask": k[:6] + "…" + k[-4:], "len": len(k)}
            for k in re.findall(r'api-key:\s*"([^"]+)"', body)]


def gemini_set(keys):
    """写 api-keys.gemini 段；keys 为空则删除该段。"""
    keys = [k.strip() for k in (keys or []) if k.strip()]
    t = read_text(CONFIG)
    lines = t.split("\n")

    # 删掉已有顶级 api-keys 段（连同其下缩进内容）
    out, skip = [], False
    for ln in lines:
        if re.match(r"^api-keys:\s*$", ln):
            skip = True
            continue
        if skip:
            if ln.startswith((" ", "\t")) or not ln.strip():
                continue
            skip = False
        out.append(ln)
    lines = out

    if keys:
        block = ["api-keys:", "  gemini:", "    - name: gemini",
                 '      base-url: "https://generativelanguage.googleapis.com"',
                 "      models:",
                 '        - name: "gemini-3-pro-preview"',
                 '          alias: "gemini"',
                 '          display-name: "Gemini 3 Pro（最强）"',
                 '        - name: "gemini-3.5-flash-lite"',
                 '          alias: "gemini-flash"',
                 '          display-name: "Gemini 3.5 Flash（快）"',
                 "      keys:"]
        for k in keys:
            block.append('        - api-key: "%s"' % k)
        # 插到 access: 之前（保持可读顺序）
        idx = next((i for i, l in enumerate(lines) if re.match(r"^access:\s*$", l)), len(lines))
        lines = lines[:idx] + block + [""] + lines[idx:]

    _write_cfg_raw("\n".join(lines))
    _invalidate_cache()
    return {"ok": True, "count": len(keys),
            "restart": restart_gateway()}


def overview():
    cfg = parse_config()
    port = cfg["port"]
    snap = _runtime_snapshot(port)
    pid, la, wd = snap["pid"], snap["la"], snap["wd"]
    models = fetch_models()
    _, _, key_items = _keys_block(_read_cfg_raw().split("\n"))
    key_labels = {k: lab for k, lab in key_items}
    win_u, hist_u, ev_list, err_list = _cached_call(
        f"ov_diag:{LOGS}",
        3.5,
        lambda: (window_usage(), hour_hist(), parse_events(30), scan_errors()),
    )
    return {
        "now": datetime.now().astimezone().isoformat(timespec="seconds"),
        "usage": {"window": win_u, "hist": hist_u},
        "quota": quota_cached(),
        "ui_auth": {"enabled": auth_enabled()},
        "local_creds": {"codex": local_codex_status()},
        "gateway": {
            "alive": bool(pid),
            "pid": pid if pid else la["pid"],
            "port": port,
            "host": cfg["host"],
            "uptime": snap["uptime"],
            "rss_mb": snap["rss_mb"],
            "version": gateway_version(),
            "config_version": cfg["config_version"],
        },
        "launchd": {"gateway": la, "watchdog": wd, "labels": [LABEL, LABEL_WD]},
        "config": {
            "host": cfg["host"],
            "keys": [{"masked": mask_key(k), "raw_last4": k[-4:], "label": key_labels.get(k, "")} for k in cfg["keys"]],
            "key_count": len(cfg["keys"]),
            "proxy_url": cfg["proxy_url"] or "（直连）",
            "retry": cfg["retry"],
            "auth_dir": cfg["auth_dir"].replace(os.path.expanduser("~"), "~"),
            "mgmt_remote": cfg["mgmt_remote"],
            "mgmt_key_set": cfg["mgmt_key_set"],
            "aliases": cfg["aliases"],
            "alias_by_channel": cfg.get("alias_by_channel", {}),
        },
        "auth": read_auth_files(),
        "models": models,
        "events": ev_list,
        "issues": err_list,
        "logs": log_stats(),
        "lan_ip": lan_ip(),
    }


TS_BIN = "/usr/local/bin/tailscale"


def _find_ts_bin():
    if os.path.isabs(TS_BIN) and os.path.exists(TS_BIN):
        return TS_BIN
    return shutil.which("tailscale") or ""


def _has_ts_daemon():
    env_sock = os.environ.get("TS_SOCKET", "").strip()
    if env_sock and os.path.exists(env_sock):
        return True
    if os.path.exists("/var/run/tailscaled.socket"):
        return True
    if glob.glob(os.path.expanduser("~/Library/Group Containers/*.io.tailscale.ipn.macos/sameuserproof-*")):
        return True
    return False


def tailscale_ip():
    """Tailscale 的 100.x 地址。固定不变，比局域网 IP 可靠得多。"""
    if not _has_ts_daemon():
        return ""
    b = _find_ts_bin()
    if not b:
        return ""
    rc, out, _ = sh([b, "ip", "-4"], timeout=0.5)
    if rc == 0:
        v = out.strip().splitlines()
        if v and v[0].startswith("100."):
            return v[0]
    return ""


def tailscale_dns():
    """MagicDNS 全名（可能被 Clash 的 fake-ip 劫持，仅作展示）。"""
    if not _has_ts_daemon():
        return ""
    b = _find_ts_bin()
    if not b:
        return ""
    rc, out, _ = sh([b, "status", "--json"], timeout=3)
    if rc == 0:
        try:
            return (json.loads(out).get("Self") or {}).get("DNSName", "").rstrip(".")
        except Exception:
            pass
    return ""


def tailscale_state():
    if not _has_ts_daemon():
        return ""
    b = _find_ts_bin()
    if not b:
        return ""
    rc, out, _ = sh([b, "status", "--json"], timeout=3)
    if rc == 0:
        try:
            d = json.loads(out)
            return d.get("BackendState", "")
        except Exception:
            pass
    return ""


def _lan_ip_uncached():
    ts = tailscale_ip()
    if ts:
        return ts
    _, out, _ = sh(["ipconfig", "getifaddr", "en0"], timeout=2)
    return out.strip()


def lan_ip():
    """对外地址：优先 Tailscale 固定 IP，退回局域网 IP（TTL 60s）。"""
    return _cached_call("lan_ip", 60, _lan_ip_uncached)


# ---------------------------------------------------------------- 服务控制
def service_action(action):
    if action == "restart":
        rc, out, err = sh(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LABEL}"], timeout=20)
    elif action == "stop":
        rc, out, err = sh(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], timeout=20)
    elif action == "start":
        plist = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
        sh(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], timeout=20)
        rc, out, err = sh(["launchctl", "bootstrap", f"gui/{os.getuid()}", plist], timeout=20)
    else:
        return {"ok": False, "error": "unknown action"}
    time.sleep(2.5)
    cfg = parse_config()
    pid = port_pid(cfg["port"])
    _invalidate_cache(f"rt:{cfg['port']}", "models")
    return {"ok": rc == 0 and bool(pid), "rc": rc,
            "out": (out or "").strip()[:400], "err": (err or "").strip()[:400],
            "pid": pid}


def run_backup():
    path = os.path.join(GW, "backup.sh")
    if not os.path.exists(path):
        return {"ok": False, "file": "", "error": "backup.sh 不存在"}
    rc, out, err = sh(["bash", path], timeout=60)
    latest = ""
    err_msg = ""
    if rc == 0:
        bks = backups_list()
        if bks:
            latest = bks[0].get("file") or ""
    else:
        err_msg = (err or out or "").strip()[-400:] or f"退出码 {rc}"
    return {"ok": rc == 0, "file": latest, "error": err_msg,
            "out": (out or "").strip()[-600:],
            "err": (err or "").strip()[-400:],
            "log": tail_lines(os.path.join(LOGS, "backup.log"), 5)}



# ---------------------------------------------------------------- 账号登录（pty 驱动）
# 三家的登录都跑在伪终端里：CLIProxyAPI 在无 tty 时会因 stdin EOF 直接退出，
# 给它一个 pty 就能稳定跑完，并且能从输出里实时抓出设备码 / 授权链接。
LOGIN_SPECS = {
    "claude": {"flag": "-claude-login",        "ttl": 420,  "label": "Claude Max"},
    "codex":  {"flag": "-codex-device-login",  "ttl": 960,  "label": "ChatGPT"},
    "xai":    {"flag": "-xai-login",           "ttl": 1860, "label": "xAI Grok"},
    "antigravity": {"flag": "-antigravity-login", "ttl": 900, "label": "Gemini（Google 账号）"},
}

_JOBS: dict = {}
_JOB_LOCK = threading.Lock()
_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\r")


def _clean(s: str) -> str:
    return _ANSI.sub("", s).replace("\x08", "")


def _parse_login(platform: str, text: str) -> dict:
    """从累计输出里解析 url / code / 结果。返回增量字段。"""
    out = {}
    t = _clean(text)
    if platform == "xai":
        m = re.search(r"(https://accounts\.x\.ai/oauth2/device\S*)", t)
        if m:
            out["url"] = m.group(1)
        m = re.search(r"enter this code:\s*([A-Za-z0-9][A-Za-z0-9\-]{3,})", t)
        if m:
            out["code"] = m.group(1).upper()
    elif platform == "antigravity":
        m = re.search(r"(https://accounts\.google\.com/o/oauth2/v2/auth\S+)", t)
        if m:
            out["url"] = m.group(1)
    elif platform == "codex":
        m = re.search(r"(https://auth\.openai\.com/codex/device\S*)", t)
        if m:
            out["url"] = m.group(1)
        m = re.search(r"Codex device code:\s*([A-Za-z0-9][A-Za-z0-9\-]{3,})", t)
        if m:
            out["code"] = m.group(1).upper()
    else:  # claude
        m = re.search(r"(https://claude\.ai/[^\s\"']*oauth[^\s\"']*)", t)
        if m:
            out["url"] = m.group(1)
        m = re.search(r"(https://claude\.ai/oauth/authorize\S*)", t)
        if m:
            out["url"] = m.group(1)
    # 结果
    m = re.search(r"Saving credentials to\s*(\S+\.json)", t)
    if m:
        out["auth_file"] = os.path.basename(m.group(1))
    if re.search(r"authentication successful|Successfully logged in|Authenticated as", t, re.I):
        # 关键：日志说成功不等于凭证真的写进来了。
        # 实测踩过：旧账号被 Anthropic 标记 account_on_hold，OAuth 走完了、
        # 日志也打 "authentication successful"，但网关拒绝落盘 → auth/ 目录空，
        # 前端却显示成功，用户以为接通了。
        plat_prefix = {"claude": "claude-", "codex": "codex-", "xai": "xai-",
                       "antigravity": "antigravity-"}.get(platform, platform + "-")
        try:
            got = [f for f in os.listdir(AUTH_DIR) if f.startswith(plat_prefix) and f.endswith(".json")]
        except OSError:
            got = []
        if got:
            out["state"] = "success"
            out["auth_file"] = sorted(got)[-1]
        else:
            out["state"] = "failed"
            out["error"] = ("授权流程报告成功，但凭证没有落盘。常见原因："
                            "①账号被平台限制（日志里会出现 account_on_hold / invalid_grant）；"
                            "②54545 端口被占用（网关重启或上次登录残留）。"
                            "先看下面日志最后几行，再重试。")
        return out

    if re.search(r"timed out|expired|authentication failed|invalid_grant", t, re.I):
        out["state"] = "failed"
    if ("Waiting for authorization" in t or "Waiting for antigravity authentication callback" in t) \
            and "state" not in out:
        out["state"] = "waiting"
    return out


def _login_worker(platform: str):
    """用 pty.openpty + subprocess 驱动登录子进程。

    不能在多线程的 HTTP 服务里用 pty.fork()——fork 会复制整个进程的线程状态，
    在 ThreadingHTTPServer 下容易让子进程卡死在自己的锁里。
    """
    spec = LOGIN_SPECS[platform]
    job = _JOBS[platform]
    proc = None
    mfd = sfd = None
    buf = ""
    try:
        # antigravity 走 Google 回调式授权，不需要 stdin 交互 —— 用普通管道反而更稳
        # （pty 下它会立刻退出，实测日志只有版本行）
        if platform == "antigravity":
            proc = subprocess.Popen(
                [BIN, "-config", CONFIG, spec["flag"]],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                close_fds=True, start_new_session=True, cwd=GW, bufsize=0,
            )
            mfd = proc.stdout.fileno()
        else:
            mfd, sfd = pty.openpty()
            proc = subprocess.Popen(
                [BIN, "-config", CONFIG, spec["flag"]],
                stdin=sfd, stdout=sfd, stderr=sfd,
                close_fds=True, start_new_session=True, cwd=GW,
            )
            os.close(sfd)
            sfd = None
        job["pid"] = proc.pid
        t0 = time.time()
        while True:
            if job.get("cancelled"):
                break
            if time.time() - t0 > spec["ttl"]:
                job["state"] = "expired"
                break
            try:
                r, _, _ = select.select([mfd], [], [], 0.35)
            except Exception:
                break
            if r:
                try:
                    chunk = os.read(mfd, 8192)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                buf += chunk.decode("utf-8", "replace")
                job["log"] = _clean(buf)[-3000:]
                upd = _parse_login(platform, buf)
                job.update(upd)
                if upd.get("state") in ("success", "failed"):
                    break
            if proc.poll() is not None:
                # 进程退出后把残余输出读完
                time.sleep(0.2)
                try:
                    while True:
                        r, _, _ = select.select([mfd], [], [], 0.2)
                        if not r:
                            break
                        chunk = os.read(mfd, 8192)
                        if not chunk:
                            break
                        buf += chunk.decode("utf-8", "replace")
                except Exception:
                    pass
                job["log"] = _clean(buf)[-3000:]
                job.update(_parse_login(platform, buf))
                break
        if job.get("state") not in ("success", "failed", "expired"):
            if job.get("cancelled"):
                job["state"] = "cancelled"
            else:
                tail = _clean(buf)[-600:].lower()
                job["state"] = "failed" if ("error" in tail or "fail" in tail) else "cancelled"
        if job.get("state") == "success":
            _invalidate_cache("local_codex", "models")
            try:
                cfg_res = ensure_channel_defaults(platform)
                if cfg_res.get("changed") and port_pid(parse_config()["port"]):
                    _restart_async()
            except Exception:
                pass
        job["ended"] = time.time()
    except Exception as e:  # noqa: BLE001
        job["state"] = "failed"
        job["error"] = f"{type(e).__name__}: {e}"
    finally:
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        for f in (mfd, sfd):
            if f is not None:
                try:
                    os.close(f)
                except Exception:
                    pass
        job["running"] = False


def login_start(platform: str) -> dict:
    # Claude 的 OAuth 走 localhost:54545 回调，被占就必然失败（而且失败得很隐蔽）
    if platform == "claude" and port_pid(54545):
        return {"ok": False, "error": "54545 端口被占用（PID %s）。这是 Claude 的 OAuth 回调端口，"
                                      "被占会导致授权走完却不落盘。先结束那个进程再试。" % port_pid(54545)}

    if platform not in LOGIN_SPECS:
        return {"ok": False, "error": "未知平台"}
    if not os.path.exists(BIN):
        return {"ok": False, "error": "找不到 cli-proxy-api 可执行文件"}
    with _JOB_LOCK:
        cur = _JOBS.get(platform)
        if cur and cur.get("running"):
            return {"ok": True, "job": _job_view(platform), "note": "已有进行中的授权"}
        _JOBS[platform] = {
            "platform": platform, "label": LOGIN_SPECS[platform]["label"],
            "state": "starting", "url": None, "code": None, "auth_file": None,
            "started": time.time(), "ttl": LOGIN_SPECS[platform]["ttl"],
            "running": True, "cancelled": False, "log": "", "pid": None,
        }
        t = threading.Thread(target=_login_worker, args=(platform,), daemon=True)
        t.start()
    return {"ok": True, "job": _job_view(platform)}


def login_cancel(platform: str) -> dict:
    with _JOB_LOCK:
        job = _JOBS.get(platform)
        if not job or not job.get("running"):
            return {"ok": False, "error": "没有进行中的授权"}
        job["cancelled"] = True
        p = job.get("pid")
    if p:
        try:
            os.kill(p, signal.SIGTERM)
        except Exception:
            pass
    return {"ok": True, "job": _job_view(platform)}


def import_local_codex() -> dict:
    """将本机 ~/.codex/auth.json 安全转换并导入网关 AUTH_DIR。"""
    p = os.path.expanduser(CODEX_AUTH_PATH)
    if not os.path.exists(p):
        return {"ok": False, "error": "本机未找到 ~/.codex/auth.json"}
    try:
        a = json.loads(read_text(p) or "{}")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"~/.codex/auth.json 解析失败：{e}"}
    if not isinstance(a, dict):
        return {"ok": False, "error": "~/.codex/auth.json 格式无效"}
    t = a.get("tokens") if isinstance(a.get("tokens"), dict) else a
    acc = (t.get("access_token") or "") if isinstance(t, dict) else ""
    ref = (t.get("refresh_token") or "") if isinstance(t, dict) else ""
    idt = (t.get("id_token") or "") if isinstance(t, dict) else ""
    if not acc:
        return {"ok": False, "error": "~/.codex/auth.json 里没有 access_token"}

    id_c = _jwt_claims(idt)
    acc_c = _jwt_claims(acc)
    acc_auth = acc_c.get("https://api.openai.com/auth") or {}
    id_auth = id_c.get("https://api.openai.com/auth") or {}
    acc_prof = acc_c.get("https://api.openai.com/profile") or {}

    exp = acc_c.get("exp")
    if isinstance(exp, (int, float)) and exp <= time.time() and not ref:
        return {"ok": False, "error": "本机 ~/.codex/auth.json 令牌已过期且无刷新令牌，请先运行 codex login 或使用扫码授权"}

    email = str(id_c.get("email") or acc_prof.get("email") or t.get("email") or a.get("email") or "")
    plan = str(acc_auth.get("chatgpt_plan_type") or id_auth.get("chatgpt_plan_type") or "")
    account_id = str(t.get("account_id") or acc_auth.get("chatgpt_account_id") or id_auth.get("chatgpt_account_id") or "")
    expired_iso = (
        datetime.fromtimestamp(float(exp), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if isinstance(exp, (int, float)) else ""
    )
    last_refresh = str(a.get("last_refresh") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

    doc = {
        "type": "codex",
        "email": email,
        "id_token": idt,
        "access_token": acc,
        "refresh_token": ref,
        "account_id": account_id,
        "last_refresh": last_refresh,
        "expired": expired_iso,
    }

    safe_email = re.sub(r"[^A-Za-z0-9_.@-]+", "_", email or (account_id[:8] if account_id else "local"))
    safe_plan = re.sub(r"[^a-z0-9_-]+", "", plan.lower()) if plan else ""
    fname = f"codex-{safe_email}{'-' + safe_plan if safe_plan else ''}.json"
    os.makedirs(AUTH_DIR, mode=0o700, exist_ok=True)
    dst = os.path.join(AUTH_DIR, fname)
    tmp = dst + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, dst)

    cfg_res = ensure_channel_defaults("codex")
    _invalidate_cache("local_codex", "models")

    alive_pid = port_pid(parse_config()["port"])
    if cfg_res.get("changed") and alive_pid:
        restart_gateway(15)
    elif alive_pid:
        time.sleep(0.8)
    _invalidate_cache("models")
    fm = fetch_models()
    if alive_pid and fm.get("ok") and fm.get("models"):
        owners = fm.get("owners") or {}
        oai_models = [m for m in fm["models"] if owners.get(m) == "openai" or m.startswith(("gpt-", "codex"))]
        if oai_models:
            al = aliases_read()
            c_list = al.get("codex") or []
            if c_list and c_list[0].get("name") not in fm["models"]:
                best = _pick_preferred_model("codex", c_list[0]["name"], oai_models)
                if best and best != c_list[0]["name"]:
                    c_list[0]["name"] = best
                    al["codex"] = c_list
                    aliases_write(al)
    return {
        "ok": True,
        "auth_file": fname,
        "email": email,
        "plan": plan,
        "alias_added": bool(cfg_res.get("changed")),
    }


def _job_view(platform: str) -> dict:
    job = _JOBS.get(platform)
    if not job:
        return {"platform": platform, "state": "idle", "running": False}
    elapsed = int(time.time() - job.get("started", time.time()))
    return {
        "platform": platform,
        "label": job.get("label"),
        "state": job.get("state"),
        "url": job.get("url"),
        "code": job.get("code"),
        "auth_file": job.get("auth_file"),
        "running": bool(job.get("running")),
        "elapsed": elapsed,
        "remain": max(0, job.get("ttl", 0) - elapsed),
        "error": job.get("error"),
        "log": (job.get("log") or "")[-700:],
    }


def login_status() -> dict:
    with _JOB_LOCK:
        jobs = {p: _job_view(p) for p in _JOBS}
    have = {a.get("type"): a for a in read_auth_files()}
    return {"jobs": jobs, "have": sorted(have.keys())}


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "GatewayOne/1.0"

    def log_message(self, *a):  # 静默
        pass

    def _send(self, data, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass
        ms = round((time.time() - getattr(self, "_t0", time.time())) * 1000)
        if (code >= 400 and code != 401) or ms >= 1000:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{ts}] {getattr(self, 'command', '?')} {self.path} -> {code} ({ms}ms)",
                  file=sys.stderr, flush=True)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", code)

    def _guard(self, path):
        """返回 True 表示放行；否则已写好 401"""
        if not auth_enabled():
            return True
        if path in AUTH_OPEN_PATHS:
            return True
        if _sess_ok(self.headers.get("Cookie")):
            return True
        self._json({"error": "需要登录", "need_login": True}, 401)
        return False

    def do_GET(self):  # noqa: N802
        self._t0 = time.time()
        p = urlparse(self.path).path
        if not self._guard(p):
            return
        try:
            if p in ("/", "/index.html"):
                html = read_text(os.path.join(BASE, "index.html"))
                return self._send(html.encode("utf-8"), "text/html; charset=utf-8")
            if p == "/favicon.ico":
                return self._send(FAVICON_SVG, "image/svg+xml")
            if p == "/api/quota":
                return self._json(quota_all(force=("force=1" in (self.path or ""))))
            if p == "/api/channel":
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                return self._json(channel_detail((qs.get("id") or [""])[0]))
            if p == "/api/usage":
                return self._json({"window": window_usage(), "hist": hour_hist(), "stats": log_stats()})
            if p == "/api/gemini":
                return self._json({"keys": gemini_keys()})
            if p == "/api/aliases":
                return self._json({"aliases": aliases_read(), "channels": list(ALIAS_CHANNELS)})
            if p == "/api/backups":
                return self._json({"backups": backups_list()})
            if p == "/api/overview":
                return self._json(overview())
            if p == "/api/events":
                return self._json({"events": parse_events(60)})
            if p == "/api/login/status":
                return self._json(login_status())
            if p == "/api/clients":
                return self._json({"clients": clients_detect()})
            if p == "/api/settings":
                return self._json(settings_get())
            if p == "/api/auth/state":
                return self._json(auth_state(self))
            if p == "/api/doctor":
                return self._json(doctor_status())
            if p == "/api/doctor/text":
                return self._json({"text": doctor_text()})
            if p == "/api/keys":
                return self._json({"keys": keys_list()})
            if p == "/api/raw":
                cfg = parse_config()
                return self._json({"main": tail_lines(os.path.join(LOGS, "main.log"), 120),
                                   "watchdog": tail_lines(os.path.join(LOGS, "watchdog.log"), 60),
                                   "port": cfg["port"]})
            return self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            sys.stderr.flush()
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):  # noqa: N802
        self._t0 = time.time()
        p = urlparse(self.path).path
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            body = {}
        if not self._guard(p):
            return
        try:
            if p == "/api/test":
                model = body.get("model") or "haiku"
                prompt = body.get("prompt") or "说一句「链路正常」。"
                return self._json(probe_model(model, prompt,
                                              int(body.get("max_tokens") or 256)))
            if p == "/api/service":
                return self._json(service_action(body.get("action") or ""))
            if p == "/api/backup":
                return self._json(run_backup())
            if p == "/api/key":
                idx = int(body.get("i", 0))
                keys = parse_config()["keys"]
                if 0 <= idx < len(keys):
                    return self._json({"ok": True, "key": keys[idx]})
                return self._json({"ok": False, "error": "index out of range"}, 400)
            if p == "/api/login/start":
                return self._json(login_start(body.get("platform") or ""))
            if p == "/api/login/cancel":
                return self._json(login_cancel(body.get("platform") or ""))
            if p == "/api/login/import-codex":
                return self._json(import_local_codex())
            if p == "/api/keys/add":
                return self._json(keys_add(body.get("label") or ""))
            if p == "/api/keys/del":
                return self._json(keys_del(body.get("key") or ""))
            if p == "/api/keys/label":
                return self._json(keys_relabel(body.get("key") or "", body.get("label") or ""))
            if p == "/api/clients/wire":
                return self._json(client_wire(body.get("id") or ""))
            if p == "/api/clients/unwire":
                return self._json(client_unwire(body.get("id") or ""))
            if p == "/api/settings":
                return self._json(settings_set(body or {}))
            if p == "/api/doctor/start":
                return self._json(doctor_start(body.get("mode") or "deep"))
            if p == "/api/notify/test":
                ok = do_notify("Gateway One", "通知已开启 —— 出问题会在这里告诉你", force=True)
                return self._json({"ok": bool(ok)})
            if p == "/api/gemini/set":
                return self._json(gemini_set(body.get("keys") or []))
            if p == "/api/aliases/save":
                r = aliases_write(body.get("aliases") or {})
                return self._json(r)
            if p == "/api/restore":
                r = backup_restore(body.get("file", ""), body.get("password", ""))
                return self._json(r)
            if p == "/api/auth/login":
                r = auth_login(body.get("password") or "")
                if r.get("ok") and r.get("token"):
                    self.send_response(200)
                    payload = json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Set-Cookie",
                                     f"go_sess={r['token']}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESS_TTL}")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                return self._json(r, 401 if not r.get("ok") else 200)
            if p == "/api/auth/set":
                return self._json(auth_set(body.get("password") or ""))
            if p == "/api/auth/off":
                return self._json(auth_off(body.get("password") or ""))
            if p == "/api/ping":
                return self._json({"ok": True, "t": time.time()})
            return self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            sys.stderr.flush()
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)


# ============================================================================
# 第一批能力：Key 管理 / 客户端一键接入 / 设置项
# ============================================================================
CFG_PATH = os.path.join(GW, "config.yaml")
GO_BAK = ".go-bak"


def _read_cfg_raw():
    return read_text(CFG_PATH) or ""


def _write_cfg_raw(text):
    tmp = CFG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CFG_PATH)


def _keys_block(lines):
    """定位 access.api-keys 段，返回 (insert_at, end, [(key,label)])"""
    for i, ln in enumerate(lines):
        if re.match(r"^\s+api-keys:\s*$", ln):
            out, j = [], i + 1
            while j < len(lines) and re.match(r"^\s+-\s", lines[j]):
                m = re.match(r'^\s+-\s*"([^"]+)"\s*(?:#\s*(.*))?$', lines[j])
                if m:
                    out.append((m.group(1), (m.group(2) or "").strip()))
                j += 1
            return i + 1, j, out
    return None, None, []


def keys_list():
    _, _, items = _keys_block(_read_cfg_raw().split("\n"))
    return [{"key": k, "masked": mask_key(k), "label": lab,
             "last4": k[-4:]} for k, lab in items]


def _gen_key():
    _, raw, _ = sh(["openssl", "rand", "-hex", "20"], timeout=5)
    return "sk-local-" + ((raw or "").strip() or os.urandom(20).hex())


def _rewrite_keys(items):
    """items = [(key,label)] -> 重写配置文件里的 api-keys 段"""
    lines = _read_cfg_raw().split("\n")
    ins, end, _ = _keys_block(lines)
    if ins is None:
        raise RuntimeError("config.yaml 里找不到 access.api-keys 段")
    pad = "    "
    new = []
    for k, lab in items:
        new.append(f'{pad}- "{k}"' + (f" # {lab}" if lab else ""))
    lines[ins:end] = new
    _write_cfg_raw("\n".join(lines))
    return len(items)


def restart_gateway(wait=30):
    """重启网关并等它重新监听（必须看到 PID 变化，否则视为没重启）"""
    cfg = parse_config()
    port = cfg["port"]
    old = port_pid(port)
    if sys.platform == "darwin":
        sh(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LABEL}"], timeout=10)
    else:
        sh(["systemctl", "--user", "restart", LABEL], timeout=10)
    t0 = time.time()
    while time.time() - t0 < wait:
        pid = port_pid(port)
        if pid and pid != old:
            _invalidate_cache(f"rt:{port}", "models")
            return {"ok": True, "seconds": round(time.time() - t0, 1),
                    "port": port, "pid": pid}
        time.sleep(0.6)
    pid = port_pid(port)
    _invalidate_cache(f"rt:{port}", "models")
    return {"ok": False, "seconds": round(time.time() - t0, 1), "port": port,
            "pid": pid, "error": "端口仍在旧进程手上，重启未生效" if pid else "端口没有重新监听"}


def keys_add(label=""):
    items = [(k, l) for k, l in _keys_block(_read_cfg_raw().split("\n"))[2]]
    if len(items) >= 20:
        return {"ok": False, "error": "Key 数量上限 20 把"}
    key = _gen_key()
    items.append((key, label or f"设备 {len(items) + 1}"))
    _rewrite_keys(items)
    r = restart_gateway()
    return {"ok": True, "key": key, "masked": mask_key(key),
            "count": len(items), "restart": r}


def _restart_async():
    """后台重启网关，不阻塞 HTTP 响应。"""
    def _go():
        try:
            restart_gateway()
        except Exception:
            pass
    threading.Thread(target=_go, daemon=True).start()


def keys_del(key):
    items = [(k, l) for k, l in _keys_block(_read_cfg_raw().split("\n"))[2]]
    keep = [(k, l) for (k, l) in items if k != key]
    if not keep:
        return {"ok": False, "error": "不能删掉最后一把 Key，否则所有客户端都会 401"}
    if len(keep) == len(items):
        return {"ok": False, "error": "没找到这把 Key"}
    _rewrite_keys(keep)
    _invalidate_cache("models")
    _restart_async()
    return {"ok": True, "count": len(keep), "restart": {"ok": True, "async": True,
            "note": "网关正在重启，约 1 秒后生效"}}


def keys_relabel(key, label):
    items = [(k, l) for k, l in _keys_block(_read_cfg_raw().split("\n"))[2]]
    hit = False
    for i, (k, l) in enumerate(items):
        if k == key:
            items[i] = (k, (label or "").strip()[:40])
            hit = True
    if not hit:
        return {"ok": False, "error": "没找到这把 Key"}
    _rewrite_keys(items)
    return {"ok": True, "note": "备注不影响运行，无需重启"}


# ---------- 客户端检测与一键接入 ----------
HOME = os.path.expanduser("~")
CLAUDE_JSON = os.path.join(HOME, ".claude", "settings.json")
CODEX_TOML = os.path.join(HOME, ".codex", "config.toml")
CODEX_BIN = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
PROVIDER_ID = "gatewayone"


def _gateway_base():
    cfg = parse_config()
    host = cfg["host"] or "127.0.0.1"
    if host in ("0.0.0.0", ""):
        host = "127.0.0.1"
    return f"http://{host}:{cfg['port']}"


def _cli_bins_cached():
    def _probe():
        has_claude = bool(shutil.which("claude")) or bool(
            (sh(["bash", "-lc", "command -v claude"], timeout=3)[1] or "").strip()
        )
        has_codex = bool(shutil.which("codex")) or os.path.exists(CODEX_BIN) or bool(
            (sh(["bash", "-lc", "command -v codex"], timeout=3)[1] or "").strip()
        )
        return {"claude": has_claude, "codex": has_codex}
    return _cached_call("cli_bins", 300, _probe)


def clients_detect():
    base = _gateway_base()
    bins = _cli_bins_cached()
    out = []

    # Claude Code —— 读 settings.json 的 env 段
    c = {"id": "claude-code", "name": "Claude Code", "file": CLAUDE_JSON,
         "exists": os.path.exists(CLAUDE_JSON), "wired": False,
         "cmd": bins["claude"],
         "protocol": "Anthropic", "base": base}
    if c["exists"]:
        try:
            d = json.loads(read_text(CLAUDE_JSON) or "{}")
            env = d.get("env") or {}
            c["wired"] = str(env.get("ANTHROPIC_BASE_URL", "")).rstrip("/") == base
            c["current"] = env.get("ANTHROPIC_BASE_URL") or ""
            c["model"] = env.get("ANTHROPIC_MODEL") or ""
        except Exception as e:  # noqa: BLE001
            c["error"] = f"settings.json 解析失败: {e}"
    out.append(c)

    # Codex CLI —— 读 config.toml 里有没有 [model_providers.gatewayone]
    c = {"id": "codex", "name": "Codex CLI", "file": CODEX_TOML,
         "exists": os.path.exists(CODEX_TOML), "wired": False,
         "cmd": bins["codex"],
         "protocol": "OpenAI", "base": base + "/v1"}
    if c["exists"]:
        t = read_text(CODEX_TOML) or ""
        c["wired"] = f"[model_providers.{PROVIDER_ID}]" in t
        m = re.search(r'^model_provider\s*=\s*"([^"]+)"', t, re.M)
        c["current"] = m.group(1) if m else ""
    out.append(c)

    # dsh —— 只能手动填（配置不在磁盘上可安全改的明文位置）
    out.append({"id": "dsh", "name": "dsh（DeepSeek Harness）",
                "exists": os.path.isdir(os.path.join(HOME, ".dsh")),
                "wired": False, "manual": True, "protocol": "Anthropic", "base": base,
                "file": "设置 → 模型 → 添加提供方 → 自定义模型 API"})
    return out


def client_wire(cid):
    keys = keys_list()
    if not keys:
        return {"ok": False, "error": "还没有任何 Key，先在 Key 管理里加一把"}
    key = keys[0]["key"]
    base = _gateway_base()

    if cid == "claude-code":
        os.makedirs(os.path.dirname(CLAUDE_JSON), exist_ok=True)
        old = read_text(CLAUDE_JSON) or "{}"
        with open(CLAUDE_JSON + GO_BAK, "w", encoding="utf-8") as f:
            f.write(old)
        try:
            d = json.loads(old)
        except Exception:  # noqa: BLE001
            d = {}
        env = d.get("env") or {}
        env.update({
            "ANTHROPIC_BASE_URL": base,
            "ANTHROPIC_AUTH_TOKEN": key,
            "ANTHROPIC_MODEL": "opus",
            "ANTHROPIC_SMALL_FAST_MODEL": "haiku",
        })
        np = [x for x in str(env.get("NO_PROXY", "localhost,127.0.0.1,::1")).split(",") if x]
        for extra in (base.split("//")[-1].split(":")[0],):
            if extra not in np:
                np.append(extra)
        env["NO_PROXY"] = ",".join(np)
        d["env"] = env
        with open(CLAUDE_JSON, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        back = json.loads(read_text(CLAUDE_JSON) or "{}")
        ok = (back.get("env") or {}).get("ANTHROPIC_BASE_URL") == base
        return {"ok": ok, "file": CLAUDE_JSON, "base": base,
                "note": "已写入并回读校验" if ok else "写入后回读不一致"}

    if cid == "codex":
        t = read_text(CODEX_TOML) or ""
        with open(CODEX_TOML + GO_BAK, "w", encoding="utf-8") as f:
            f.write(t)
        # 去掉旧的 gatewayone 段与同名顶行
        t = re.sub(r'\n?\[model_providers\.%s\][^\[]*' % PROVIDER_ID, "\n", t)
        t = re.sub(r'^model\s*=.*\n', "", t, flags=re.M)
        t = re.sub(r'^model_provider\s*=.*\n', "", t, flags=re.M)
        block = (
            f'\nmodel = "opus"\n'
            f'model_provider = "{PROVIDER_ID}"\n'
            f'\n[model_providers.{PROVIDER_ID}]\n'
            f'name = "Gateway One"\n'
            f'base_url = "{base}/v1"\n'
            f'env_key = "GATEWAYONE_API_KEY"\n'
            f'wire_api = "chat"\n'
        )
        with open(CODEX_TOML, "w", encoding="utf-8") as f:
            f.write(t.rstrip("\n") + "\n" + block)
        back = read_text(CODEX_TOML) or ""
        ok = f"[model_providers.{PROVIDER_ID}]" in back
        return {"ok": ok, "file": CODEX_TOML, "base": base + "/v1", "key": key,
                "note": "还要设一个环境变量才生效",
                "env_hint": f'export GATEWAYONE_API_KEY="{key}"'}

    if cid == "dsh":
        return {"ok": False, "manual": True, "base": base, "key": key,
                "steps": ["打开 dsh → 设置 → 模型 → 添加提供方",
                          "选「自定义模型 API」",
                          f"标识填 {PROVIDER_ID}",
                          "协议选 Anthropic Messages",
                          f"地址填 {base}",
                          f"Key 填 {key}",
                          "模型填 opus，勾选后保存"]}
    return {"ok": False, "error": "未知客户端"}


def client_unwire(cid):
    f = CLAUDE_JSON if cid == "claude-code" else CODEX_TOML if cid == "codex" else None
    if not f or not os.path.exists(f + GO_BAK):
        return {"ok": False, "error": "没有找到备份文件"}
    bak = read_text(f + GO_BAK) or ""
    with open(f, "w", encoding="utf-8") as fh:
        fh.write(bak)
    return {"ok": True, "file": f, "note": "已还原到接入前的状态"}


# ---------- 设置项 ----------
def settings_get():
    cfg = parse_config()
    t = _read_cfg_raw()
    dbg = _first(r"^\s+debug:\s*(true|false)", t, "false")
    return {
        "host": cfg["host"],
        "port": cfg["port"],
        "retry": cfg["retry"],
        "proxy_url": cfg["proxy_url"] or "",
        "log_debug": str(dbg).lower() == "true",
        "backup_enabled": os.access(os.path.join(GW, "backup.sh"), os.X_OK),
        "auth_dir": cfg["auth_dir"],
        "ui_port": UI_PORT,
        "notify_level": _notify_level(),
    }


def settings_set(patch):
    t = _read_cfg_raw()
    old = settings_get()
    changed = []
    if "notify_level" in patch:
        v = str(patch["notify_level"])
        if v not in ("all", "severe", "off"):
            return {"ok": False, "error": "notify_level 只能是 all / severe / off"}
        with open(os.path.join(GW, ".notify-level"), "w", encoding="utf-8") as f:
            f.write(v)
        os.chmod(os.path.join(GW, ".notify-level"), 0o600)
        changed.append("通知级别")
    if "host" in patch:
        v = str(patch["host"])
        if v not in ("127.0.0.1", "0.0.0.0"):
            return {"ok": False, "error": "host 只能是 127.0.0.1 或 0.0.0.0"}
        t = re.sub(r'^(\s+)host:\s*"[^"]*"', lambda m: f'{m.group(1)}host: "{v}"', t, count=1, flags=re.M)
        changed.append("访问范围")
    if "port" in patch:
        try:
            v = int(patch["port"])
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": "端口必须是数字"}
        if not (1024 <= v <= 65535):
            return {"ok": False, "error": "端口需在 1024-65535 之间"}
        t = re.sub(r"^(\s+)port:\s*\d+", lambda m: f"{m.group(1)}port: {v}", t, count=1, flags=re.M)
        changed.append("端口")
    if "log_debug" in patch:
        v = "true" if patch["log_debug"] else "false"
        t = re.sub(r"^(\s+)debug:\s*(?:true|false)", lambda m: f"{m.group(1)}debug: {v}", t, count=1, flags=re.M)
        changed.append("日志级别")
    if not changed:
        return {"ok": False, "error": "没有可改的项"}
    # 只有动了网关配置（host/port/debug）才需要写 config.yaml 与重启；通知级别只影响控制台
    need_restart = any(c in ("访问范围", "端口", "日志级别") for c in changed)
    if need_restart:
        _write_cfg_raw(t)
    _invalidate_cache()
    r = restart_gateway() if need_restart else {"ok": True, "skipped": True}
    return {"ok": r["ok"], "changed": changed, "restart": r, "before": old,
            "after": settings_get()}


# ============================================================================
# 第二批：一键体检
# ============================================================================
DOCTOR = {"running": False, "items": [], "started": 0, "finished": 0,
          "total": 0, "log": [], "verdict": ""}
DOC_LOCK = threading.Lock()
DOC_TS = 0.0


def fmt_dur(sec):
    if sec is None:
        return "—"
    neg = sec < 0
    sec = abs(sec)
    d, h, m = int(sec // 86400), int(sec % 86400 // 3600), int(sec % 3600 // 60)
    s = f"{d}天{h}小时" if d else (f"{h}小时{m}分" if h else f"{m}分")
    return ("已过期 " + s) if neg else s


def _doc_add(name, ok, detail, hint="", level=None):
    detail = str(detail or "")
    if len(detail) > 110:
        detail = detail[:110].rstrip() + "…"
    with DOC_LOCK:
        DOCTOR["items"].append({
            "name": name, "ok": bool(ok), "level": level or ("ok" if ok else "bad"),
            "detail": detail, "hint": hint,
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        })
        DOCTOR["log"].append(f"{'✓' if ok else '✗'} {name} — {detail}")
        DOCTOR["log"] = DOCTOR["log"][-40:]


def _doc_step(name):
    with DOC_LOCK:
        DOCTOR["log"].append(f"… {name}")


def _probe_once(model, prompt, max_tokens=64, timeout=150):
    """打一发，返回 (ok, ms, text_or_error, real_model)"""
    t0 = time.time()
    r = probe_model(model, prompt, max_tokens)
    ms = int((time.time() - t0) * 1000)
    if r.get("ok"):
        return True, ms, (r.get("text") or "")[:60], r.get("model") or ""
    return False, ms, (r.get("error") or "未知错误")[:200], ""


def _doctor_probe_model(pf: str) -> str:
    """选择体检实调用的模型名：优先用已配置别名，其次首个可用渠道模型，最后按平台兜底。"""
    try:
        al = (aliases_read() or {}).get(pf) or []
        names = [a.get("alias") or a.get("name") for a in al if a.get("alias") or a.get("name")]
        if names:
            if pf == "claude" and "haiku" in names:
                return "haiku"
            return names[0]
    except Exception:  # noqa: BLE001
        pass
    owner = (CHANNEL_META.get(pf) or {}).get("owner")
    if owner:
        try:
            fm = fetch_models() or {}
            owners = fm.get("owners") or {}
            for m in (fm.get("models") or []):
                if owners.get(m) == owner:
                    return m
        except Exception:  # noqa: BLE001
            pass
    return {"claude": "haiku", "xai": "grok", "codex": "codex"}.get(pf, pf)


def doctor_worker(mode="deep"):
    global DOC_TS
    try:
        cfg = parse_config()

        # 1 进程与端口
        _doc_step("检查进程与端口")
        pid = port_pid(cfg["port"])
        if pid:
            _doc_add("网关进程", True, f"PID {pid} 正在监听 {cfg['host']}:{cfg['port']}")
        else:
            la = launch_status(LABEL)
            _doc_add("网关进程", False, "端口上没有进程",
                     "在设置里重启网关，或 launchctl kickstart -k gui/$(id -u)/" + LABEL)

        # 2 launchd 托管
        la = launch_status(LABEL)
        wd = launch_status(LABEL_WD)
        if la["loaded"]:
            _doc_add("launchd 托管", True,
                     f"网关 {'运行中' if la['pid'] else '已注册未运行'} · watchdog {'已装载' if wd['loaded'] else '未装载'}")
        else:
            _doc_add("launchd 托管", False, "网关没有注册到 launchd（崩溃后不会自愈）",
                     "bash install.sh 重装，或手动 bootstrap plist")

        # 3 凭证
        _doc_step("检查凭证")
        auths = read_auth_files()
        if not auths:
            _doc_add("凭证", False, "没有任何凭证", "点「+ 添加账号」接入一个平台")
        for a in auths:
            label = {"claude": "Claude Max", "codex": "ChatGPT",
                     "xai": "xAI Grok"}.get(a["type"], a["type"])
            if a.get("disabled"):
                _doc_add(f"凭证 · {label}", False, f"{a.get('email','')} 已被禁用", "重新登录")
            elif a.get("remain_sec") is not None and a["remain_sec"] < 0:
                _doc_add(f"凭证 · {label}", False,
                         f"{a.get('email','')} 访问令牌已过期 {fmt_dur(-a['remain_sec'])}",
                         "有 refresh_token 会自动续；若一直失败就重新登录",
                         level="warn")
            else:
                left = "无期限" if a.get("remain_sec") is None else f"剩 {fmt_dur(a['remain_sec'])}"
                _doc_add(f"凭证 · {label}", True, f"{a.get('email','')} · {left}")

        # 4 出口代理
        if mode == "deep":
            _doc_step("检查出口网络")
            proxy = cfg.get("proxy_url") or ""
            rc, out, _ = sh(["curl", "-s", "-m", "15", "--noproxy", "*",
                             "http://api.ipify.org"], timeout=18)
            direct_ip = out.strip() if rc == 0 and out.strip() else ""
            _doc_add("出口网络（直连）", bool(direct_ip),
                     f"出口 IP {direct_ip}" if direct_ip else "直连取不到公网 IP",
                     "配置了代理时直连失败不影响；直连完全不通说明机器网络本身有问题",
                     level="ok" if direct_ip else "warn")
            if proxy:
                _doc_add("出口代理配置", True, proxy)

            # 5 三家实调用
            for label, pf in (("Claude Max", "claude"),
                              ("xAI Grok", "xai"),
                              ("ChatGPT", "codex")):
                model = _doctor_probe_model(pf)
                has = any(a["type"] == pf and not a.get("disabled") for a in auths)
                if not has:
                    _doc_add(f"实调用 · {label}", True, "未接入，跳过", level="skip")
                    continue
                _doc_step(f"实调用 {label}（{model}）")
                ok, ms, txt, real = _probe_once(model, "回一个字：好", 128)
                if ok:
                    _doc_add(f"实调用 · {label}", True, f"{ms}ms · {real} · 回复「{txt[:12]}」")
                else:
                    low = txt.lower()
                    quota = ("usage_limit_reached" in low or "model_cooldown" in low
                             or "rate_limit" in low or "额度" in txt)
                    if quota:
                        _doc_add(f"实调用 · {label}", True,
                                 f"{ms}ms · 账号额度用尽，等窗口重置或换号",
                                 "这不是网关故障；额度恢复后会自动可用",
                                 level="warn")
                    else:
                        _doc_add(f"实调用 · {label}", False, f"{ms}ms · {txt}",
                                 "先看 logs/error-v1-*.log，再考虑重新登录", level="bad")

            # 6 本地磁盘与日志
        _doc_step("检查磁盘与日志")
        rc, out, _ = sh(["df", "-k", GW], timeout=6)
        free_gb = None
        try:
            free_gb = round(int(out.split("\n")[1].split()[3]) / 1048576, 1)
        except Exception:  # noqa: BLE001
            pass
        if free_gb is None:
            _doc_add("磁盘余量", True, "读不到", level="skip")
        else:
            _doc_add("磁盘余量", free_gb > 2, f"还有 {free_gb} GB",
                     "日志与备份会占空间，低于 2GB 要清理" if free_gb <= 2 else "")

        log_sz = 0
        for f in glob.glob(os.path.join(LOGS, "*.log")):
            try:
                log_sz += os.path.getsize(f)
            except Exception:  # noqa: BLE001
                pass
        _doc_add("日志体积", log_sz < 200 * 1048576,
                 f"{round(log_sz / 1048576, 1)} MB",
                 "超过 200MB 建议在设置里关掉日志调试或手工清空" if log_sz >= 200 * 1048576 else "")

        # 7 备份
        bks = sorted(glob.glob(os.path.join(os.path.expanduser("~"),
                                            "claude-gateway-backups", "auth-*.enc")))
        if bks:
            age_h = round((time.time() - os.path.getmtime(bks[-1])) / 3600, 1)
            _doc_add("加密备份", age_h < 48,
                     f"最近一份 {os.path.basename(bks[-1])} · {age_h} 小时前",
                     "超过两天没备份，检查 watchdog 是否在跑" if age_h >= 48 else "",
                     level="ok" if age_h < 48 else "warn")
        else:
            _doc_add("加密备份", False, "没有找到任何备份", "运行 bash ~/claude-gateway/backup.sh")

        with DOC_LOCK:
            bad = [x for x in DOCTOR["items"] if x["level"] == "bad"]
            warn = [x for x in DOCTOR["items"] if x["level"] == "warn"]
            DOCTOR["verdict"] = (
                "全部正常" if not bad and not warn else
                f"{len(bad)} 个问题、{len(warn)} 个提醒" if bad or warn else "全部正常")
        DOC_TS = time.time()
    except Exception as e:  # noqa: BLE001
        _doc_add("体检异常", False, f"{type(e).__name__}: {e}")
    finally:
        with DOC_LOCK:
            DOCTOR["running"] = False
            DOCTOR["finished"] = time.time()


def doctor_start(mode="deep"):
    with DOC_LOCK:
        if DOCTOR["running"]:
            return {"ok": False, "error": "体检正在进行中"}
        DOCTOR.update({"running": True, "items": [], "log": [], "verdict": "",
                       "started": time.time(), "finished": 0})
    threading.Thread(target=doctor_worker, args=(mode,), daemon=True).start()
    return {"ok": True, "mode": mode}


def doctor_status():
    with DOC_LOCK:
        d = dict(DOCTOR)
        d["items"] = list(DOCTOR["items"])
        d["log"] = list(DOCTOR["log"])
    if d["started"]:
        d["elapsed"] = round((d["finished"] or time.time()) - d["started"], 1)
    d["ts"] = DOC_TS
    return d


def doctor_text():
    if not DOCTOR.get("items"):
        return ("Gateway One 体检 —— 还没跑过。\n\n"
                "在面板顶栏点「体检」，或用：curl -X POST http://127.0.0.1:8899/api/doctor/start\n"
                "约 10~60 秒后回来看结果。")
    """把结果拼成可复制的一段文本"""
    d = doctor_status()
    lines = [f"Gateway One 体检 — {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}",
             f"结论：{d.get('verdict') or '未完成'}（{d.get('elapsed', 0)}s）", ""]
    for it in d["items"]:
        mark = {"ok": "✓", "warn": "!", "bad": "✗", "skip": "–"}.get(it["level"], "?")
        lines.append(f"[{mark}] {it['name']}: {it['detail']}")
        if it.get("hint"):
            lines.append(f"      → {it['hint']}")
    return "\n".join(lines)


# ============================================================================
# 第二批：面板加锁（跨设备访问前的硬前提）
# ============================================================================
PWFILE = os.path.join(GW, ".ui-password")
SESSIONS = {}
SESS_TTL = 7 * 86400
AUTH_OPEN_PATHS = ("/", "/index.html", "/favicon.ico", "/api/auth/login", "/api/auth/state", "/api/auth/off")
FAVICON_SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="8" fill="#141d2b"/><path d="M16 7l9 9-9 9-9-9z" fill="none" stroke="#60a5fa" stroke-width="2.4"/></svg>'


# ───────── 系统通知 ─────────
NOTIFY_SEEN = set()
NOTIFY_LAST = [0.0]
NOTIFY_MIN_GAP = 600          # 两条通知之间至少 10 分钟，避免打扰
NOTIFY_SCAN_SEC = 300         # 后台每 5 分钟扫一次


def do_notify(title, body, force=False):
    """走 macOS 通知中心。force=True 时跳过节流（手动测试用）。"""
    import json as _json
    now = time.time()
    if not force and now - NOTIFY_LAST[0] < NOTIFY_MIN_GAP:
        return False
    parts = ['display notification %s with title %s' % (_json.dumps(body, ensure_ascii=False),
                                                        _json.dumps(title, ensure_ascii=False))]
    if sys.platform == "darwin":
        parts.append('sound name "Glass"')
    sh(["osascript", "-e", " ".join(parts)], 8)
    NOTIFY_LAST[0] = now
    return True


# 额度类异常：属于账号配额，不是网关故障。默认不弹通知。
QUOTA_KINDS = ("额度用尽", "模型冷却", "限流", "usage_limit", "cooldown", "rate_limit", "429")
NOTIFY_SEEN_AT = {}   # key → 上次提醒的时间戳（同一件事 12 小时内只打扰一次）
NOTIFY_REPEAT_SEC = 12 * 3600


def _notify_level():
    """all | severe | off —— 存在 config 的 note 里太绕，单独存个小文件。"""
    try:
        with open(os.path.join(GW, ".notify-level"), encoding="utf-8") as f:
            v = f.read().strip()
        return v if v in ("all", "severe", "off") else "severe"
    except Exception:
        return "severe"


def _is_quota_issue(i):
    blob = "%s %s %s" % (i.get("kind") or "", i.get("model") or "", i.get("detail") or i.get("note") or "")
    return any(k in blob for k in QUOTA_KINDS)


def _notify_scan():
    """扫一遍 issues。同一件事 12 小时内只弹一次；额度类默认不弹。"""
    try:
        lvl = _notify_level()
        if lvl == "off":
            return
        ov = overview()
        now = time.time()
        for i in (ov.get("issues") or []):
            quota = _is_quota_issue(i)
            if lvl == "severe" and quota:
                continue          # 额度用尽不是故障，不打扰
            # 关键修：key 不再带 reset_sec（它每次都变，导致同一件事反复弹）
            key = "%s|%s" % (i.get("kind"), i.get("model") or i.get("provider") or "")
            last = NOTIFY_SEEN_AT.get(key, 0)
            if now - last < NOTIFY_REPEAT_SEC:
                continue
            NOTIFY_SEEN_AT[key] = now
            kind = i.get("kind") or "异常"
            who = i.get("model") or i.get("provider") or ""
            body = ("%s %s" % (kind, who)).strip()
            if i.get("reset_sec"):
                body += " · 重置 " + fmt_dur(i["reset_sec"])
            do_notify("Gateway One", body)
    except Exception:
        pass


def _notify_loop():
    time.sleep(20)
    while True:
        _notify_scan()
        time.sleep(NOTIFY_SCAN_SEC)




# ============================================================================
# 平台详情页（点订阅卡进入）
# ============================================================================
CHANNEL_META = {
    "claude": {"label": "Claude Max", "owner": "anthropic", "color": "#d97757",
               "cfgkey": "claude", "sect": "upstream",
               "desc": "Anthropic 官方订阅（Claude Max / Pro），走 OAuth + Claude Code 指纹"},
    "codex": {"label": "ChatGPT", "owner": "openai", "color": "#10a37f",
              "cfgkey": "codex", "sect": "upstream",
              "desc": "OpenAI ChatGPT / Codex 订阅，走 OAuth（wham 接口可查额度）"},
    "xai": {"label": "xAI Grok", "owner": "xai", "color": "#a78bfa",
            "cfgkey": "xai", "sect": "upstream",
            "desc": "xAI SuperGrok 订阅，设备码授权；官方未开放额度接口"},
    "antigravity": {"label": "Gemini", "owner": "antigravity", "color": "#4285f4",
                    "cfgkey": "gemini", "sect": "upstream",
                    "desc": "Google Antigravity（Gemini Code Assist 额度），Google 账号 OAuth"},
}


def _section_text(sect, key=None):
    """取 config.yaml 里 sect 段；给了 key 就只取它下面的子段（缩进两空格起）。"""
    t = _read_cfg_raw()
    lines = t.splitlines()
    out, on = [], False
    for ln in lines:
        if ln == sect + ":":
            on = True
            continue                      # 不要父行，只取子内容
        if on:
            if ln and not ln[0].isspace() and not ln.startswith("#"):
                break
            out.append(ln)
    if not key:
        return "\n".join(out).strip()
    # 在段内找 "  <key>:" 这一行开始，取到下一个同级 key 为止
    res, take = [], False
    for ln in out:
        if ln.startswith("  " + key + ":"):
            take = True
            res.append(ln)
            continue
        if take:
            if ln.startswith("  ") and not ln.startswith("   ") and not ln.strip().startswith("#") and ln.strip():
                break
            res.append(ln)
    return "\n".join(res).strip()


def channel_detail(ch):
    meta = CHANNEL_META.get(ch)
    if not meta:
        return {"ok": False, "error": "未知平台"}

    fm = fetch_models() or {}
    owners = fm.get("owners") or {}
    allm = fm.get("models") or []
    mine = [m for m in allm if owners.get(m) == meta["owner"]]

    al = aliases_read() or {}
    my_alias = al.get(ch) or []
    alias_of = {a["name"]: a["alias"] for a in my_alias}
    alias_names = {a["alias"] for a in my_alias}

    # 凭证
    creds = []
    for a in (read_auth_files() or []):
        if a.get("type") == ch:
            creds.append(a)

    # 额度
    qc = quota_cached() or {}
    q = qc.get(meta["cfgkey"]) or qc.get(ch) or {}

    # 异常（只该平台的）
    pref = {"claude": ("claude", "anthropic"), "codex": ("gpt", "codex", "openai"),
            "xai": ("grok", "xai"), "antigravity": ("gemini", "antigravity")}[ch]
    issues = [i for i in (scan_errors() or [])
              if any((i.get("model") or i.get("provider") or "").startswith(p) for p in pref)]

    models = []
    seen_pairs = set()
    for m in mine:
        al_name = alias_of.get(m, "")
        models.append({"id": m, "alias": al_name, "is_alias": False})
        seen_pairs.add((m, al_name))
    for a in my_alias:
        pair = (a.get("name") or "", a.get("alias") or "")
        if pair in seen_pairs or (pair[0] in mine and not pair[1]):
            continue
        models.append({"id": a.get("name"), "alias": a.get("alias"),
                       "display": a.get("display_name") or a.get("display") or "", "is_alias": True})

    cfg = parse_config()
    is_lan = cfg["host"] in ("0.0.0.0", "")
    eff_host = (lan_ip() or "127.0.0.1") if is_lan else "127.0.0.1"
    base_lan = "http://%s:%s" % (eff_host, cfg["port"])
    return {
        "ok": True, "id": ch,
        "label": meta["label"], "color": meta["color"], "desc": meta["desc"],
        "creds": creds,
        "local_cred": local_codex_status() if ch == "codex" else None,
        "models": models,
        "model_count": len(mine),
        "alias_count": len(my_alias),
        "aliases": my_alias,
        "quota": q,
        "issues": issues[:6],
        "config": {
            "upstream": _section_text("upstream", meta["cfgkey"]),
            "alias": _section_text("oauth", "model-alias"),
        },
        "api": {
            "base_anthropic": base_lan,
            "base_openai": base_lan + "/v1",
            "example_model": (my_alias[0]["alias"] if my_alias else (mine[0] if mine else "")),
            "models": [m for m in mine][:80],
        },
        "usage": window_usage(),
    }


def _auth_file():
    return read_text(PWFILE) or ""


def auth_enabled():
    return bool(_auth_file().strip())


def _hash_pw(pw, salt=None):
    import binascii
    import hashlib
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, 200000)
    return binascii.hexlify(salt).decode() + "$" + binascii.hexlify(dk).decode()


def _check_pw(pw):
    raw = _auth_file().strip()
    if "$" not in raw:
        return False
    salt_hex = raw.split("$", 1)[0]
    import binascii
    try:
        salt = binascii.unhexlify(salt_hex)
    except Exception:  # noqa: BLE001
        return False
    return _hash_pw(pw, salt) == raw


def auth_set(pw):
    pw = (pw or "").strip()
    if len(pw) < 4:
        return {"ok": False, "error": "密码至少 4 位"}
    tmp = PWFILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(_hash_pw(pw))
    os.chmod(tmp, 0o600)
    os.replace(tmp, PWFILE)
    SESSIONS.clear()
    return {"ok": True, "enabled": True}


def auth_off(pw):
    if not _check_pw(pw or ""):
        return {"ok": False, "error": "当前密码不对"}
    try:
        os.unlink(PWFILE)
    except Exception:  # noqa: BLE001
        pass
    SESSIONS.clear()
    return {"ok": True, "enabled": False}


def auth_state(h=None):
    """need_login 反映「这次请求是否已带有效 session」，不是「是否启用密码」。"""
    enabled = auth_enabled()
    logged = h is not None and _sess_ok(h.headers.get("Cookie"))
    return {"enabled": enabled, "need_login": enabled and not logged}


def _sess_new():
    tok = os.urandom(24).hex()
    SESSIONS[tok] = time.time() + SESS_TTL
    return tok


def _sess_ok(cookie):
    if not cookie:
        return False
    m = re.search(r"go_sess=([0-9a-f]{16,})", cookie)
    if not m:
        return False
    exp = SESSIONS.get(m.group(1))
    if not exp or exp < time.time():
        SESSIONS.pop(m.group(1), None)
        return False
    return True


def auth_login(pw):
    if not auth_enabled():
        return {"ok": True, "token": "", "note": "未启用密码"}
    # 简单防爆破：同一 IP 连续失败 5 次锁 60 秒
    now = time.time()
    fail = LOGIN_FAIL.get("n", 0)
    if fail >= 5 and now - LOGIN_FAIL.get("t", 0) < 60:
        return {"ok": False, "error": "失败次数过多，60 秒后再试"}
    if not _check_pw(pw or ""):
        LOGIN_FAIL["n"] = fail + 1 if now - LOGIN_FAIL.get("t", 0) < 300 else 1
        LOGIN_FAIL["t"] = now
        time.sleep(0.35)
        return {"ok": False, "error": "密码不对"}
    LOGIN_FAIL["n"] = 0
    return {"ok": True, "token": _sess_new()}


LOGIN_FAIL = {}


def main():
    srv = ThreadingHTTPServer((os.environ.get("CG_BIND", "127.0.0.1"), UI_PORT), Handler)
    srv.daemon_threads = True
    print(f"Gateway One Console → http://127.0.0.1:{UI_PORT}/", flush=True)
    print(f"  网关目录 {GW}", flush=True)
    threading.Thread(target=_notify_loop, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
