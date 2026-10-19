"""AudioLink 线协议。

设计目标：极简、无依赖、足够低延迟。
  - UDP 单播，每包一个 5ms 音频块（48kHz 立体声 16bit = 960 字节，远小于 MTU，不触发分片）
  - 18 字节定长头，大端序
  - 控制帧（HELLO/STAT/DISCOVER）载荷为 UTF-8 JSON

帧头布局：
    0..3   magic  'ALNK'
    4      version
    5      type
    6      channels
    7      flags
    8..11  seq        (uint32, 音频帧序号，从 1 开始)
    12..15 ts_ms      (uint32, 发送端 monotonics 毫秒，仅用于抖动估计)
    16..17 plen       (uint16, 载荷字节数)
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Optional

MAGIC = b"ALNK"
VERSION = 1

PORT_DATA = 45800          # 音频数据面
PORT_DISCO = 45801         # 设备发现面（广播）

DEFAULT_RATE = 48000
CHANNELS = 2
SAMPLE_BYTES = 2           # int16
FRAME_MS = 5               # 每个 UDP 包的音频时长
CAPTURE_MS = 20            # 采集一次读多久的音频（再切成 5ms 包发出去）
                           # 5ms 一读时 Python 侧开销会把有效吞吐打到 96%，
                           # 20ms 一读吞吐回到 99.9%，代价是 +15ms 采集延迟
PLAY_MS = 10               # 接收端一次写多久（10ms 给 Python 留出余量，不易爆音）

MAX_PAYLOAD = 1400

T_AUDIO = 1
T_HELLO = 2
T_STAT = 3
T_BYE = 4
T_DISCOVER = 5
T_OFFER = 6

TYPE_NAMES = {
    T_AUDIO: "audio",
    T_HELLO: "hello",
    T_STAT: "stat",
    T_BYE: "bye",
    T_DISCOVER: "discover",
    T_OFFER: "offer",
}

_HDR = struct.Struct("!4sBBBBIIH")
HEADER_SIZE = _HDR.size  # 18


@dataclass
class Frame:
    mtype: int
    seq: int
    ts_ms: int
    channels: int
    flags: int
    payload: bytes

    @property
    def type_name(self) -> str:
        return TYPE_NAMES.get(self.mtype, "?")

    def json(self) -> dict:
        if not self.payload:
            return {}
        try:
            return json.loads(self.payload.decode("utf-8", "replace"))
        except Exception:
            return {}


def pack(
    mtype: int,
    payload: bytes = b"",
    seq: int = 0,
    ts_ms: int = 0,
    channels: int = 0,
    flags: int = 0,
) -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"payload too large: {len(payload)} > {MAX_PAYLOAD}")
    head = _HDR.pack(
        MAGIC,
        VERSION,
        mtype & 0xFF,
        channels & 0xFF,
        flags & 0xFF,
        seq & 0xFFFFFFFF,
        ts_ms & 0xFFFFFFFF,
        len(payload),
    )
    return head + payload


def pack_json(mtype: int, obj: dict, **kw) -> bytes:
    return pack(mtype, json.dumps(obj, ensure_ascii=False).encode("utf-8"), **kw)


def unpack(data: bytes) -> Optional[Frame]:
    if len(data) < HEADER_SIZE:
        return None
    magic, ver, mtype, ch, flags, seq, ts_ms, plen = _HDR.unpack_from(data, 0)
    if magic != MAGIC or ver != VERSION:
        return None
    if len(data) < HEADER_SIZE + plen:
        return None
    return Frame(mtype, seq, ts_ms, ch, flags, data[HEADER_SIZE:HEADER_SIZE + plen])


def frames_in_payload(plen: int, channels: int = CHANNELS) -> int:
    """从载荷长度推算音频帧数。"""
    if channels <= 0:
        return 0
    return plen // (channels * SAMPLE_BYTES)


def block_frames(rate: int, frame_ms: int = FRAME_MS) -> int:
    return max(32, int(rate * frame_ms / 1000))
