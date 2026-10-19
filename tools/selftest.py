"""AudioLink 自测。

两个模式：

  python tools/selftest.py sim
      纯数值仿真：模拟两端声卡晶振不同源（源时钟比输出时钟快 3000ppm），
      检查抖动缓冲的漂移补偿是否把水位稳定在目标值附近。

  python tools/selftest.py live [--tone-dev 设备] [--sink-dev 设备]
      真实链路：往一个虚拟播放设备打 1kHz 正弦波 -> 回环采集 -> UDP -> 接收端渲染到
      另一个（静音）虚拟设备，检查丢包/欠载/电平。全程不外放，不打扰人。
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audiolink import protocol as P  # noqa: E402
from audiolink.devices import (  # noqa: E402
    find_device,
    list_devices,
    open_render_compatible,
    stereo_to_int16,
)
from audiolink.engine import CaptureSource, Receiver, Sender  # noqa: E402
from audiolink.jitter import JitterBuffer  # noqa: E402


# --------------------------------------------------------------------- 仿真
def sim() -> int:
    rate_out = 48000
    rate_src = int(48000 * 1.003)          # 源快 0.3%（现场常见 0.005%，这里故意放大 60 倍）
    jb = JitterBuffer(rate_src, rate_out, target_ms=120)
    block_src = 240

    samples = []
    for i in range(6000):                  # 约 30 秒音频
        t = np.arange(block_src) / rate_src + i * block_src / rate_src
        x = 0.2 * np.sin(2 * np.pi * 440 * t)
        jb.write(np.stack([x, x], axis=1).astype(np.float32))
        jb.read_block(240)
        if i > 600:
            samples.append(jb.count * 1000.0 / jb.src)

    arr = np.array(samples)
    drift_ppm = (rate_src / rate_out - 1.0) * 1e6
    print(f"仿真：源 {rate_src}Hz vs 输出 {rate_out}Hz（+{drift_ppm:.0f} ppm）")
    print(f"缓冲水位：均值 {arr.mean():.1f} ms，最小 {arr.min():.1f}，最大 {arr.max():.1f}"
          f"（目标 {jb.target_ms:.0f} ms）")
    st = jb.stats()
    print(f"欠载 {st.underruns} 次，溢出 {st.overflows} 次，实际重采样比 "
          f"{st.effective_ratio:.6f}")
    ok = abs(arr.mean() - jb.target_ms) < 40 and arr.max() < 400 and st.underruns <= 2
    print("结果：" + ("通过 ✅ 漂移被吸收，水位稳定" if ok else "不通过 ❌"))
    return 0 if ok else 1


# --------------------------------------------------------------------- 真实链路
def _tone_player(dev, seconds: float, stop: threading.Event, freq: float = 1000.0) -> None:
    rate = dev.samplerate
    block = 240
    stream, rate, ch = open_render_compatible(dev, rate, 2, block)
    t = 0.0
    t0 = time.perf_counter()
    try:
        while not stop.is_set() and time.perf_counter() - t0 < seconds:
            i = np.arange(block) / rate + t
            x = 0.25 * np.sin(2 * np.pi * freq * i)
            frame = np.stack([x, x], axis=1).astype(np.float32)
            stream.write(stereo_to_int16(frame, ch, 1.0))
            t += block / rate
    finally:
        stream.close()


def live(args: argparse.Namespace) -> int:
    devs = list_devices()
    loops = [d for d in devs if d.kind == "input" and d.is_loopback]
    outputs = [d for d in devs if d.kind == "output"]
    if not loops or len(outputs) < 2:
        print("本机缺少虚拟设备，live 模式需要至少一个可回环的播放设备 + 一个可静音播放的设备")
        return 2

    tone_dev = find_device(args.tone_dev, "output") if args.tone_dev else outputs[0]
    cap_dev = None
    for d in loops:
        if d.name == tone_dev.name:
            cap_dev = d
            break
    if cap_dev is None:
        print(f"找不到「{tone_dev.name}」的回环设备")
        return 2

    others = [d for d in outputs if d.uid != tone_dev.uid]
    sink_dev = find_device(args.sink_dev, "output") if args.sink_dev else others[0]
    if sink_dev.uid == tone_dev.uid:
        print("警告：音源与接收设备相同，会形成回授环（这里仅用于测量，不要外放）")

    port = 45999
    print(f"音源设备 : {tone_dev.label}")
    print(f"回环采集 : {cap_dev.label}")
    print(f"接收输出 : {sink_dev.label}")
    print(f"端口     : {port}\n")

    rx = Receiver(sink_dev, port=port, latency_ms=args.latency, volume=0.6)
    rx.start()
    time.sleep(0.4)

    stop = threading.Event()
    th = threading.Thread(target=_tone_player, args=(tone_dev, args.seconds, stop), daemon=True)
    th.start()

    src = CaptureSource(cap_dev)
    tx = Sender(src, [("127.0.0.1", port)], name="selftest", port=port)
    tx.start()

    t0 = time.monotonic()
    peak_rms = 0.0
    while time.monotonic() - t0 < args.seconds - 0.2:
        time.sleep(0.25)
        peak_rms = max(peak_rms, rx.stats.rms)
        sys.stdout.write(
            f"\r收 {rx.stats.packets:5d} 包  丢 {rx.stats.lost:3d}  缓冲 "
            f"{rx.stats.buffered_ms:5.1f}/{rx.stats.target_ms:3.0f}ms  "
            f"欠载 {rx.stats.underruns:2d}  输出电平 {rx.stats.rms:.4f}   "
        )
        sys.stdout.flush()

    stop.set()
    time.sleep(0.2)
    tx.stop()
    rx.stop()
    th.join(timeout=1.0)

    s = rx.stats
    print("\n")
    print(f"发送包数 {s.packets}，丢包 {s.lost}，迟到 {s.late}，欠载 {s.underruns}")
    print(f"源采样率 {s.source_rate}Hz -> 输出 {s.out_rate}Hz/{s.out_channels}ch")
    print(f"峰值电平 {peak_rms:.4f}（>0.02 表示信号确实从对端流过来了）")
    print(f"抖动估计 {s.jitter_ms:.2f} ms，缓冲 {s.buffered_ms:.1f}/{s.target_ms:.0f} ms")
    if s.error:
        print(f"错误：{s.error}")

    ok = s.packets > 100 and s.lost <= 2 and s.underruns <= 5 and peak_rms > 0.02
    print("结果：" + ("通过 ✅ 端到端链路可用" if ok else "不通过 ❌"))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["sim", "live"], nargs="?", default="sim")
    ap.add_argument("--tone-dev", help="打入音调的目标播放设备")
    ap.add_argument("--sink-dev", help="接收端渲染设备（建议选静音虚拟设备）")
    ap.add_argument("--seconds", type=float, default=4.0)
    ap.add_argument("--latency", type=float, default=120.0)
    return sim() if ap.parse_args().mode == "sim" else live(ap.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
