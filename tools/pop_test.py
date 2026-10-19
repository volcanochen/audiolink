"""爆音（click/pop）专项测试。

用 1kHz 正弦（@48k，相邻样本差上限约 0.13）模拟实时音频，依次制造三种爆音场景：
  1) 启动（首次 primed 前的静音->有声）
  2) 发送暂停 200ms 导致的欠载（缓冲耗尽后恢复）
  3) 周期性丢包（conceal 插静音）

对比 fade_ms=0（旧版硬切）与 fade_ms=4（淡入淡出）的输出中「相邻样本大跳变」次数。
爆音处样本会从 ±A 直接跳到 ∓B（跳变远超正常正弦上限 0.13），用阈值过滤即可量化。
"""

import sys

import numpy as np

sys.path.insert(0, ".")
from audiolink.jitter import JitterBuffer  # noqa: E402


def gen_sine(n: int, rate: int = 48000, freq: float = 1000.0, amp: float = 0.6):
    t = np.arange(n) / rate
    x = amp * np.sin(2 * np.pi * freq * t)
    return np.stack([x, x], axis=1).astype(np.float32)


def run(fade_ms: float, rate: int = 48000, block: int = 480):
    # 用较小 target + 刚好够 primed 的预填，确保「暂停」能真正耗尽缓冲触发欠载
    jb = JitterBuffer(rate, rate, target_ms=120, fade_ms=fade_ms)
    # 预填约 130ms（刚好 primed，余量很小，暂停即可耗尽）
    for _ in range(13):
        jb.write(gen_sine(block, rate))

    out = []

    # 阶段1：实时 1s（边写边读）
    for _ in range(int(rate / block)):
        jb.write(gen_sine(block, rate))
        b, _ = jb.read_block(block)
        out.append(b)

    # 阶段2：发送暂停 200ms（只读不写 -> 必然欠载后恢复）
    for _ in range(int(0.2 * rate / block)):
        b, _ = jb.read_block(block)
        out.append(b)

    # 阶段3：恢复实时 1s，并周期性丢一整块
    for i in range(int(rate / block)):
        if i % 15 == 7:
            jb.conceal(block)
        else:
            jb.write(gen_sine(block, rate))
        b, _ = jb.read_block(block)
        out.append(b)

    out = np.concatenate(out, axis=0)
    # 相邻样本最大声道差
    diff = np.max(np.abs(np.diff(out, axis=0)), axis=1)
    thr = 0.15
    clicks = int((diff > thr).sum())
    max_jump = float(diff.max())
    return clicks, max_jump


if __name__ == "__main__":
    print("场景：1kHz 正弦 @48k，阶段=启动/200ms 欠载/周期丢包；跳变阈值=0.15（正常上限≈0.078）")
    print("-" * 70)
    results = {}
    for fm in (0.0, 4.0):
        clicks, mj = run(fm)
        results[fm] = (clicks, mj)
        print(f"  fade_ms={fm:>4} : 爆音跳变次数 = {clicks:5d}    最大跳变 = {mj:.3f}")
    old, new = results[0.0], results[4.0]
    print("-" * 70)
    if new[0] == 0 and (old[0] > 0 or old[1] > new[1] * 1.5):
        print(f"  ✅ 修复有效：爆音跳变 {old[0]} -> {new[0]}，"
              f"最大跳变 {old[1]:.3f} -> {new[1]:.3f}")
    elif old[0] == 0 and new[0] == 0 and old[1] <= new[1] + 1e-6:
        print("  ⚠️  旧版也未测出爆音（场景未触发），需检查测试")
    else:
        print("  ❌ 未见改善，需排查")
    sys.exit(0 if new[0] <= old[0] and new[1] <= old[1] else 1)
