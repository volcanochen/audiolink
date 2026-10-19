"""命令行入口：python -m audiolink <命令>"""

from __future__ import annotations

import argparse
import os
import sys
import time
import unicodedata
from typing import List

from . import __version__, protocol as P
from .devices import (
    DeviceInfo,
    default_device,
    find_device,
    list_devices,
)


# ---------------------------------------------------------------- 小工具
def _w(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def pad(s: str, width: int) -> str:
    return s + " " * max(0, width - _w(s))


def _bar(rms: float) -> str:
    """用方块字符表示电平（0~1）。"""
    if rms <= 1e-5:
        return "".ljust(10, "·")
    db = 20 * __import__("math").log10(max(rms, 1e-6))
    n = int(max(0, min(10, (db + 60) / 6)))
    return "█" * n + "·" * (10 - n)


def _require_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


# ---------------------------------------------------------------- devices
def cmd_devices(args: argparse.Namespace) -> int:
    devs = list_devices()
    for kind, title in (("input", "输入 / 音源（[系统声音] = 可抓整机输出）"),
                        ("output", "输出 / 目标设备（可写进虚拟声卡）")):
        if args.kind and args.kind != kind:
            continue
        print(f"\n== {title} ==")
        rows = [d for d in devs if d.kind == kind]
        if not rows:
            print("  （无）")
        for d in rows:
            flag = "默认" if d.is_default else "    "
            name = d.label if _w(d.label) <= 58 else d.label[:40] + "…" + d.label[-10:]
            print(
                f"  {pad(d.uid, 10)}{pad(flag, 5)}{pad(name, 60)}"
                f"{d.channels}ch {d.samplerate}Hz  {d.hostapi}"
            )
    print()
    return 0


# ---------------------------------------------------------------- send
def _pick_source_device(args: argparse.Namespace) -> DeviceInfo:
    q = args.device
    if q in (None, "", "system", "loopback", "auto"):
        if args.source == "mic":
            return default_device("input")
        # 优先"当前默认播放设备"对应的回环（= 抓整机听到的声音）
        loops = [d for d in list_devices() if d.kind == "input" and d.is_loopback]
        if loops:
            try:
                dft_out = default_device("output")
                for d in loops:
                    if d.name == dft_out.name:
                        return d
            except LookupError:
                pass
            dft = [d for d in loops if d.is_default]
            return dft[0] if dft else loops[0]
        return default_device("input")
    return find_device(q, kind="input")


def cmd_send(args: argparse.Namespace) -> int:
    from .engine import CaptureSource, FileSource, Sender, discover, parse_peer

    peers: List[tuple] = []
    for p in args.peer or []:
        peers.append(parse_peer(p, args.port))
    if not peers and not args.no_discover:
        print("未指定 --peer，正在局域网内自动发现接收端 ...")
        found = discover()
        for f in found:
            print(f"  发现：{f.get('name')} @ {f.get('addr')}  设备={f.get('device')}")
            peers.append(parse_peer(f["addr"], args.port))
    if not peers:
        print("没有找到接收端。请在接收端机器上先运行：python -m audiolink recv ...")
        return 2

    print("\n== AudioLink 发送端 ==")
    if args.file:
        src = FileSource(args.file, loop=not args.no_loop)
        print(f"音源：{src.label}")
    else:
        dev = _pick_source_device(args)
        src = CaptureSource(dev)
        print(f"音源：{src.label}")
        if not dev.is_loopback and args.source != "mic":
            print("  提示：当前设备不是回环设备，抓到的可能是麦克风/线路输入。")
    print(f"目标：{', '.join(f'{h}:{p}' for h, p in peers)}")
    print(f"帧长：{P.FRAME_MS}ms  带宽：约 {src.rate * P.CHANNELS * 2 * 8 / 1e6:.2f} Mbps/路")
    print("按 Ctrl+C 结束\n")

    sender = Sender(src, peers, name=args.name or os.environ.get("COMPUTERNAME", ""),
                    port=args.port, volume=args.volume)
    sender.start()
    t0 = time.monotonic()
    last_pk = 0
    try:
        while sender.stats.running:
            time.sleep(0.5)
            s = sender.stats
            dt = time.monotonic() - t0
            t0 = time.monotonic()
            kbps = (s.bytes_sent - last_pk) * 8 / 1000.0 / max(dt, 1e-3)
            last_pk = s.bytes_sent
            peer_txt = ""
            for k, v in s.peers.items():
                peer_txt = (f" | 对端 {k} 收{v.get('pkts', 0)}包 丢{v.get('lost', 0)} "
                            f"缓冲{v.get('buffered_ms', 0)}ms")
                break
            if s.error:
                print(f"\n错误：{s.error}")
                break
            sys.stdout.write(
                f"\r↑ 已发 {s.packets} 包 {kbps:6.0f} kbps {_bar(s.rms)}{peer_txt}   "
            )
            sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    finally:
        sender.stop()
        print("\n已停止发送。")
    return 0


# ---------------------------------------------------------------- recv
def cmd_recv(args: argparse.Namespace) -> int:
    from .engine import Receiver

    if args.device:
        dev = find_device(args.device, kind="output")
    else:
        dev = default_device("output")
    print("\n== AudioLink 接收端 ==")
    print(f"目标设备：{dev.label}  ({dev.channels}ch {dev.samplerate}Hz)")
    print(f"监听：UDP {args.port}（发现端口 {P.PORT_DISCO}）")
    print(f"缓冲：{args.latency:.0f}ms  音量：{args.volume:.2f}")
    print("等待发送端连接 …… 按 Ctrl+C 结束\n")

    rx = Receiver(dev, port=args.port, latency_ms=args.latency,
                  volume=args.volume, name=args.name or os.environ.get("COMPUTERNAME", ""),
                  fade_ms=args.fade)
    try:
        rx.start()
    except Exception as e:  # noqa: BLE001
        print(f"启动失败：{e}")
        return 1
    try:
        while rx.stats.running:
            time.sleep(0.5)
            s = rx.stats
            if s.error:
                print(f"\n错误：{s.error}")
                break
            if s.waiting:
                sys.stdout.write("\r等待发送端 ……                                        ")
            else:
                sys.stdout.write(
                    f"\r↓ {pad(s.sender, 14)} {pad(s.sender_addr, 18)}"
                    f"收{s.packets} 丢{s.lost} 缓冲{s.buffered_ms:5.0f}/{s.target_ms:3.0f}ms "
                    f"抖动{s.jitter_ms:4.1f}ms 欠载{s.underruns} {_bar(s.rms)}   "
                )
            sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    finally:
        rx.stop()
        print("\n已停止接收。")
    return 0


# ---------------------------------------------------------------- discover
def cmd_discover(args: argparse.Namespace) -> int:
    from .engine import discover, probe

    if args.ip:
        info = probe(args.ip)
        if not info:
            print(f"{args.ip} 没有响应（可能未运行接收端，或防火墙拦截 UDP {P.PORT_DISCO}）")
            return 1
        print(f"{info.get('name')} @ {info['ip']}:{info.get('port')}  "
              f"设备={info.get('device')}")
        return 0
    print(f"正在广播发现（{args.timeout}s）……")
    found = discover(timeout=args.timeout)
    if not found:
        print("没有发现接收端。")
        return 1
    for f in found:
        print(f"  {pad(f.get('name', '?'), 18)}{pad(f['addr'], 22)}{f.get('device', '')}")
    return 0


# ---------------------------------------------------------------- ui
def cmd_ui(args: argparse.Namespace) -> int:
    from .webui import serve

    serve(port=args.port, open_browser=not args.no_browser, role=args.role, device=args.device)
    return 0


# ---------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="audiolink",
        description="AudioLink / 音链 —— 局域网跨平台音频共享套件",
    )
    ap.add_argument("-v", "--version", action="version", version=f"AudioLink {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("devices", help="列出本机音频设备")
    d.add_argument("--kind", choices=["input", "output"])
    d.set_defaults(func=cmd_devices)

    s = sub.add_parser("send", help="发送端：把本机声音推给远端")
    s.add_argument("--device", "-d", help="音源设备（名称/序号/uid；默认=默认播放设备的回环）")
    s.add_argument("--source", choices=["system", "mic"], default="system",
                   help="system=系统声音（Windows 回环），mic=麦克风/线路输入")
    s.add_argument("--file", "-f", help="改为推送本地音频文件（wav，或装 soundfile 后 mp3/flac）")
    s.add_argument("--no-loop", action="store_true", help="文件不循环")
    s.add_argument("--peer", "-p", action="append", help="接收端地址 IP 或 IP:端口，可多次")
    s.add_argument("--port", type=int, default=P.PORT_DATA, help="接收端数据端口")
    s.add_argument("--volume", type=float, default=1.0, help="发送增益 0~4")
    s.add_argument("--name", help="本机显示名")
    s.add_argument("--no-discover", action="store_true", help="不做自动发现")
    s.set_defaults(func=cmd_send)

    r = sub.add_parser("recv", help="接收端：把远端声音播到本机设备")
    r.add_argument("--device", "-d", help="播放设备（名称/序号/uid；留空=系统默认）")
    r.add_argument("--port", type=int, default=P.PORT_DATA, help="监听端口")
    r.add_argument("--latency", type=float, default=120.0, help="目标缓冲毫秒（默认 120）")
    r.add_argument("--fade", type=float, default=4.0, help="爆音抑制淡入/淡出时长毫秒（默认 4，设 0 关闭）")
    r.add_argument("--volume", type=float, default=1.0, help="播放音量 0~4")
    r.add_argument("--name", help="本机显示名")
    r.set_defaults(func=cmd_recv)

    f = sub.add_parser("discover", help="发现局域网内的接收端")
    f.add_argument("--ip", help="单播探测指定 IP")
    f.add_argument("--timeout", type=float, default=1.6)
    f.set_defaults(func=cmd_discover)

    u = sub.add_parser("ui", help="启动网页控制台")
    u.add_argument("--port", type=int, default=8770)
    u.add_argument("--no-browser", action="store_true")
    u.add_argument("--role", choices=["send", "recv"])
    u.add_argument("--device", help="预选设备")
    u.set_defaults(func=cmd_ui)
    return ap


def main(argv: List[str] | None = None) -> int:
    _require_utf8()
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except LookupError as e:
        print(f"设备错误：{e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001
        print(f"出错：{type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
