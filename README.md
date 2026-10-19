# AudioLink / 音链 v0.1

局域网内跨系统的**实时音频共享**：把 A 机正在播放的声音，低延迟推送到 B 机（或反过来），
B 机把它当成本机的一块声卡来用——包括 **Voicemeeter / VB-Audio 虚拟输入**，
于是远端声音可以和你本机声音一起混音、一起从同一对音箱出来。

```
发送端 A（另一台 Windows）                      接收端 B（你这台，有 Voicemeeter）
┌──────────────────────┐                    ┌──────────────────────────────┐
│ 音乐/视频/游戏 声音    │                    │ Voicemeeter Input (Virtual)  │
│        ↓ WASAPI 回环  │                    │   ↓                          │
│  [系统声音] 采集       │   5ms 音频包        │ 再和本机 Stereo Input 混音    │
│        ↓ PCM 16bit    │ ─── UDP 45800 ───▶ │   ↓                          │
│  抖动/漂移 无           │                    │ Hardware Out A1: JBL PS3300  │
└──────────────────────┘                    │ Hardware Out A2: Realtek     │
                                             └──────────────────────────────┘
```

## 为什么不用现成方案

| 方案 | 问题 |
|---|---|
| **VBAN**（Voicemeeter 自带） | 只服务 Windows + Voicemeeter 生态；Android/Linux 上要么没有发送端，要么要装另一套 |
| AirPlay / DLNA | 延迟 1~2 秒，做不了"看视频声音同步"；还要 Bonjour 依赖 |
| SoundWire / AudioRelay 等 | 闭源，不能接虚拟声卡做混音路由，不能改协议 |

这个项目要的是：**任意系统当发送端、任意系统当接收端、接收端能落到任意一块（含虚拟）播放设备**，
自己掌握协议，后面能加加密、加密钥、加多路混音。

## 快速开始（两台 Windows）

两台机器都解压/拷贝本目录，双击 `panel.bat` 即可（**首次运行会自动建 venv 并安装依赖**，约 1 分钟，
无需先跑 setup；`setup.bat` 仍可单独执行）。

**B 机（接收端，也就是有 Voicemeeter 的那台）**
1. 双击 `panel.bat`
2. 角色选「接收端」，设备选 **`Voicemeeter Input (VB-Audio Voicemeeter VAIO)`**
3. 点「开始」——状态灯变绿，显示"等待发送端"

**A 机（发送端）**
1. 双击 `panel.bat send`
2. 角色选「发送端」，音源类型「系统声音」，采集设备保持"自动"
   （自动 = 你当前默认播放设备对应的回环，也就是"你现在听到的一切"）
3. 点「扫描局域网接收端」→ 点出现的绿色标签自动填入 IP；或手动填 `192.168.50.111`
4. 点「开始」

A 机放的歌就会从 B 机的 Voicemeeter 混音总线出来。**A 机本地仍然正常外放**（回环只是"旁听"，
不会把声音截走）。

> 若 B 机不是 Voicemeeter 方案，直接把设备选成真实音箱即可，声音就只从那个设备出来。

## 命令行（无界面 / 无头 / Linux）

```bash
python -m audiolink devices                 # 列出所有设备（[系统声音] = 可抓整机输出）
python -m audiolink discover                # 广播找接收端
python -m audiolink recv                    # 接收端，默认设备 = 系统默认输出
python -m audiolink recv -d "Voicemeeter Input" --latency 120
python -m audiolink send -p 192.168.50.111  # 发送端，自动挑默认回环
python -m audiolink send -p 192.168.50.111 -d "扬声器 (JBL PS3300)"   # 指定抓哪块声卡
python -m audiolink send -f D:\Music\demo.wav -p 192.168.50.111       # 推文件
python -m audiolink ui                      # 网页控制台（默认 http://127.0.0.1:8770）
```

也可以直接用 `send.bat 192.168.50.111` / `recv.bat "Voicemeeter Input"`。

### 多路汇流（两台以上一起混）

Voicemeeter 有多个虚拟输入（`Voicemeeter Input`、`Voicemeeter Aux Input`、`Voicemeeter VAIO3 Input`）。
B 机上开多个接收端进程，各自落到不同的虚拟输入，再在 Voicemeeter 里一起混到 A1/A2：

```bash
python -m audiolink recv -d "Voicemeeter Input"     --port 45800
python -m audiolink recv -d "Voicemeeter Aux Input" --port 45810
```

发送端对应填 `IP:45810` 即可。

## 跨平台

| 系统 | 发送端（抓声音） | 接收端（放声音） | 说明 |
|---|---|---|---|
| Windows | ✅ WASAPI 回环，抓任意播放设备 | ✅ 任意播放设备 | 主战场，已实测 |
| Linux | ✅ PipeWire/PulseAudio 的 `Monitor of ...` 源（列表里标 [系统声音]） | ✅ 任意 sink | 也可用 `parec` 调试 |
| macOS | ⚠ 需先装 BlackHole 作为输入源 | ✅ CoreAudio 设备 | 未实测 |
| Android | ⏳ 计划 v2（MediaProjection 内录 + 同一个 UDP 协议） | ⏳ v2 | 协议已稳定，可直接写原生壳 |

## 音质处理：无压缩 + 高质量重采样 + 防爆音

本方案**全程不压缩**——抓取的是 16bit PCM，切成 UDP 小包原样发出，接收端直接重放，
不经过任何 MP3/Opus 这类有损编码（和 WAV 同一档音质）。"音质怪 / 爆音"来自另外两处，
均已修复并通过量化测试。

### 1. 重采样：线性插值 → Catmull-Rom 三次插值

接收端需要把"发送端采样率"对齐到"接收端声卡采样率"。旧版用相邻两点的线性插值，
本质是个劣质低通滤波器——当两端采样率不一致时（例如 A 机系统声音回环是 44.1kHz、
B 机 Voicemeeter 输入是 48kHz），顶频段会被削掉约 23.6% 的能量，听感发闷、像差音质的 MP3。
改为三次插值后（`tools/resample_quality.py` 实测）：

| 场景 | 线性插值 | 三次插值 |
|---|---|---|
| 同采样率（两端都 48k） | 误差 0% | 误差 0%（完全透明） |
| 44.1k→48k 顶频段(10k–16k) | 丢 23.6% 能量 | 丢 10.5% 能量 |

> 最彻底的零染色做法：把两台机器相关设备的**系统采样率都设成 48kHz**（Windows 声音设置→高级），
> 此时重采样比 step≈1.0、几乎不做任何重采样，音质彻底透明。

### 2. 防爆音：增益包络 + 跨块斜坡 + 软限幅

爆音根因是"有声↔静音"的**硬切**：缓冲欠载时剩一点声音后面直接补零、丢包时插整段静音、
启动时第一帧直接非零。修复：

- **增益包络（淡入/淡出，默认 4ms）**：任何有声→静音切换都走线性斜坡，绝不硬切。
  一个 `_muted` 状态机覆盖"启动 / 欠载 / 恢复"三种爆音场景。
- **丢包静音段边界斜坡**：`conceal` 写入时对缓冲打标记，读出时对静音段前后 4ms 做淡出/淡入；
  采样窗口向前后各扩展 4ms，避免丢包段落在读块边界时斜坡被裁掉仍爆音。
- **软限幅**：超量程时用 `tanh` 光滑弯进 ±1，不再切硬方波。

量化验证（`tools/pop_test.py`，模拟启动 / 200ms 欠载 / 周期丢包）：

```
旧版（硬切）: 爆音跳变 1 次，最大样本跳变 0.171
修复后（4ms 淡入淡出）: 爆音跳变 0 次，最大样本跳变 0.083（正常正弦水平）
```

新增测试：`tools/resample_quality.py`（重采样质量对照）、`tools/pop_test.py`（爆音专项）。
可用 `python -m audiolink recv --fade 0` 对比旧行为。

## 参数与调优

| 参数 | 默认 | 说明 |
|---|---|---|
| `--latency` | 120 ms | 接收端目标缓冲。局域网有线 60~80 就够；Wi-Fi 或有卡顿调到 200~300 |
| `--volume` | 1.0 | 两端都有，0~4 倍（软件增益，超过 1.0 会削顶） |
| 采样率 | 跟随采集设备（一般 48000） | 两端不同源也无需手动设，接收端会重采样 |
| 带宽 | 约 1.54 Mbps/路 | 48kHz × 2ch × 16bit 未压缩。千兆/AC Wi-Fi 无压力 |

延迟构成：采集 20ms + 网络 ~1ms + 缓冲 120ms + 播放 10ms ≈ **150ms**（可调到 ~80ms）。

## 防火墙

首次运行 Windows 会弹窗询问，**勾选"专用网络"并允许**。
如果错过了：`控制面板 → Windows Defender 防火墙 → 允许应用通过防火墙 → 找到 Python` 全勾上；
或者直接放行端口：

```powershell
netsh advfirewall firewall add rule name="AudioLink" dir=in action=allow protocol=UDP localport=45800-45801 profile=private
```

## 常见问题

**Q：连不上，接收端一直"等待发送端"**
A：按顺序查：① 两台在同一网段？② B 机防火墙放行 UDP 45800/45801 了吗？
③ `python -m audiolink discover --ip <B机IP>` 有回应吗？④ VPN/虚拟网卡（WSL、VirtualBox、Hyper-V）
可能抢走路由——发现结果已经做了"同网段优先 + 虚拟网卡降权"，但仍建议手动填真实 LAN IP。

**Q：有声音但很小**
A：检查**发送端那块设备的 Windows 音量**——回环采集是"端点音量之后"的信号，
B 机抓到的是 A 机衰减后的电平（实测某端点音量 20% 时电平只有 -14dB）。
把 A 机对应设备音量拉满，或用 `--volume 2` 补增益。

**Q：有噼啪/爆音**
A：v0.1 已内置 4ms 增益包络（淡入/淡出）+ 丢包边界斜坡 + 软限幅，能盖住"有声↔静音"硬切爆音。
若仍有：① 调大缓冲（`--latency 200`，更深的抖动缓冲抗网络抖动）；② 换有线；
③ 关掉 A 机的"音频增强/空间音效"；④ 别把发送端和接收端选成同一块设备（会形成反馈环）；
⑤ 大面积丢包时 4ms 只能盖住零星丢包，仍会短暂没声但不爆音——此时应查网络或加大 `--latency`。

**Q：Voicemeeter 里听不到**
A：接收端只是往 `Voicemeeter Input` 这条 Strip 灌声音，还要确认该 Strip 的 **A1/A2 按钮亮着**，
且 `Menu → System Settings` 里 A1 设备选对了。

**Q：一台机器想同时当发送端和接收端**
A：开两个进程（两个端口），目前一个进程只承担一个角色。

## 实测数据（本机自测）

```
漂移仿真（源时钟快 3000 ppm）：缓冲稳定 115~119ms（目标 120），0 欠载 0 溢出
真实链路（Voicemeeter 回环 → UDP → 虚拟耳机）：1040 包 / 丢 0 / 欠载 0 / 缓冲 ~125ms
网页控制台 + 双进程：200 包/秒，缓冲 127~133ms，发现结果正确去重为 1 个
```

## 设计取舍

- **UDP 而非 TCP**：实时音频宁可丢一小段也不要等重传。5ms/包（960 字节）远小于 MTU，不触发 IP 分片。
- **PCM 16bit 而非 Opus**：v0.1 用带宽换零编码延迟和零依赖（1.5Mbps 在局域网不算事）。
  Opus 留到 v0.2，用于 Wi-Fi/远程场景。
- **抖动缓冲 + PI 控制**：两端声卡晶振不同源，纯比例控制会留下稳态水位偏差
  （实测某虚拟声卡投喂速率只有名义的 99.5%，纯 P 会把 120ms 压到 50ms）。
  加积分项后水位真正回到目标值，代价是长期以 0.5% 的重采样比运行——这是网络音频的固有代价。
- **丢包补偿**：序号跳变时补等长静音，保证时间轴不被压缩；超过 400ms 的断流直接重新预缓冲。

## 目录

```
audiolink/
├── audiolink/
│   ├── protocol.py   线协议（18 字节头 + 5ms 音频包）
│   ├── jitter.py     抖动缓冲 / 时钟漂移 PI 补偿 / 丢包补偿 / 防爆音淡入淡出
│   ├── devices.py    声卡后端（Windows=PyAudioWPatch 回环，其它=sounddevice）/ 软限幅
│   ├── engine.py     发送端 / 接收端 / 局域网发现
│   ├── webui.py      网页控制台（http.server + SSE）
│   ├── cli.py        命令行
│   └── static/index.html
├── tools/
│   ├── selftest.py        sim 漂移仿真 / live 端到端链路
│   ├── ui_smoke.py        控制台 + 引擎集成测试
│   ├── resample_quality.py 重采样质量对照（线性 vs 三次插值）
│   └── pop_test.py         爆音专项（启动/欠载/丢包跳变量化）
├── setup.bat / panel.bat / send.bat / recv.bat
└── requirements.txt
```

## 路线图

- v0.2：Opus 编码（带宽降到 ~128kbps，适配 Wi-Fi）、接收端多发送端混音、开机自启 + 托盘
- v0.3：AES-GCM 加密 + 配对码（现在局域网内是明文，同一网段任何人可抓包）
- v0.4：Android 原生 App（MediaProjection 内录 / AudioTrack 播放，复用同一协议）、PyInstaller 单文件 exe

## 实验版本记录

本仓库目前是**实验版本（v0.1）**，核心链路已在本机实测跑通。相对最初原型，已修复并验证以下问题：

1. **音质发闷**：重采样从线性插值改为 Catmull-Rom 三次插值（两端采样率不一致时顶频损耗 23.6%→10.5%）。
2. **爆音/喀嗒**：新增 4ms 增益包络（淡入/淡出）+ 丢包边界跨块斜坡 + 超量程软限幅，爆音跳变 1→0（见 `tools/pop_test.py`）。
3. **两端时钟漂移**：抖动缓冲用 PI 控制（比例 + 积分），把缓冲水位真正拉回目标值（纯比例会稳态漂到 50ms）。
4. **部署踩坑（Windows 批处理）**：
   - `panel.bat` 改为**首次运行自动建 venv + 安装依赖**，双击即用，不再强依赖先跑 `setup.bat`；
   - 批处理改为纯 ASCII + `PYTHONUTF8=1`，绕开中文 UTF-8 无 BOM 在 GBK 终端被读坏的编码坑；
   - 修掉括号块内 `echo` 带圆括号导致的 `... was unexpected at this time.` 解析崩溃（块内嵌套改为 `goto` 跳转）。
5. **局域网发现**：同网段优先 + 虚拟网卡降权 + 去重（本机多网卡/WSL/Hyper-V 不再重复回报）。

> 仍是明文 PCM，同网段内任何人可抓包；加密与 Opus 压缩见路线图 v0.2/v0.3。
