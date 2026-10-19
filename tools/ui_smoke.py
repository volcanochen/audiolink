"""网页控制台 + 引擎的集成冒烟测试。

流程：拉起 ui 服务 -> 读设备 -> 启动接收端 -> 另起进程推送一路音频
      -> 校验状态面（包数/缓冲/丢包）-> 停止 -> 关闭服务。

用法：python tools/ui_smoke.py [--device 设备名] [--seconds 6]
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def api(port: int, path: str, payload: dict | None = None) -> dict:
    url = f"http://127.0.0.1:{port}{path}"
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"},
        method="GET" if payload is None else "POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def wait_up(port: int, timeout: float = 15.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            api(port, "/api/status")
            return True
        except Exception:
            time.sleep(0.3)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", help="接收端渲染设备（默认自动=系统默认输出）")
    ap.add_argument("--source", help="发送端采集设备（默认自动）")
    ap.add_argument("--seconds", type=float, default=6.0)
    args = ap.parse_args()

    py = sys.executable
    port = free_port()
    # 用一次性随机数据端口，避免本机残留的旧接收端仍占用默认 45800 造成抢包
    rport = free_port()
    env = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING="utf-8")

    print(f"[1] 启动 ui :{port}")
    ui = subprocess.Popen(
        [py, "-m", "audiolink", "ui", "--port", str(port), "--no-browser"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    ok = True
    try:
        if not wait_up(port):
            print("    服务没起来：", ui.stdout.read().decode("utf-8", "replace")[-800:])
            return 1

        devs = api(port, "/api/devices")
        loops = [d for d in devs["inputs"] if d["is_loopback"]]
        print(f"    设备：输入 {len(devs['inputs'])}（回环 {len(loops)}），"
              f"输出 {len(devs['outputs'])}")

        print("[2] 启动接收端")
        r = api(port, "/api/start", {
            "role": "recv", "device": args.device or "auto",
            "latency": 120, "volume": 0.5, "port": rport,
        })
        print("    ", r)
        time.sleep(1.0)
        st = api(port, "/api/status")
        if not st.get("running"):
            print("    接收端未启动：", st.get("error"))
            return 1
        print(f"    渲染设备：{st['recv']['device']}  "
              f"输出 {st['recv']['out_rate']}Hz/{st['recv']['out_channels']}ch  端口 {rport}")

        print("[3] 启动发送端（另一进程）")
        tx = subprocess.Popen(
            [py, "-m", "audiolink", "send", "--no-discover",
             "--peer", f"127.0.0.1:{rport}",
             "--device", args.source or "auto", "--volume", "0.5"],
            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        t0 = time.monotonic()
        peak_pkts = 0
        while time.monotonic() - t0 < args.seconds:
            time.sleep(0.8)
            st = api(port, "/api/status")
            rc = st.get("recv", {})
            peak_pkts = max(peak_pkts, rc.get("packets", 0))
            print(f"    收 {rc.get('packets'):>5} 包  丢 {rc.get('lost')}  "
                  f"缓冲 {rc.get('buffered_ms')}ms  抖动 {rc.get('jitter_ms')}ms  "
                  f"电平 {rc.get('rms')}")

        print("[4] 发现接口")
        try:
            d = api(port, "/api/discover", {})
            print(f"    发现 {len(d.get('receivers', []))} 个接收端")
        except Exception as e:
            print(f"    发现失败（广播可能被环境限制）：{e}")

        print("[5] 停止")
        api(port, "/api/stop", {})
        stop_st = api(port, "/api/status")

        tx.terminate()
        try:
            tx.wait(timeout=5)
        except subprocess.TimeoutExpired:
            tx.kill()

        ok = (peak_pkts > 50 and stop_st.get("running") is False
              and not stop_st.get("error"))
        print("\n结果：" + ("通过 ✅" if ok else "不通过 ❌"))
        return 0 if ok else 1
    finally:
        api_safe_stop = None
        try:
            if ui.poll() is None:
                ui.terminate()
                ui.wait(timeout=5)
        except Exception:
            ui.kill()


if __name__ == "__main__":
    raise SystemExit(main())
