"""声卡后端抽象：Windows 用 PyAudioWPatch（支持 WASAPI 回环），其它系统用 sounddevice。

统一三件事：
  - 枚举设备（区分输入/输出，标记"回环=可抓系统声音"的虚拟输入）
  - 打开采集流（阻塞式 read，每块 5ms）
  - 打开播放流（阻塞式 write）

Linux 上"系统声音"的抓法：PipeWire/PulseAudio 会暴露 `Monitor of <输出设备>` 输入源，
在设备列表里会被标记成「系统声音」；macOS 需先装 BlackHole 之类的虚拟声卡。
"""

from __future__ import annotations

import platform
import threading
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

IS_WIN = platform.system() == "Windows"


@dataclass
class DeviceInfo:
    uid: str
    name: str
    kind: str                  # 'input' | 'output'
    channels: int
    samplerate: int
    is_loopback: bool = False  # True = 可抓取系统声音
    hostapi: str = ""
    is_default: bool = False
    native_index: int = -1
    backend: str = ""

    @property
    def label(self) -> str:
        tag = "[系统声音] " if self.is_loopback else ""
        return f"{tag}{self.name}"

    def to_dict(self) -> dict:
        return {
            "uid": self.uid,
            "name": self.name,
            "label": self.label,
            "kind": self.kind,
            "channels": self.channels,
            "samplerate": self.samplerate,
            "is_loopback": self.is_loopback,
            "hostapi": self.hostapi,
            "is_default": self.is_default,
            "backend": self.backend,
        }


class CaptureStream:
    def read(self, frames: int) -> bytes:  # pragma: no cover - 接口
        raise NotImplementedError

    def close(self) -> None:
        pass


class RenderStream:
    def write(self, data: bytes) -> None:  # pragma: no cover - 接口
        raise NotImplementedError

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------- Windows
class _WinBackend:
    name = "pyaudiowpatch"

    def __init__(self) -> None:
        import pyaudiowpatch as pyaudio  # 延迟导入，非 Windows 不安装

        self.pa_mod = pyaudio
        self._pa = None
        self._lock = threading.Lock()

    @property
    def pa(self):
        with self._lock:
            if self._pa is None:
                self._pa = self.pa_mod.PyAudio()
            return self._pa

    def list_devices(self) -> List[DeviceInfo]:
        pa = self.pa
        mod = self.pa_mod
        out: List[DeviceInfo] = []
        hi = pa.get_host_api_info_by_type(mod.paWASAPI)
        host_name = hi.get("name", "WASAPI")
        default_in = hi.get("defaultInputDevice", -1)
        default_out = hi.get("defaultOutputDevice", -1)

        for i in range(hi["deviceCount"]):
            d = pa.get_device_info_by_host_api_device_index(hi["index"], i)
            idx = int(d["index"])
            rate = int(round(float(d.get("defaultSampleRate", 48000))))
            loop = bool(d.get("isLoopbackDevice", False))
            name = str(d.get("name", f"device {idx}")).replace(" [Loopback]", "")
            if loop:
                out.append(
                    DeviceInfo(
                        uid=f"loop:{idx}",
                        name=name,
                        kind="input",
                        channels=int(d.get("maxInputChannels", 2)),
                        samplerate=rate,
                        is_loopback=True,
                        hostapi=host_name,
                        is_default=(idx == default_in),
                        native_index=idx,
                        backend=self.name,
                    )
                )
            else:
                if int(d.get("maxInputChannels", 0)) > 0:
                    out.append(
                        DeviceInfo(
                            uid=f"in:{idx}",
                            name=name,
                            kind="input",
                            channels=int(d["maxInputChannels"]),
                            samplerate=rate,
                            hostapi=host_name,
                            is_default=(idx == default_in),
                            native_index=idx,
                            backend=self.name,
                        )
                    )
                if int(d.get("maxOutputChannels", 0)) > 0:
                    out.append(
                        DeviceInfo(
                            uid=f"out:{idx}",
                            name=name,
                            kind="output",
                            channels=int(d["maxOutputChannels"]),
                            samplerate=rate,
                            hostapi=host_name,
                            is_default=(idx == default_out),
                            native_index=idx,
                            backend=self.name,
                        )
                    )
        return out

    def open_capture(self, dev: DeviceInfo, rate: int, channels: int, blocksize: int) -> CaptureStream:
        mod = self.pa_mod
        pa = self.pa

        class _S(CaptureStream):
            def __init__(self) -> None:
                self.stream = pa.open(
                    format=mod.paInt16,
                    channels=channels,
                    rate=rate,
                    input=True,
                    input_device_index=dev.native_index,
                    frames_per_buffer=blocksize,
                )

            def read(self, frames: int) -> bytes:
                return self.stream.read(frames, exception_on_overflow=False)

            def close(self) -> None:
                try:
                    self.stream.stop_stream()
                finally:
                    self.stream.close()

        return _S()

    def open_render(self, dev: DeviceInfo, rate: int, channels: int, blocksize: int) -> RenderStream:
        mod = self.pa_mod
        pa = self.pa

        class _R(RenderStream):
            def __init__(self) -> None:
                self.stream = pa.open(
                    format=mod.paInt16,
                    channels=channels,
                    rate=rate,
                    output=True,
                    output_device_index=dev.native_index,
                    frames_per_buffer=blocksize,
                )

            def write(self, data: bytes) -> None:
                self.stream.write(data)

            def close(self) -> None:
                try:
                    self.stream.stop_stream()
                finally:
                    self.stream.close()

        return _R()


# --------------------------------------------------------------------------- 其它系统
class _SdBackend:
    name = "sounddevice"

    def __init__(self) -> None:
        import sounddevice as sd

        self.sd = sd

    def list_devices(self) -> List[DeviceInfo]:
        sd = self.sd
        out: List[DeviceInfo] = []
        hostapis = sd.query_hostapis()
        defaults = sd.default.device  # (in, out)

        for i, d in enumerate(sd.query_devices()):
            ha = hostapis[d["hostapi"]]["name"] if d["hostapi"] < len(hostapis) else ""
            rate = int(round(float(d.get("default_samplerate", 48000))))
            name = str(d["name"])
            low = name.lower()
            is_monitor = ("monitor" in low) or ("monitor" in ha.lower())
            if d["max_input_channels"] > 0:
                out.append(
                    DeviceInfo(
                        uid=f"in:{i}",
                        name=name,
                        kind="input",
                        channels=int(d["max_input_channels"]),
                        samplerate=rate,
                        is_loopback=is_monitor,
                        hostapi=ha,
                        is_default=(i == defaults[0]),
                        native_index=i,
                        backend=self.name,
                    )
                )
            if d["max_output_channels"] > 0:
                out.append(
                    DeviceInfo(
                        uid=f"out:{i}",
                        name=name,
                        kind="output",
                        channels=int(d["max_output_channels"]),
                        samplerate=rate,
                        hostapi=ha,
                        is_default=(i == defaults[1]),
                        native_index=i,
                        backend=self.name,
                    )
                )
        return out

    def open_capture(self, dev: DeviceInfo, rate: int, channels: int, blocksize: int) -> CaptureStream:
        sd = self.sd

        class _S(CaptureStream):
            def __init__(self) -> None:
                self.stream = sd.RawInputStream(
                    samplerate=rate,
                    channels=channels,
                    dtype="int16",
                    blocksize=blocksize,
                    device=dev.native_index,
                )
                self.stream.start()

            def read(self, frames: int) -> bytes:
                data, _ = self.stream.read(frames)
                return bytes(data)

            def close(self) -> None:
                try:
                    self.stream.stop()
                finally:
                    self.stream.close()

        return _S()

    def open_render(self, dev: DeviceInfo, rate: int, channels: int, blocksize: int) -> RenderStream:
        sd = self.sd

        class _R(RenderStream):
            def __init__(self) -> None:
                self.stream = sd.RawOutputStream(
                    samplerate=rate,
                    channels=channels,
                    dtype="int16",
                    blocksize=blocksize,
                    device=dev.native_index,
                )
                self.stream.start()

            def write(self, data: bytes) -> None:
                self.stream.write(bytes(data))

            def close(self) -> None:
                try:
                    self.stream.stop()
                finally:
                    self.stream.close()

        return _R()


_backend = None
_backend_lock = threading.Lock()


def backend():
    global _backend
    with _backend_lock:
        if _backend is None:
            _backend = _WinBackend() if IS_WIN else _SdBackend()
        return _backend


def list_devices() -> List[DeviceInfo]:
    return backend().list_devices()


def find_device(query: str, kind: Optional[str] = None) -> DeviceInfo:
    """按 uid / 序号 / 名称子串（不区分大小写）查找设备。"""
    q = (query or "").strip()
    devs = list_devices()
    if kind:
        devs = [d for d in devs if d.kind == kind]
    if not q:
        raise LookupError("未指定设备")
    for d in devs:
        if d.uid == q:
            return d
    if q.isdigit():
        n = int(q)
        numbered = [d for d in devs if d.native_index == n]
        if len(numbered) == 1:
            return numbered[0]
    low = q.lower()
    hits = [d for d in devs if low in d.name.lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise LookupError(f"找不到设备：{q}")
    exact = [d for d in hits if d.name.lower() == low]
    if len(exact) == 1:
        return exact[0]
    cand = "；".join(f"{d.uid} {d.label}" for d in hits[:6])
    raise LookupError(f"设备名有歧义，请用 uid 指定：{cand}")


def default_device(kind: str) -> DeviceInfo:
    devs = [d for d in list_devices() if d.kind == kind and d.is_default]
    if devs:
        return devs[0]
    devs = [d for d in list_devices() if d.kind == kind]
    if not devs:
        raise LookupError(f"系统中没有可用的{kind}设备")
    return devs[0]


def open_render_compatible(
    dev: DeviceInfo, want_rate: int, want_ch: int = 2, blocksize: int = 240
):
    """尽最大可能打开播放流；逐个降级尝试（声道数/采样率）。

    返回 (stream, rate, channels)。调用方需按实际 channels 做上混。
    """
    attempts = [
        (want_rate, want_ch),
        (dev.samplerate, want_ch),
        (want_rate, max(1, dev.channels)),
        (dev.samplerate, max(1, dev.channels)),
    ]
    seen = set()
    last_err = None
    for rate, ch in attempts:
        key = (rate, ch)
        if key in seen:
            continue
        seen.add(key)
        try:
            st = backend().open_render(dev, rate, ch, blocksize)
            return st, rate, ch
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(f"打不开播放设备「{dev.label}」：{last_err}")


def downsample_to_stereo(raw: bytes, channels: int) -> np.ndarray:
    """int16 交错原始数据 -> (n,2) float32。

    多声道按 ITU-R BS.775 下混，而不是简单平均：
        L' = L + 0.707*C + 0.707*Ls     R' = R + 0.707*C + 0.707*Rs
    这样额外声道静音时电平不会被摊薄（4ch 设备若用平均法会直接砍半），
    真正带中置/环绕的内容也不会丢音。
    """
    if channels <= 0:
        return np.zeros((0, 2), dtype=np.float32)
    a = np.frombuffer(raw, dtype="<i2")
    if a.size % channels:
        a = a[: a.size - a.size % channels]
    if a.size == 0:
        return np.zeros((0, 2), dtype=np.float32)
    m = a.reshape(-1, channels).astype(np.float32) / 32768.0

    if channels == 1:
        return np.repeat(m, 2, axis=1)
    if channels == 2:
        return m
    if channels == 3:                                   # L R C
        c = 0.707 * m[:, 2]
        out = np.stack([m[:, 0] + c, m[:, 1] + c], axis=1)
    elif channels == 4:                                 # L R BL BR
        out = np.stack([m[:, 0] + 0.707 * m[:, 2], m[:, 1] + 0.707 * m[:, 3]], axis=1)
    elif channels == 6:                                 # L R C LFE BL BR
        c = 0.707 * m[:, 2]
        out = np.stack(
            [m[:, 0] + c + 0.707 * m[:, 4], m[:, 1] + c + 0.707 * m[:, 5]], axis=1
        )
    else:                                               # 7.1：L R C LFE BL BR SL SR
        c = 0.707 * m[:, 2]
        sl = m[:, 6] if channels > 6 else np.zeros(m.shape[0], np.float32)
        sr = m[:, 7] if channels > 7 else np.zeros(m.shape[0], np.float32)
        out = np.stack(
            [m[:, 0] + c + 0.707 * (m[:, 4] + 0.707 * sl),
             m[:, 1] + c + 0.707 * (m[:, 5] + 0.707 * sr)],
            axis=1,
        )
    np.clip(out, -1.0, 1.0, out=out)
    return out


def _soft_clip(x: np.ndarray, thr: float = 0.99) -> np.ndarray:
    """软限幅：仅在超过 thr 时把信号光滑地弯进 [-1,1]，避免硬方波爆音。

    0..thr 区间保持线性（零额外染色），thr 以上用 tanh 光滑延伸，
    最高不超过 1.0、且在 thr 处一阶连续，听感上只是轻微软饱和而非切顶破音。
    """
    ax = np.abs(x)
    over = ax > thr
    if not over.any():
        return x
    mag = np.where(over, thr + (1.0 - thr) * np.tanh((ax - thr) / (1.0 - thr)), ax)
    return np.sign(x) * mag


def stereo_to_int16(frames: np.ndarray, channels: int, gain: float = 1.0) -> bytes:
    """(n,2) float32 -> 目标声道数 int16 交错字节。"""
    x = frames * float(gain)
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    if peak > 1.0:
        # 仅在确实超量程时软限幅，正常信号走直通，零额外染色
        x = _soft_clip(x)
    np.clip(x, -1.0, 1.0, out=x)
    i16 = (x * 32767.0).astype("<i2")
    if channels == 2:
        return i16.tobytes()
    if channels == 1:
        mono = ((x[:, 0] + x[:, 1]) * 0.5 * 32767.0).astype("<i2")
        return mono.tobytes()
    out = np.zeros((i16.shape[0], channels), dtype="<i2")
    out[:, 0] = i16[:, 0]
    out[:, 1] = i16[:, 1]
    return out.tobytes()
