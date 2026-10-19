"""内置网页控制台：http.server + SSE，无第三方依赖。

    python -m audiolink ui            # 打开 http://127.0.0.1:8770
    python -m audiolink ui --host 0.0.0.0   # 允许手机/平板当遥控器

接口：
    GET  /api/devices      设备列表
    GET  /api/status       一次性状态
    GET  /api/events       SSE 推送（每 400ms）
    POST /api/start        启动 {role, device, source, file, peers, latency, volume}
    POST /api/stop         停止
    POST /api/discover     广播发现接收端
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Deque, Dict, Optional
from urllib.parse import urlparse

from . import __version__, protocol as P
from .devices import DeviceInfo, default_device, find_device, list_devices

STATIC_DIR = Path(__file__).with_name("static")


# ------------------------------------------------------------------ 应用状态
class AppState:
    def __init__(self) -> None:
        self.role: str = "recv"
        self.engine: Any = None
        self.lock = threading.Lock()
        self.logs: Deque[Dict[str, Any]] = deque(maxlen=200)
        self.last_error = ""
        self.started_at = 0.0
        self.config: Dict[str, Any] = {}

    def log(self, msg: str, level: str = "info") -> None:
        self.logs.append({"t": time.strftime("%H:%M:%S"), "m": msg, "l": level})
        print(f"[{time.strftime('%H:%M:%S')}] {msg}")

    # ------------------------------------------------------------ 启停
    def start(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        from .engine import CaptureSource, FileSource, Receiver, Sender, parse_peer

        self.stop(silent=True)
        role = cfg.get("role") or self.role
        self.role = role
        volume = float(cfg.get("volume", 1.0))
        latency = float(cfg.get("latency", 120.0))
        name = cfg.get("name") or socket.gethostname()

        if role == "send":
            dev_q = cfg.get("device")
            peers_raw = cfg.get("peers") or []
            if isinstance(peers_raw, str):
                peers_raw = [p for p in peers_raw.replace(";", ",").split(",") if p.strip()]
            if not peers_raw:
                raise RuntimeError("请先选择或填写接收端 IP")
            peers = [parse_peer(p.strip(), P.PORT_DATA) for p in peers_raw]

            if cfg.get("source") == "file" or cfg.get("file"):
                path = (cfg.get("file") or "").strip().strip('"')
                if not path or not os.path.exists(path):
                    raise RuntimeError(f"文件不存在：{path}")
                src = FileSource(path, loop=bool(cfg.get("loop", True)))
            else:
                if dev_q and dev_q not in ("auto", "system"):
                    dev = find_device(dev_q, "input")
                elif cfg.get("source") == "mic":
                    dev = default_device("input")
                else:
                    dev = self._auto_source_device()
                src = CaptureSource(dev)
            eng = Sender(src, peers, name=name, volume=volume)
            eng.start()
            self.engine = eng
            self.config = cfg
            self.started_at = time.time()
            self.log(f"开始发送：{src.label} -> {', '.join(f'{h}:{p}' for h, p in peers)}")
        else:
            dev_q = cfg.get("device")
            dev = find_device(dev_q, "output") if dev_q and dev_q != "auto" else default_device("output")
            eng = Receiver(dev, port=int(cfg.get("port", P.PORT_DATA)),
                           latency_ms=latency, volume=volume, name=name)
            eng.start()
            self.engine = eng
            self.config = cfg
            self.started_at = time.time()
            self.log(f"开始接收：渲染到 {dev.label}，缓冲 {latency:.0f}ms")
        return {"ok": True}

    def _auto_source_device(self) -> DeviceInfo:
        loops = [d for d in list_devices() if d.kind == "input" and d.is_loopback]
        if loops:
            try:
                out = default_device("output")
                for d in loops:
                    if d.name == out.name:
                        return d
            except LookupError:
                pass
            return loops[0]
        return default_device("input")

    def stop(self, silent: bool = False) -> Dict[str, Any]:
        with self.lock:
            if self.engine is not None:
                try:
                    self.engine.stop()
                except Exception as e:  # noqa: BLE001
                    self.log(f"停止时出错：{e}", "warn")
                self.engine = None
                if not silent:
                    self.log("已停止")
        return {"ok": True}

    # ------------------------------------------------------------ 快照
    def status(self) -> Dict[str, Any]:
        eng = self.engine
        out: Dict[str, Any] = {
            "version": __version__,
            "role": self.role,
            "running": bool(eng and getattr(eng.stats, "running", False)),
            "uptime": round(time.time() - self.started_at, 1) if eng else 0,
            "error": self.last_error,
            "config": self.config,
            "logs": list(self.logs)[-60:],
        }
        if eng is None:
            return out
        s = eng.stats
        if self.role == "send":
            out["send"] = {
                "packets": s.packets,
                "kbps": round(s.bytes_sent * 8 / 1000.0 / max(0.5, time.time() - self.started_at), 1),
                "rms": round(s.rms, 4),
                "source": s.source,
                "rate": s.rate,
                "peers": [
                    {"addr": k, **{kk: v.get(kk) for kk in
                                   ("name", "device", "pkts", "lost", "buffered_ms",
                                    "target_ms", "jitter_ms", "underruns")}}
                    for k, v in s.peers.items()
                ],
            }
        else:
            out["recv"] = {
                "packets": s.packets,
                "lost": s.lost,
                "late": s.late,
                "buffered_ms": round(s.buffered_ms, 1),
                "target_ms": round(s.target_ms, 1),
                "jitter_ms": round(s.jitter_ms, 2),
                "underruns": s.underruns,
                "rms": round(s.rms, 4),
                "sender": s.sender,
                "sender_addr": s.sender_addr,
                "device": s.device,
                "source_rate": s.source_rate,
                "out_rate": s.out_rate,
                "out_channels": s.out_channels,
                "waiting": s.waiting,
                "bytes": s.bytes_recv,
            }
        if getattr(s, "error", ""):
            out["error"] = s.error
        return out


STATE = AppState()


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = f"AudioLink/{__version__}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # 静音默认日志
        pass

    # -------------------------------------------------------------- 响应
    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _body(self) -> Dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # -------------------------------------------------------------- 路由
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            f = STATIC_DIR / "index.html"
            if f.exists():
                self._send(200, f.read_bytes(), "text/html; charset=utf-8")
            else:
                self._send(500, b"static/index.html missing", "text/plain; charset=utf-8")
            return
        if path == "/api/devices":
            try:
                devs = list_devices()
            except Exception as e:  # noqa: BLE001
                self._json({
                    "inputs": [],
                    "outputs": [],
                    "host": socket.gethostname(),
                    "error": (
                        f"无法枚举音频设备：{type(e).__name__}: {e}。"
                        "若提示缺少 pyaudiowpatch，请重新运行 setup.bat 或 panel.bat 以安装依赖。"
                    ),
                })
                return
            self._json({
                "inputs": [d.to_dict() for d in devs if d.kind == "input"],
                "outputs": [d.to_dict() for d in devs if d.kind == "output"],
                "host": socket.gethostname(),
            })
            return
        if path == "/api/status":
            self._json(STATE.status())
            return
        if path == "/api/events":
            self._sse()
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        body = self._body()
        try:
            if path == "/api/start":
                self._json(STATE.start(body))
            elif path == "/api/stop":
                self._json(STATE.stop())
            elif path == "/api/discover":
                from .engine import discover

                found = discover(timeout=float(body.get("timeout", 1.6)))
                if found:
                    STATE.log(f"发现 {len(found)} 个接收端：" +
                              "、".join(f"{f.get('name')}@{f['addr']}" for f in found))
                else:
                    STATE.log("未发现接收端（检查对方是否已启动接收、防火墙是否放行 UDP 45801）", "warn")
                self._json({"receivers": found})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"
            STATE.last_error = msg
            STATE.log(msg, "err")
            self._json({"ok": False, "error": msg}, 400)

    # -------------------------------------------------------------- SSE
    def _sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while True:
                payload = json.dumps(STATE.status(), ensure_ascii=False)
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                time.sleep(0.4)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


def serve(port: int = 8770, open_browser: bool = True, role: str = "recv",
          device: Optional[str] = None, host: str = "127.0.0.1") -> None:
    STATE.role = role or "recv"
    if device:
        STATE.config["device"] = device
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}/"
    print(f"AudioLink 控制台：{url}   （Ctrl+C 退出）")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
    finally:
        STATE.stop(silent=True)
        httpd.server_close()
