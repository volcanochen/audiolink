"""重采样质量对照：线性插值 vs 三次（Catmull-Rom）插值。

结论预期：
  - 同采样率（step=1.0）：两种插值都应接近完美，证明「无需重采样时不引入失真」。
  - 采样率不一致（如 44.1k -> 48k，ratio≈0.919）：线性插值会明显削掉高频，
    三次插值保住高频、误差低一个数量级——这正是「音质发闷」的根因与修复。
"""

import numpy as np


def resample(sig: np.ndarray, src_rate: int, out_rate: int, method: str) -> np.ndarray:
    ratio = src_rate / out_rate
    n_out = int(len(sig) * out_rate / src_rate)
    out = np.zeros(n_out)
    L = len(sig)
    for m in range(n_out):
        p = m * ratio
        i0 = int(np.floor(p))
        frac = p - i0
        if method == "lin":
            a = sig[i0]
            b = sig[i0 + 1] if i0 + 1 < L else sig[i0]
            out[m] = a + (b - a) * frac
        else:  # cubic
            j = max(1, min(i0, L - 3))
            x0, x1, x2, x3 = sig[j - 1], sig[j], sig[j + 1], sig[j + 2]
            f, f2, f3 = frac, frac * frac, frac ** 3
            out[m] = 0.5 * ((2 * x1) + (-x0 + x2) * f
                            + (2 * x0 - 5 * x1 + 4 * x2 - x3) * f2
                            + (-x0 + 3 * x1 - 3 * x2 + x3) * f3)
    return out


def band_energy(x: np.ndarray, rate: int, f_lo: float, f_hi: float) -> float:
    X = np.abs(np.fft.rfft(x))
    fr = np.fft.rfftfreq(len(x), 1.0 / rate)
    mask = (fr >= f_lo) & (fr < f_hi)
    return np.sqrt(np.mean(X[mask] ** 2)) if mask.any() else 0.0


def run(src_rate: int, out_rate: int, dur: float = 0.5) -> None:
    freqs = [100, 1000, 5000, 10000, 15000]
    n_src = int(src_rate * dur)
    t_src = np.arange(n_src) / src_rate
    sig = np.zeros(n_src)
    for f in freqs:
        sig = sig + np.sin(2 * np.pi * f * t_src)

    n_out = int(out_rate * dur)
    t_out = np.arange(n_out) / out_rate
    true = np.zeros(n_out)
    for f in freqs:
        true = true + np.sin(2 * np.pi * f * t_out)

    lin = resample(sig, src_rate, out_rate, "lin")
    cub = resample(sig, src_rate, out_rate, "cubic")

    print(f"\n=== {src_rate} -> {out_rate} (ratio={src_rate / out_rate:.4f}) ===")
    print(f"{'频段':<9}{'真值能量':>11}{'线性误差':>11}{'三次误差':>11}")
    bands = [("20-1k", 20, 1000), ("1k-5k", 1000, 5000),
             ("5k-10k", 5000, 10000), ("10k-16k", 10000, 16000)]
    tot_lin = tot_cub = 0.0
    for nm, lo, hi in bands:
        e_ref = band_energy(true, out_rate, lo, hi)
        el = abs(band_energy(lin, out_rate, lo, hi) - e_ref) / e_ref * 100
        ec = abs(band_energy(cub, out_rate, lo, hi) - e_ref) / e_ref * 100
        tot_lin += el
        tot_cub += ec
        print(f"{nm:<9}{e_ref:>11.2f}{el:>10.1f}%{ec:>10.1f}%")
    print(f"{'平均':<9}{'':>11}{tot_lin / 4:>10.1f}%{tot_cub / 4:>10.1f}%")


if __name__ == "__main__":
    run(48000, 48000)          # 同采样率：应几乎无损
    run(44100, 48000)          # 采样率不一致：线性应明显削高频，三次保住
