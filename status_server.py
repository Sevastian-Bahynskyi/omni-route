#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HOST = "127.0.0.1"
DEFAULT_PORT = 8787
HOME = Path.home()
CONFIG_PATH = HOME / ".omnigent" / "codex-account-pool.json"
STATE_PATH = HOME / ".omnigent" / "codex-account-pool-state.json"
PATCHED_BASE = HOME / ".local" / "share" / "omnigent-subscription-rotation"
PATCHED_LAUNCHER = HOME / ".local" / "bin" / "omni-rotate"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _command_ok(command: list[str], timeout: float = 2.0) -> bool | None:
    if shutil.which(command[0]) is None:
        return None
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.returncode == 0


def _desktop_installed() -> bool:
    candidates = list(Path("/Applications").glob("Omnigent*.app"))
    candidates.extend((HOME / "Applications").glob("Omnigent*.app"))
    return any(path.is_dir() for path in candidates)


def collect_status() -> dict[str, Any]:
    config = _read_json(CONFIG_PATH)
    state = _read_json(STATE_PATH)
    now = int(time.time())

    accounts_raw = config.get("accounts")
    accounts_raw = accounts_raw if isinstance(accounts_raw, list) else []
    cooldowns = state.get("cooldowns")
    cooldowns = cooldowns if isinstance(cooldowns, dict) else {}
    bindings = state.get("session_bindings")
    bindings = bindings if isinstance(bindings, dict) else {}
    current = state.get("current_account") if isinstance(state.get("current_account"), str) else None

    accounts: list[dict[str, Any]] = []
    for index, item in enumerate(accounts_raw, start=1):
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        auth_json = item.get("auth_json")
        if not isinstance(name, str) or not name:
            continue
        auth_path = Path(auth_json).expanduser() if isinstance(auth_json, str) and auth_json else None
        auth_present = bool(auth_path and auth_path.is_file() and auth_path.stat().st_size > 0)
        cooldown = cooldowns.get(name)
        cooldown = cooldown if isinstance(cooldown, dict) else {}
        retry_at = cooldown.get("retry_at")
        retry_at = int(retry_at) if isinstance(retry_at, (int, float)) else None
        cooling_down = retry_at is not None and retry_at > now
        sessions = sum(1 for value in bindings.values() if value == name)

        if not auth_present:
            status = "missing_auth"
        elif cooling_down:
            status = "cooldown"
        elif current == name:
            status = "active"
        else:
            status = "ready"

        accounts.append(
            {
                "index": index,
                "name": name,
                "status": status,
                "authPresent": auth_present,
                "current": current == name,
                "sessions": sessions,
                "retryAt": retry_at,
                "cooldownReason": cooldown.get("reason") if isinstance(cooldown.get("reason"), str) else None,
            }
        )

    claude_agent = config.get("claude_fallback_agent")
    claude_configured = isinstance(claude_agent, str) and bool(claude_agent.strip())
    claude_cli = shutil.which("claude") is not None
    claude_auth = _command_ok(["claude", "auth", "status"]) if claude_configured and claude_cli else None

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "router": {
            "configured": bool(accounts),
            "enabled": bool(config.get("enabled", True)) and bool(accounts),
            "threshold": config.get("rotate_at_percent", 99),
            "currentAccount": current,
            "accountCount": len(accounts),
            "activeBindings": len(bindings),
        },
        "accounts": accounts,
        "claude": {
            "configured": claude_configured,
            "agent": claude_agent if claude_configured else None,
            "cliInstalled": claude_cli,
            "authenticated": claude_auth,
        },
        "install": {
            "patchedRuntime": PATCHED_BASE.is_dir(),
            "patchedLauncher": PATCHED_LAUNCHER.is_file(),
            "normalOmniCli": shutil.which("omni") is not None,
            "desktopApp": _desktop_installed(),
        },
    }


HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OMNI ROUTE // STATUS</title>
<style>
:root{--bg:#020604;--panel:rgba(3,16,9,.82);--green:#35ff7a;--green2:#0acb58;--muted:#668b70;--line:rgba(53,255,122,.18);--bad:#ff5d68;--warn:#ffd166}
*{box-sizing:border-box}html,body{margin:0;min-height:100%;background:var(--bg);color:var(--green);font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,"Liberation Mono",monospace}
body:before{content:"";position:fixed;inset:0;pointer-events:none;z-index:9;background:repeating-linear-gradient(0deg,rgba(255,255,255,.018) 0,rgba(255,255,255,.018) 1px,transparent 1px,transparent 4px);mix-blend-mode:screen}
canvas{position:fixed;inset:0;width:100%;height:100%;opacity:.12;z-index:0}
.wrap{position:relative;z-index:1;max-width:980px;margin:0 auto;padding:38px 20px 60px}.top{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;border-bottom:1px solid var(--line);padding-bottom:20px}.brand{font-size:22px;font-weight:700;letter-spacing:.08em;text-shadow:0 0 12px rgba(53,255,122,.4)}.sub{color:var(--muted);font-size:12px;margin-top:7px}.ro{border:1px solid var(--green2);padding:7px 10px;font-size:11px;letter-spacing:.12em;background:rgba(10,203,88,.08);box-shadow:0 0 16px rgba(53,255,122,.08)}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:18px 0}.stat,.panel{border:1px solid var(--line);background:var(--panel);box-shadow:inset 0 0 30px rgba(53,255,122,.025)}.stat{padding:13px}.label{font-size:10px;letter-spacing:.12em;color:var(--muted);text-transform:uppercase}.value{margin-top:7px;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.ok{color:var(--green)}.bad{color:var(--bad)}.warn{color:var(--warn)}
.panel{margin-top:12px;padding:16px}.panel-title{font-size:11px;letter-spacing:.14em;color:var(--muted);margin-bottom:14px}.route{display:flex;align-items:center;flex-wrap:wrap;gap:8px;font-size:12px}.node{border:1px solid var(--line);padding:8px 10px}.node.active{border-color:var(--green);box-shadow:0 0 14px rgba(53,255,122,.12)}.arrow{color:#237f44}
.accounts{display:grid;gap:8px}.account{display:grid;grid-template-columns:52px 1.5fr 1fr 1fr;gap:10px;align-items:center;padding:11px 0;border-top:1px solid rgba(53,255,122,.09)}.account:first-child{border-top:0}.idx{color:#275c37}.name{font-weight:700}.meta{font-size:11px;color:var(--muted)}.status{font-size:11px;text-transform:uppercase;letter-spacing:.08em}.dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:7px;background:currentColor;box-shadow:0 0 9px currentColor}
.install-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}.install-item{font-size:11px;padding:10px;border:1px solid rgba(53,255,122,.1)}.foot{margin-top:16px;display:flex;justify-content:space-between;gap:12px;color:#3d6f4c;font-size:10px}.blink{animation:blink 1.1s steps(1) infinite}@keyframes blink{50%{opacity:.15}}
@media(max-width:760px){.stats,.install-grid{grid-template-columns:1fr 1fr}.account{grid-template-columns:42px 1fr}.account>div:nth-child(3),.account>div:nth-child(4){grid-column:2}.top{align-items:center}.brand{font-size:18px}}
</style>
</head>
<body>
<canvas id="matrix"></canvas>
<div class="wrap">
  <div class="top"><div><div class="brand">OMNI ROUTE // STATUS<span class="blink">_</span></div><div class="sub">LOCAL SUBSCRIPTION ROUTER TELEMETRY</div></div><div class="ro">READ ONLY // 127.0.0.1</div></div>
  <div class="stats">
    <div class="stat"><div class="label">Router</div><div class="value" id="router">...</div></div>
    <div class="stat"><div class="label">Active</div><div class="value" id="active">...</div></div>
    <div class="stat"><div class="label">Threshold</div><div class="value" id="threshold">...</div></div>
    <div class="stat"><div class="label">Claude fallback</div><div class="value" id="claude">...</div></div>
  </div>
  <div class="panel"><div class="panel-title">ROUTE CHAIN</div><div class="route" id="route"></div></div>
  <div class="panel"><div class="panel-title">CODEX ACCOUNTS</div><div class="accounts" id="accounts"></div></div>
  <div class="panel"><div class="panel-title">INSTALLATION</div><div class="install-grid" id="install"></div></div>
  <div class="foot"><span>AUTO REFRESH // 2s</span><span id="sync">waiting for telemetry...</span></div>
</div>
<script>
const $=id=>document.getElementById(id); const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function bool(v){return v===true?'<span class="ok">ONLINE</span>':v===false?'<span class="bad">OFFLINE</span>':'<span class="warn">UNKNOWN</span>'}
function until(ts){if(!ts)return '—';const d=ts*1000-Date.now();if(d<=0)return 'ready';const m=Math.ceil(d/60000);if(m<60)return m+'m';const h=Math.floor(m/60),mm=m%60;return h+'h '+mm+'m'}
function statusClass(s){return s==='active'||s==='ready'?'ok':s==='cooldown'?'warn':'bad'}
async function refresh(){try{const r=await fetch('/api/status',{cache:'no-store'});const d=await r.json();$('router').innerHTML=d.router.enabled?'<span class="ok">ENABLED</span>':'<span class="bad">DISABLED</span>';$('active').textContent=d.router.currentAccount||'none';$('threshold').textContent=d.router.threshold+'%';let c='NOT CONFIGURED';if(d.claude.configured)c=d.claude.authenticated===true?'CONNECTED':d.claude.authenticated===false?'AUTH NEEDED':'CONFIGURED';$('claude').innerHTML='<span class="'+(d.claude.authenticated===true?'ok':d.claude.configured?'warn':'bad')+'">'+esc(c)+'</span>';
$('route').innerHTML=d.accounts.map(a=>'<span class="node '+(a.current?'active':'')+'">'+esc(a.name)+'</span>').join('<span class="arrow">→</span>')+(d.claude.configured?'<span class="arrow">→</span><span class="node">claude</span>':'');
$('accounts').innerHTML=d.accounts.length?d.accounts.map(a=>'<div class="account"><div class="idx">['+String(a.index).padStart(2,'0')+']</div><div><div class="name">'+esc(a.name)+'</div><div class="meta">auth '+(a.authPresent?'linked':'missing')+' // sessions '+a.sessions+'</div></div><div class="status '+statusClass(a.status)+'"><span class="dot"></span>'+esc(a.status.replace('_',' '))+'</div><div class="meta">'+(a.status==='cooldown'?'reset '+until(a.retryAt):a.cooldownReason?esc(a.cooldownReason):'—')+'</div></div>').join(''):'<div class="meta">No Codex accounts configured.</div>';
const installs=[['patched runtime',d.install.patchedRuntime],['patched launcher',d.install.patchedLauncher],['normal omni',d.install.normalOmniCli],['desktop app',d.install.desktopApp]];$('install').innerHTML=installs.map(x=>'<div class="install-item"><div class="label">'+esc(x[0])+'</div><div style="margin-top:7px">'+bool(x[1])+'</div></div>').join('');$('sync').textContent='sync '+new Date().toLocaleTimeString();}catch(e){$('sync').textContent='telemetry error';}}
refresh();setInterval(refresh,2000);
const cv=$('matrix'),ctx=cv.getContext('2d');let drops=[];function size(){cv.width=innerWidth;cv.height=innerHeight;drops=Array(Math.ceil(cv.width/18)).fill(1)}size();addEventListener('resize',size);function rain(){ctx.fillStyle='rgba(2,6,4,.09)';ctx.fillRect(0,0,cv.width,cv.height);ctx.fillStyle='#35ff7a';ctx.font='13px monospace';drops.forEach((y,i)=>{const t=String.fromCharCode(0x30A0+Math.random()*96);ctx.fillText(t,i*18,y*18);if(y*18>cv.height&&Math.random()>.975)drops[i]=0;drops[i]++});requestAnimationFrame(rain)}rain();
</script>
</body></html>'''


class StatusHandler(BaseHTTPRequestHandler):
    server_version = "OmniRouteStatus/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _headers(self, content_type: str, length: int, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'none'; frame-ancestors 'none'")
        self.end_headers()

    def do_GET(self) -> None:
        if self.path == "/" or self.path == "/index.html":
            body = HTML.encode("utf-8")
            self._headers("text/html; charset=utf-8", len(body))
            self.wfile.write(body)
            return
        if self.path == "/api/status":
            body = json.dumps(collect_status(), separators=(",", ":")).encode("utf-8")
            self._headers("application/json; charset=utf-8", len(body))
            self.wfile.write(body)
            return
        body = b"not found\n"
        self._headers("text/plain; charset=utf-8", len(body), 404)
        self.wfile.write(body)

    def do_HEAD(self) -> None:
        if self.path in {"/", "/index.html"}:
            self._headers("text/html; charset=utf-8", len(HTML.encode("utf-8")))
        elif self.path == "/api/status":
            self._headers("application/json; charset=utf-8", 0)
        else:
            self._headers("text/plain; charset=utf-8", 0, 404)

    def _readonly(self) -> None:
        body = b"read only\n"
        self._headers("text/plain; charset=utf-8", len(body), 405)
        self.wfile.write(body)

    do_POST = _readonly
    do_PUT = _readonly
    do_PATCH = _readonly
    do_DELETE = _readonly


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Omni Route localhost status dashboard")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-open", action="store_true", help="Do not open the browser automatically")
    args = parser.parse_args()
    if not (1 <= args.port <= 65535):
        parser.error("--port must be between 1 and 65535")

    server = ThreadingHTTPServer((HOST, args.port), StatusHandler)
    url = f"http://{HOST}:{args.port}/"
    print(f"Omni Route status: {url}")
    print("Read-only. Bound to localhost only. Ctrl+C to stop.")
    if not args.no_open:
        threading.Timer(0.25, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
