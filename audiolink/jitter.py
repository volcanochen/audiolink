"""抖动缓冲 + 时钟漂移补偿 + 丢包补偿 + 爆音抑制。

四件事一起解决：

1. 抗抖动：UDP 到达时间不均匀，先把数据放进环形缓冲，攒够 target_ms 再开始播放，
   之后按填充水位"软"调节读取速度（正弦级 ±1.5% 的变比），把水位拉回目标值。
2. 时钟漂移：发送端和接收端是两块独立声卡，48kHz 名义相同、实际差几十 ppm。
   若不做补偿，1 小时会漂 200~500ms，最终必然爆音或断流。做法是把"填充水位"当
   被控量、把"重采样步长"当控制量做 PI 控制（P 段比例 + I 段积分吸收固有偏差）。
3. 丢包补偿：序号跳变时补等长静音，保持时间轴长度，重新对齐即可继续。
4. 爆音抑制（v0.2 修复"会有爆音"）：
   - 增益包络（淡入/淡出）：任何"有声→静音"的切换（启动、欠载、恢复）都走 4ms
     斜坡，绝不直接硬切，消除喀嗒声。
   - 丢包静音段边界淡入淡出：conceal 插入的静音段，其前后 4ms 用斜坡盖住，
     避免"有声-静音-有声"的硬边界爆音。
   - 软限幅在 devices.stereo_to_int16 中处理（增益叠加超量程时 tanh 软饱和而非硬方波）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np


@dataclass
class JitterStats:
    buffered_ms: float = 0.0
    target_ms: float = 0.0
    underruns: int = 0
    overflows: int = 0
    concealed_frames: int = 0
    effective_ratio: float = 1.0


class JitterBuffer:
    """源采样率 -> 输出采样率 的环形缓冲，附带漂移自适应与爆音抑制。

    写：源采样率下的 float32 (n, 2)
    读：输出采样率下的 float32 (n, 2)
    """

    def __init__(
        self,
        src_rate: int,
        out_rate: int,
        target_ms: float = 120.0,
        max_ms: float = 1500.0,
        adapt: bool = True,
        fade_ms: float = 4.0,
    ) -> None:
        self.src = float(max(1, src_rate))
        self.out = float(max(1, out_rate))
        self.capacity = int(self.src * max_ms / 1000.0)
        self.buf = np.zeros((self.capacity, 2), dtype=np.float32)
        # 标记每个源帧是否来自「丢包静音补偿」——读出时对其边界做淡入淡出
        self.tag = np.zeros(self.capacity, dtype=np.uint8)

        self.wpos = 0                 # 写指针（模 capacity）
        self.rpos = 0.0               # 读指针（模 capacity，浮点）
        self.count = 0.0              # 缓冲内帧数（源帧单位）

        self.base_step = self.src / self.out
        self.step = self.base_step

        self.target = self.src * target_ms / 1000.0
        self.target_ms = target_ms
        self.max_target = self.src * max_ms * 0.5 / 1000.0
        self.min_target = self.src * 30.0 / 1000.0
        self.adapt = adapt

        self.primed = False
        self.underruns = 0
        self.overflows = 0
        self.concealed_frames = 0
        self.integ = 0.0             # 积分项：吸收两端时钟的固有偏差
        self._level_ema = self.target  # 慢速平滑水位（滤掉到达抖动，避免速率抖动）
        self._last_write = 0.0

        # ---- 爆音抑制：增益包络（淡入/淡出）----
        self.fade_samples = max(1, int(fade_ms / 1000.0 * self.out))
        # 初始静音：攒够缓冲后才淡入，避免「第一帧直接非零」的启动喀嗒
        self._muted = True
        self._gain = 0.0

    # ------------------------------------------------------------------ 写
    def write(self, frames: np.ndarray) -> None:
        n = int(frames.shape[0])
        if n <= 0:
            return
        if n > self.capacity:                       # 病态输入
            frames = frames[-self.capacity:]
            n = self.capacity

        if self.count + n > self.capacity:          # 溢出：丢最老的
            drop = self.count + n - self.capacity
            self.rpos = (self.rpos + drop) % self.capacity
            self.count -= drop
            self.overflows += 1
            self.primed = False                     # 数据已不连续，重新预缓冲

        pos = self.wpos
        end = pos + n
        if end <= self.capacity:
            self.buf[pos:end] = frames
            self.tag[pos:end] = 0
        else:
            k = self.capacity - pos
            self.buf[pos:] = frames[:k]
            self.tag[pos:] = 0
            self.buf[: end - self.capacity] = frames[k:]
            self.tag[: end - self.capacity] = 0
        self.wpos = end % self.capacity
        self.count += n
        self._last_write = time.monotonic()

    def conceal(self, frames: int) -> None:
        """丢包补偿：插入等长静音，保持时间轴长度；并标记该段供边界淡入淡出。"""
        if frames <= 0:
            return
        frames = min(int(frames), int(self.src * 0.5))
        self.write(np.zeros((frames, 2), dtype=np.float32))
        # 刚写入的整段标记为「丢包静音」，读出时在其前后做 4ms 斜坡
        pos = (self.wpos - frames) % self.capacity
        end = pos + frames
        if end <= self.capacity:
            self.tag[pos:end] = 1
        else:
            self.tag[pos:] = 1
            self.tag[: end - self.capacity] = 1
        self.concealed_frames += frames

    # ------------------------------------------------------------------ 读
    def read_block(self, n: int) -> tuple[np.ndarray, bool]:
        """返回 (n,2) float32 与 underrun 标志。"""
        need = n * self.step

        if not self.primed:
            if self.count < self.target:
                out = np.zeros((n, 2), dtype=np.float32)
                self._apply_fade(out)
                return out, True
            self.primed = True

        if self.count < need:                        # 欠载
            avail = int(self.count)
            got = 0
            out = np.zeros((n, 2), dtype=np.float32)
            if avail > 0:
                got = min(n, int(avail / self.step))
                if got > 0:
                    out[:got] = self._interp(self.rpos, self.step, got)
                    self.rpos = (self.rpos + got * self.step) % self.capacity
                    self.count -= got * self.step
            self.underruns += 1
            if self.adapt:
                # 轻微抬高目标水位，避免反复欠载（上限 max_target）
                self.target = min(self.max_target, self.target * 1.15)
                self.target_ms = self.target * 1000.0 / self.src
            self.primed = False
            self.count = 0.0
            self.rpos = float(self.wpos)                 # 重新对齐写指针
            self._muted = True                          # 触发淡出，消除「有声→零」硬切
            self._apply_fade(out)
            return out, True

        # ---- 正常读出 ----
        out = self._interp(self.rpos, self.step, n)
        cmask = self._conceal_mask(self.rpos, n)        # 用读出前的 rpos
        used = need
        self.rpos = (self.rpos + used) % self.capacity
        self.count -= used
        if self._muted and self.count >= self.target:
            self._muted = False                         # 恢复：下一块起淡入
        self._control(n)

        self._apply_fade(out)
        if cmask is not None:
            out *= cmask[:, None]
        return out, False

    def _conceal_mask(self, rpos: float, n: int):
        """为丢包静音段构造淡入/淡出窗（0=静音区，1=有声区，边界斜坡）。

        采样窗口向前后各扩展 fade 个输出样本，使斜坡可以「跨读块边界」连续——
        否则当静音段恰好落在某读块的起点时，下坡斜坡会被块边界裁掉，造成硬切爆音。
        返回 None 表示本块附近无静音段，无需乘。
        """
        if self.fade_samples <= 1:
            return None
        fade = self.fade_samples
        n_full = n + 2 * fade
        # 窗口中心对齐本块 [0, n)：full 索引 k 对应本块样本 k-fade
        idx = (np.floor(rpos + (np.arange(n_full, dtype=np.float64) - fade) * self.step)
               % self.capacity).astype(np.int64)
        s = self.tag[idx] == 1
        if not s.any():
            return None
        g = np.where(s, 0.0, 1.0)
        idxs = np.where(s)[0]
        jumps = np.where(np.diff(idxs) > 1)[0]
        starts = [int(idxs[0])] + [int(idxs[j + 1]) for j in jumps]
        ends = [int(idxs[j]) for j in jumps] + [int(idxs[-1])]
        for a, b in zip(starts, ends):
            lo = a - fade
            if lo < 0:
                lo = 0
            if lo < a:                                 # 静音起点前 fade：1 -> 0
                ramp = (np.arange(lo, a) - lo) / fade
                g[lo:a] = np.minimum(g[lo:a], ramp)
            hi = b + 1 + fade
            if hi > n_full:
                hi = n_full
            if b + 1 < hi:                            # 静音终点后 fade：0 -> 1
                ramp = (np.arange(b + 1, hi) - (b + 1)) / fade
                g[b + 1:hi] = np.minimum(g[b + 1:hi], ramp)
        return g[fade:fade + n].astype(np.float32)

    def _apply_fade(self, out: np.ndarray) -> None:
        """按 _muted 状态对 out 施加整块增益斜坡（淡入/淡出）。"""
        if self.fade_samples <= 1 or out.shape[0] == 0:
            return
        target = 0.0 if self._muted else 1.0
        g0 = self._gain
        per = 1.0 / self.fade_samples
        n = out.shape[0]
        if g0 < target:
            g1 = min(target, g0 + per * n)
        elif g0 > target:
            g1 = max(target, g0 - per * n)
        else:
            g1 = g0
        if g1 != g0:
            ramp = np.linspace(g0, g1, n)[:, None].astype(np.float32)
            out *= ramp
        self._gain = g1

    def _interp(self, pos: float, step: float, n: int) -> np.ndarray:
        """三次（Catmull-Rom）插值，取代原先的线性插值。

        线性插值等价于一个劣质低通，会削掉高频、让声音发闷（差音质 MP3 那种感觉），
        尤其当两端采样率不一致（如 44.1k<->48k，base_step≈0.919）时尤为明显。
        三次插值几乎无损地保住高频与相位，且 step=1.0、整点位置时返回原样样本
        （round-trip 误差在 1e-4 量级），无需重采样时画质透明。
        """
        idx = pos + np.arange(n, dtype=np.float64) * step
        i0 = np.floor(idx).astype(np.int64)
        frac = (idx - np.floor(idx))[:, None]                      # (n,1) float64
        cap = self.capacity
        p0 = self.buf[(i0 - 1) % cap]
        p1 = self.buf[i0 % cap]
        p2 = self.buf[(i0 + 1) % cap]
        p3 = self.buf[(i0 + 2) % cap]
        f = frac
        f2 = f * f
        f3 = f2 * f
        out = 0.5 * (
            (2.0 * p1)
            + (-p0 + p2) * f
            + (2.0 * p0 - 5.0 * p1 + 4.0 * p2 - p3) * f2
            + (-p0 + 3.0 * p1 - 3.0 * p2 + p3) * f3
        )
        return out.astype(np.float32)

    def _control(self, n: int) -> None:
        """低速、平滑地调整重采样步长，把缓冲水位拉回目标值。

        修复「音质怪」的两大来源：
        1. 用慢速 EMA 平滑水位，滤掉 UDP 到达抖动的瞬时噪声——否则步长会随每个包
           忽快忽慢，产生可闻的音调抖动（wow/flutter / 轻微跑调）。
        2. 即使需要修正，每 10ms 也只向目标「滑」极小一步（<3e-5），速率变化远在人耳
           可辨阈值（~0.1%）与可辨调制频率之外；收敛是缓慢的一次性微小音高微调，
           而非持续抖动。
        积分项吸收两端声卡时钟的固有 ppm 偏差，让水位真正回到目标、而非漏到低位稳住。
        """
        self._level_ema = self._level_ema * 0.9992 + self.count * 0.0008
        err = self._level_ema - self.target              # 正：水位偏高
        self.integ = float(np.clip(self.integ + err * 8e-9, -0.02, 0.02))
        desired_corr = float(np.clip(err / (self.src * 50.0) + self.integ,
                                     -0.025, 0.025))
        desired_step = self.base_step * (1.0 + desired_corr)
        # 限速滑动：杜绝可闻的速率抖动
        self.step += float(np.clip(desired_step - self.step, -3e-5, 3e-5))

    # ------------------------------------------------------------- 状态
    def reset(self) -> None:
        self.wpos = 0
        self.rpos = 0.0
        self.count = 0.0
        self.step = self.base_step
        self.integ = 0.0
        self._level_ema = self.target
        self.primed = False
        self.tag[:] = 0
        # 重开后从静音重新淡入，避免喀嗒
        self._muted = True
        self._gain = 0.0

    def stats(self) -> JitterStats:
        return JitterStats(
            buffered_ms=self.count * 1000.0 / self.src,
            target_ms=self.target_ms,
            underruns=self.underruns,
            overflows=self.overflows,
            concealed_frames=self.concealed_frames,
            effective_ratio=self.step / self.base_step * (self.out / self.src),
        )

    def silent_for(self) -> float:
        return time.monotonic() - self._last_write
