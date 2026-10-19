"""发送端 / 接收端 / 局域网自动发现。"""

from __future__ import annotations

import json
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from . import protocol as P
from .devices import (
    CaptureStream,
    DeviceInfo,
    backend,
    downsample_to_stereo,
    open_render_compatible,
    stereo_to_int16,
)
from .jitter import JitterBuffer

Addr = Tuple[str, int]


def _now_ms() -> int:
    return int(time.monotonic() * 1000) & 0xFFFFFFFF


def _rms(x: np.ndarray) -> float:
    if x.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))


def parse_peer(text: str, default_port: int = P.PORT_DATA) -> Addr:
    text = (text or "").strip()
    if not text:
        raise ValueError("空地址")
    if text.count(":") == 1:
        host, port = text.split(":")
        return (socket.gethostbyname(host), int(port))
    return (socket.gethostbyname(text), default_port)


def local_broadcasts() -> List[str]:
    """尽力给出本机各网段的广播地址（无需第三方依赖）。"""
    out = {"255.255.255.255"}
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("127."):
                continue
            parts = ip.split(".")
            if len(parts) == 4:
                out.add(".".join(parts[:3] + ["255"]))
    except Exception:
        pass
    return sorted(out)


# =========================================================================== 音源
class Source:
    rate: int = P.DEFAULT_RATE
    label: str = ""

    def read(self) -> np.ndarray:  # (n,2) float32
        raise NotImplementedError

    def close(self) -> None:
        pass


class CaptureSource(Source):
    """声卡采集：Windows 上选 "[系统声音]" 设备即为整机声音回环。"""

    def __init__(self, dev: DeviceInfo) -> None:
        self.dev = dev
        self.rate = int(dev.samplerate)
        self.channels = max(1, int(dev.channels))
        self.frame_ms = P.FRAME_MS
        self.block = max(64, min(int(self.rate * P.CAPTURE_MS / 1000.0), 4800))
        self.label = f"{dev.label} @ {self.rate}Hz/{self.channels}ch"
        self.stream: CaptureStream = backend().open_capture(
            dev, self.rate, self.channels, self.block
        )

    def read(self) -> np.ndarray:
        raw = self.stream.read(self.block)
        return downsample_to_stereo(raw, self.channels)

    def close(self) -> None:
        try:
            self.stream.close()
        except Exception:
            pass


class FileSource(Source):
    """把本地音频文件当音源（wav 内置支持；mp3/flac 需装 soundfile）。"""

    def __init__(self, path: str, loop: bool = True) -> None:
        self.path = path
        self.loop = loop
        self.frame_ms = P.FRAME_MS
        self._data: Optional[np.ndarray] = None
        self._pos = 0
        self._t0 = 0.0
        self._played = 0.0
        self._load(path)
        self.block = max(64, min(int(self.rate * P.CAPTURE_MS / 1000.0), 4800))
        self.label = f"文件 {path} @ {self.rate}Hz"

    def _load(self, path: str) -> None:
        low = path.lower()
        sr = None
        data = None
        if low.endswith(".wav"):
            import wave

            with wave.open(path, "rb") as w:
                sr = w.getframerate()
                ch = w.getnchannels()
                sw = w.getsampwidth()
                raw = w.readframes(w.getnframes())
            if sw != 2:
                raise RuntimeError("仅支持 16bit wav，其它格式请安装 soundfile")
            data = downsample_to_stereo(raw, ch)
        else:
            try:
                import soundfile as sf
            except ImportError as e:
                raise RuntimeError(
                    f"读取 {path} 需要 soundfile：pip install soundfile"
                ) from e
            x, sr = sf.read(path, dtype="float32", always_2d=True)
            data = x[:, :2] if x.shape[1] >= 2 else np.repeat(x[:, :1], 2, axis=1)
        if data is None or len(data) == 0:
            raise RuntimeError(f"音频文件为空：{path}")
        self.rate = int(sr)
        self._data = data.astype(np.float32, copy=False)

    def read(self) -> np.ndarray:
        """按真实时间节奏产出（读到哪，就等到那一刻再返回）。

        这里必须是"节流"的：文件读得比实时快，若不过闸门，发送端会以几十倍速
        灌包，接收端缓冲区瞬间撑满并连续溢出。
        """
        assert self._data is not None
        n = self.block
        total = len(self._data)

        if self._pos >= total:
            if not self.loop:
                time.sleep(n / self.rate)          # 播完仍保持节奏，避免空转
                return np.zeros((n, 2), dtype=np.float32)
            self._pos = 0
            self._t0 = time.perf_counter() - 0.0

        now = time.perf_counter()
        if self._t0 == 0.0:
            self._t0 = now

        seg = self._data[self._pos: self._pos + n]
        self._pos += n
        if seg.shape[0] < n:
            pad = np.zeros((n - seg.shape[0], 2), dtype=np.float32)
            seg = np.vstack([seg, pad])

        # 闸门：这一块理论上应该在 _t0 + pos/rate 时刻被消费
        due = self._t0 + self._pos / self.rate
        dt = due - time.perf_counter()
        if dt > 0:
            time.sleep(dt)
        elif dt < -0.25:                            # 被系统挂起/卡顿过，重新对齐
            self._t0 = time.perf_counter() - self._pos / self.rate
        return seg

    def progress(self) -> float:
        if self._data is None or len(self._data) == 0:
            return 0.0
        return min(1.0, self._pos / len(self._data))


# =========================================================================== 发送端
@dataclass
class SenderStats:
    running: bool = False
    packets: int = 0
    packets_lost_by_peer: int = 0
    bytes_sent: int = 0
    rms: float = 0.0
    peak: float = 0.0
    rate: int = 0
    source: str = ""
    peers: Dict[str, dict] = field(default_factory=dict)
    error: str = ""
    started_at: float = 0.0


class Sender:
    def __init__(
        self,
        source: Source,
        peers: List[Addr],
        name: str = "",
        port: int = P.PORT_DATA,
        volume: float = 1.0,
    ) -> None:
        self.source = source
        self.name = name or socket.gethostname()
        self.port = port
        self.volume = float(volume)
        self._peers: Dict[str, Addr] = {}
        for a in peers:
            self.add_peer(a[0], a[1])
        self.stats = SenderStats(rate=source.rate, source=source.label)
        self._seq = 0
        self._pkt_frames = max(32, int(source.rate * P.FRAME_MS / 1000.0))
        self._carry = np.zeros((0, 2), dtype=np.float32)
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------- 对端管理
    def add_peer(self, host: str, port: Optional[int] = None) -> Addr:
        addr = (host, int(port or self.port))
        self._peers[f"{addr[0]}:{addr[1]}"] = addr
        return addr

    def remove_peer(self, key: str) -> None:
        self._peers.pop(key, None)

    @property
    def peers(self) -> List[Addr]:
        return list(self._peers.values())

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self._threads:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0x10)  # 低延迟
        except Exception:
            pass
        sock.bind(("0.0.0.0", 0))
        self._sock = sock
        self._stop.clear()
        self.stats.running = True
        self.stats.started_at = time.time()
        self.stats.error = ""
        self._threads = [
            threading.Thread(target=self._run, name="al-send", daemon=True),
            threading.Thread(target=self._recv_stats, name="al-send-stat", daemon=True),
        ]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=1.5)
        self._threads = []
        if self._sock:
            try:
                for a in self.peers:
                    self._sock.sendto(P.pack_json(P.T_BYE, {"name": self.name}), a)
            except Exception:
                pass
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        try:
            self.source.close()
        except Exception:
            pass
        self.stats.running = False

    # ------------------------------------------------------------------ 主循环
    def _hello(self) -> bytes:
        return P.pack_json(
            P.T_HELLO,
            {
                "name": self.name,
                "rate": self.source.rate,
                "channels": P.CHANNELS,
                "frame_ms": P.FRAME_MS,
                "app": "AudioLink",
                "ver": P.VERSION,
            },
            channels=P.CHANNELS,
        )

    def _run(self) -> None:
        assert self._sock is not None
        sock = self._sock
        last_hello = 0.0
        peak_decay = 0.0
        try:
            while not self._stop.is_set():
                if time.monotonic() - last_hello > 1.0:
                    hello = self._hello()
                    for addr in self.peers:
                        try:
                            sock.sendto(hello, addr)
                        except OSError:
                            pass
                    last_hello = time.monotonic()

                block = self.source.read()          # 阻塞式，天然节奏控制
                if block.size == 0:
                    continue
                if self._carry.shape[0]:
                    block = np.vstack([self._carry, block])
                if self.volume != 1.0:
                    block = block * self.volume

                # 大块读入 -> 切成 5ms 小包发出（每包 ≤ 1KB，不触发 IP 分片）
                n_pkt = self._pkt_frames
                i = 0
                total = block.shape[0]
                while total - i >= n_pkt:
                    seg = block[i: i + n_pkt]
                    i += n_pkt
                    self._seq += 1
                    payload = stereo_to_int16(seg, P.CHANNELS, 1.0)
                    pkt = P.pack(P.T_AUDIO, payload, seq=self._seq,
                                 ts_ms=_now_ms(), channels=P.CHANNELS)
                    for addr in self.peers:
                        try:
                            sock.sendto(pkt, addr)
                        except OSError:
                            pass
                    self.stats.packets += 1
                    self.stats.bytes_sent += len(pkt) * max(1, len(self._peers))
                self._carry = block[i:].copy() if i < total else np.zeros((0, 2), np.float32)

                lvl = _rms(block)
                peak_decay = max(lvl, peak_decay * 0.92)
                self.stats.rms = 0.7 * self.stats.rms + 0.3 * lvl
                self.stats.peak = peak_decay
        except Exception as e:  # noqa: BLE001
            self.stats.error = f"{type(e).__name__}: {e}"
            self.stats.running = False

    def _recv_stats(self) -> None:
        assert self._sock is not None
        sock = self._sock
        sock.settimeout(0.4)
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            f = P.unpack(data)
            if not f or f.mtype != P.T_STAT:
                continue
            info = f.json()
            info["last_seen"] = time.time()
            info["addr"] = f"{addr[0]}:{addr[1]}"
            with self._lock:
                self.stats.peers[info["addr"]] = info


# =========================================================================== 接收端
@dataclass
class ReceiverStats:
    running: bool = False
    bound_port: int = 0
    packets: int = 0
    lost: int = 0
    late: int = 0
    bytes_recv: int = 0
    rate: int = 0
    source_rate: int = 0
    sender: str = ""
    sender_addr: str = ""
    device: str = ""
    out_rate: int = 0
    out_channels: int = 0
    buffered_ms: float = 0.0
    target_ms: float = 0.0
    jitter_ms: float = 0.0
    underruns: int = 0
    concealed_ms: float = 0.0
    rms: float = 0.0
    error: str = ""
    waiting: bool = True


class Receiver:
    def __init__(
        self,
        sink: DeviceInfo,
        port: int = P.PORT_DATA,
        latency_ms: float = 120.0,
        volume: float = 1.0,
        name: str = "",
        open_to_all: bool = True,
        fade_ms: float = 4.0,
    ) -> None:
        self.sink = sink
        self.port = int(port)
        self.latency_ms = float(latency_ms)
        self.volume = float(volume)
        self.name = name or socket.gethostname()
        self.open_to_all = open_to_all
        self.fade_ms = float(fade_ms)

        self.stats = ReceiverStats(device=sink.label)
        self._jb: Optional[JitterBuffer] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._sock: Optional[socket.socket] = None
        self._out = None
        self._out_rate = 0
        self._out_ch = 0
        self._block = 240
        self._last_seq: Optional[int] = None
        self._last_payload_frames = 240
        self._sender_addr: Optional[Addr] = None
        self._last_transit: Optional[float] = None
        self._jitter = 0.0

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self._threads:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2 << 20)
        except Exception:
            pass
        sock.bind(("0.0.0.0", self.port))
        sock.settimeout(0.5)
        self._sock = sock
        self.stats.bound_port = sock.getsockname()[1]

        self._out_rate = int(self.sink.samplerate)
        self._block = max(64, int(self._out_rate * P.PLAY_MS / 1000.0))
        stream, rate, ch = open_render_compatible(
            self.sink, self._out_rate, P.CHANNELS, self._block
        )
        self._out = stream
        self._out_rate = rate
        self._out_ch = ch
        self._block = max(64, int(rate * P.PLAY_MS / 1000.0))
        self.stats.out_rate = rate
        self.stats.out_channels = ch
        self.stats.device = self.sink.label

        self._stop.clear()
        self.stats.running = True
        self.stats.error = ""
        self._threads = [
            threading.Thread(target=self._recv_loop, name="al-recv", daemon=True),
            threading.Thread(target=self._play_loop, name="al-play", daemon=True),
            threading.Thread(target=self._disco_loop, name="al-disco", daemon=True),
        ]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=1.5)
        self._threads = []
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        if self._out:
            try:
                self._out.close()
            except Exception:
                pass
            self._out = None
        self.stats.running = False
        self.stats.waiting = True

    def set_volume(self, v: float) -> None:
        self.volume = max(0.0, min(4.0, float(v)))

    def set_latency(self, ms: float) -> None:
        self.latency_ms = max(20.0, min(800.0, float(ms)))
        with self._lock:
            if self._jb is not None:
                self._jb = JitterBuffer(
                    int(self.stats.source_rate or P.DEFAULT_RATE),
                    self._out_rate,
                    self.latency_ms,
                    fade_ms=self.fade_ms,
                )
                self.stats.target_ms = self._jb.target_ms

    # ------------------------------------------------------------------ 收包
    def _recv_loop(self) -> None:
        assert self._sock is not None
        sock = self._sock
        last_stat = 0.0
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                self._maybe_stat(last_stat, None)
                last_stat = time.monotonic()
                continue
            except OSError:
                break
            f = P.unpack(data)
            if f is None:
                continue

            if f.mtype == P.T_HELLO:
                self._on_hello(f, addr)
            elif f.mtype == P.T_AUDIO:
                self._on_audio(f, addr)
            elif f.mtype == P.T_BYE:
                self.stats.sender = f.json().get("name", self.stats.sender)

            now = time.monotonic()
            if now - last_stat >= 1.0:
                self._maybe_stat(last_stat, addr)
                last_stat = now

    def _maybe_stat(self, last: float, addr: Optional[Addr]) -> None:
        if self._sock is None or self._sender_addr is None:
            return
        s = self.stats
        jb = self._jb
        st = jb.stats() if jb else None
        payload = {
            "name": self.name,
            "device": s.device,
            "pkts": s.packets,
            "lost": s.lost,
            "late": s.late,
            "underruns": s.underruns,
            "buffered_ms": round(st.buffered_ms, 1) if st else 0.0,
            "target_ms": round(st.target_ms, 1) if st else 0.0,
            "jitter_ms": round(self._jitter, 2),
            "out_rate": s.out_rate,
            "rms": round(s.rms, 4),
        }
        try:
            self._sock.sendto(P.pack_json(P.T_STAT, payload), self._sender_addr)
        except OSError:
            pass

    def _on_hello(self, f: P.Frame, addr: Addr) -> None:
        info = f.json()
        rate = int(info.get("rate") or P.DEFAULT_RATE)
        with self._lock:
            need_new = self._jb is None or int(self.stats.source_rate) != rate
            self.stats.sender = str(info.get("name", "?"))
            self.stats.sender_addr = f"{addr[0]}:{addr[1]}"
            self.stats.source_rate = rate
            self.stats.rate = rate
            self._sender_addr = addr
            if need_new:
                self._jb = JitterBuffer(
                    rate, self._out_rate, self.latency_ms, fade_ms=self.fade_ms
                )
                self.stats.target_ms = self._jb.target_ms
                self._last_seq = None
        if self.stats.waiting:
            self.stats.waiting = False

    def _on_audio(self, f: P.Frame, addr: Addr) -> None:
        with self._lock:
            jb = self._jb
            if jb is None:
                return
            self._sender_addr = addr
            ch = f.channels or P.CHANNELS
            frames = P.frames_in_payload(len(f.payload), ch)
            if frames <= 0:
                return
            if self._last_seq is not None:
                gap = (f.seq - self._last_seq) & 0xFFFFFFFF
                if gap == 0:
                    self.stats.late += 1
                    return
                if gap > 1:
                    missing = gap - 1
                    self.stats.lost += missing
                    if missing * frames <= int(jb.src * 0.4):
                        jb.conceal(missing * frames)
                    else:                       # 断流太久，直接重新预缓冲
                        jb.reset()
                        self._last_seq = None
            self._last_seq = f.seq
            self._last_payload_frames = frames
            jb.write(downsample_to_stereo(f.payload, ch))
            self.stats.packets += 1
            self.stats.bytes_recv += len(f.payload)

            # 抖动估计（RFC3550 简化版）
            transit = (time.monotonic() * 1000.0) - float(f.ts_ms)
            if self._last_transit is not None:
                d = abs(transit - self._last_transit)
                self._jitter += (d - self._jitter) / 16.0
            self._last_transit = transit

    # ------------------------------------------------------------------ 播放
    def _reopen_output(self) -> bool:
        """播放流异常时尝试重开设备（拔插耳机、驱动重置、休眠唤醒都会遇到）。"""
        try:
            if self._out:
                try:
                    self._out.close()
                except Exception:
                    pass
                self._out = None
            time.sleep(0.25)
            stream, rate, ch = open_render_compatible(
                self.sink, self._out_rate, P.CHANNELS, self._block
            )
            self._out = stream
            changed = rate != self._out_rate
            self._out_rate = rate
            self._out_ch = ch
            self._block = max(64, int(rate * P.PLAY_MS / 1000.0))
            self.stats.out_rate = rate
            self.stats.out_channels = ch
            if changed:                      # 采样率变了，缓冲需要按新速率重建
                with self._lock:
                    self._jb = None
                    self._last_seq = None
            return True
        except Exception as e:  # noqa: BLE001
            self.stats.error = f"重开播放设备失败：{e}"
            return False

    def _play_loop(self) -> None:
        errors = 0
        while not self._stop.is_set():
            out = self._out
            if out is None:
                if not self._reopen_output():
                    time.sleep(0.5)
                continue
            n = self._block
            with self._lock:
                jb = self._jb
            if jb is None:
                try:
                    out.write(b"\x00" * (n * self._out_ch * 2))
                except Exception:
                    errors += 1
                    if errors > 8 or not self._reopen_output():
                        break
                continue
            block, under = jb.read_block(n)
            if not under:
                self.stats.rms = 0.7 * self.stats.rms + 0.3 * _rms(block)
            try:
                out.write(stereo_to_int16(block, self._out_ch, self.volume))
                errors = 0
            except Exception as e:  # noqa: BLE001
                errors += 1
                self.stats.error = f"播放异常（{type(e).__name__}），正在重开设备…"
                if errors > 8 or not self._reopen_output():
                    self.stats.error = f"播放中断：{e}"
                    break
                continue
            st = jb.stats()
            self.stats.buffered_ms = st.buffered_ms
            self.stats.target_ms = st.target_ms
            self.stats.underruns = st.underruns
            self.stats.concealed_ms = st.concealed_frames * 1000.0 / jb.src

    # ------------------------------------------------------------------ 发现应答
    def _disco_loop(self) -> None:
        try:
            ds = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            ds.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            ds.bind(("0.0.0.0", P.PORT_DISCO))
            ds.settimeout(0.5)
        except OSError:
            return
        try:
            while not self._stop.is_set():
                try:
                    data, addr = ds.recvfrom(2048)
                except socket.timeout:
                    continue
                except OSError:
                    break
                f = P.unpack(data)
                if not f or f.mtype != P.T_DISCOVER:
                    continue
                reply = P.pack_json(
                    P.T_OFFER,
                    {
                        "name": self.name,
                        "host": socket.gethostname(),
                        "port": self.port,
                        "device": self.stats.device,
                        "ver": P.VERSION,
                    },
                )
                try:
                    ds.sendto(reply, addr)
                except OSError:
                    pass
        finally:
            try:
                ds.close()
            except Exception:
                pass


# =========================================================================== 发现
def _local_nets() -> set:
    """本机所在的 /24 网段，用于判断发现到的 IP 是否与自己同网段。"""
    nets = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("127."):
                continue
            nets.add(ip.rsplit(".", 1)[0])
    except Exception:
        pass
    return nets


def _ip_score(ip: str, nets: set) -> int:
    """给候选 IP 打分：同网段最高，虚拟网卡（WSL/Hyper-V/VirtualBox）降权。"""
    if ip.startswith("127."):
        return -20
    if ip.startswith("169.254."):
        return -15
    score = 0
    if ip.rsplit(".", 1)[0] in nets:
        score += 10
    if ip.startswith("192.168.56."):        # VirtualBox Host-Only
        score -= 8
    elif ip.startswith("192.168."):
        score += 3
    elif ip.startswith("10."):
        score += 2
    elif ip.startswith("172."):
        try:
            if 16 <= int(ip.split(".")[1]) <= 31:   # Hyper-V / WSL / docker
                score -= 6
        except (IndexError, ValueError):
            pass
    return score


def discover(timeout: float = 1.6, port: int = P.PORT_DISCO) -> List[dict]:
    """广播寻找局域网内正在监听的接收端。

    一台机器可能有多张网卡，会在多个网段各回一次；这里按
    (名称, 端口) 去重，并优先保留同网段的真实 LAN 地址。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", 0))
    s.settimeout(0.25)
    msg = P.pack_json(P.T_DISCOVER, {"q": "who", "ver": P.VERSION})
    for bc in local_broadcasts():
        try:
            s.sendto(msg, (bc, port))
        except OSError:
            pass
    nets = _local_nets()
    end = time.monotonic() + timeout
    found: Dict[str, dict] = {}
    try:
        while time.monotonic() < end:
            try:
                data, addr = s.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            f = P.unpack(data)
            if not f or f.mtype != P.T_OFFER:
                continue
            info = f.json()
            ip = addr[0]
            info["ip"] = ip
            info["addr"] = f"{ip}:{info.get('port', P.PORT_DATA)}"
            info["_score"] = _ip_score(ip, nets)
            key = f"{info.get('name')}|{info.get('port')}|{info.get('device')}"
            old = found.get(key)
            if old is None or info["_score"] > old["_score"]:
                found[key] = info
    finally:
        s.close()
    out = sorted(found.values(), key=lambda x: (-x["_score"], x["addr"]))
    for x in out:
        x.pop("_score", None)
    return out


def probe(ip: str, timeout: float = 0.8, port: int = P.PORT_DISCO) -> Optional[dict]:
    """单播探测某个 IP 是否为接收端。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    msg = P.pack_json(P.T_DISCOVER, {"q": "who", "ver": P.VERSION})
    try:
        s.sendto(msg, (ip, port))
        data, addr = s.recvfrom(2048)
        f = P.unpack(data)
        if f and f.mtype == P.T_OFFER:
            info = f.json()
            info["ip"] = addr[0]
            return info
    except OSError:
        return None
    finally:
        s.close()
    return None
