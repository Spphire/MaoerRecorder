# MaoerRecorder

录制猫耳 FM 直播（音频 + 弹幕）。**游客模式，无需登录。**

## 快速开始

### Windows 控制面板（推荐）

安装依赖后双击 `start_dashboard.bat`，或运行：

```bash
py dashboard.py
```

控制面板默认监听 `http://127.0.0.1:8765/`，并在 Windows 任务栏通知区域显示随系统深浅主题切换的猫耳图标。双击图标可重新打开面板；关闭网页不影响录制进程。每个房间使用独立进程、日志、状态和停止信号，可在面板中启动、停止、重启、强制结束、查看日志及打开录制目录。历史录制过的房间会自动出现在“常用房间”预设中，并显示主播名称、房间 ID 和场次数。

构建便携版 EXE：

```bat
build_exe.bat
```

构建脚本会创建隔离环境，并打包无头 Chromium、ffmpeg 和 ffprobe。完成后双击 `dist\MaoerRecorder\MaoerRecorder.exe` 即可启动，无需另装 Python。在仓库内直接运行该 EXE 时会复用项目根目录已有的 `recordings` 并接管面板任务；将整个 `dist\MaoerRecorder` 文件夹复制到其他位置后，录制默认保存在 EXE 同级的 `recordings`。使用 `onedir` 是为了避免每个录制进程重复解压大型浏览器运行时。

### 安装

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

需要本机有 `ffmpeg` 在 PATH，或通过 `FFMPEG_PATH` 指定。

Windows 推荐：
```bash
winget install --id=Gyan.FFmpeg -e
```

### 录制（推荐：守护模式）

**推荐使用 supervisor 守护进程**，自动处理崩溃/冻结：

```bash
supervisor.bat 868802213
```

或手动启动（无守护，进程崩溃后不会自动重启）：

```bash
python main.py record --room 868802213
```

查看实时状态：
```bash
status.bat          # Windows
./status.sh         # Linux/Mac
```

优雅停止（触发 finalize 合并）：
```bash
stop.bat            # Windows  
./stop.sh           # Linux/Mac
```

### 录制输出

录制文件落在 `./recordings/<room>_<creator>/<timestamp>/`：

- **`final.m4a`**：直播结束后自动合并去重生成的整段，采用 M4A 容器、AAC-LC、48 kHz 双声道，**时间轴已对齐**。源片段参数一致且校验通过时优先按 AAC 帧直通；不满足直通条件时，回退为重建完整 PCM 时间线后只做一次 AAC 编码
- **`chat.jsonl`**：弹幕流，按时间偏移 `t_audio` 对齐音频（秒数直接对应成品音频的播放位置）
- `meta.json`：元数据（含时长、来源拆分、中断归因 `gaps`）
- `audio_A_*.ts`, `audio_B_*.ts`：两路分段音频原始文件（A/B 热备）
- `segments.jsonl`：每段在时间轴上的起点，供对齐重建用

旧场次的 `final.mp3` 和 `final.ts` 仍可被控制面板及状态脚本识别。升级不会自动转换历史录音，原文件会保持不变，避免再做一次有损转码。

## 核心特性

### 1. 双路热备 + Cookie 隔离（默认开启）

**为什么需要双路？** 单 ffmpeg 任何重启（URL 失效、CDN 抖动、崩溃）都是音频中断。

**双路如何工作？**
- 两路 ffmpeg **同时拉流**、错开 8 秒启动
- 各自独立录制 `audio_A_*.ts` / `audio_B_*.ts`，各有 watchdog 与崩溃恢复
- 任一路崩溃/卡死时，另一路仍在录 → **覆盖不中断**
- `finalize` 按时间轴去重合并：**A 路优先，A 的缺口用 B 路填，两路都缺才补静音**

**Cookie 隔离防节流（关键）**：每路录制从「cookie 池」取一个**独立游客身份**（不同 `buvid3`/`MSESSID` + 各自签名 URL），控制路径（开播检测 + 弹幕）再单独占一个身份。这样服务端看到的是 N 个互不相关的观众，而不是「一个观众开 N 个流连接」——后者会被高人气房间节流。

**实测效果**（头部房间 47 万热度）：
- 共用 cookie：两路几乎每秒卡，合并后大量 silence
- **独立 cookie 池（当前方案）**：最终成品音频 silence **<1%**

**Cookie 烧毁自动轮换**：当某路连续空段（cookie 被平台临时封禁），自动从池中的 2 个备用身份换一个新鲜 cookie 继续录制，被烧毁的 cookie 充分冷却后重新进池。

资源代价：每路一个浏览器上下文（dual=5 个 context：控制 + 2 路 + 2 备用）。低配机器可 `MAOER_DUAL_RECORD=0` 回退单路。

### 2. 时间轴对齐（音频 ↔ 弹幕）

网络波动会导致音频丢失，但弹幕仍在记录。为保证对齐，`final.m4a` 在合并时会**在每个音频缺口处补入等长静音**：

- 每段录制启动时记录其在会话时间轴上的起点 `t_start`（与弹幕 `t_audio` 同一时钟）
- 合并时按 `t_start` 走时间轴，缺口（双路都没覆盖）> 0.5s 则插入等长静音
- 会话结束若最后一段之后仍有时间（末段崩溃丢失），补尾部静音到会话结束时刻

**结果**：`final.m4a` 的时间轴 == 墙钟时间轴 == 弹幕 `t_audio`。

任意一条弹幕的 `t_audio` 秒数，直接就是它在 `final.m4a` 里的播放位置。

### 3. Supervisor 守护进程（推荐）

**为什么需要 supervisor？** Windows 系统空闲时会自动睡眠，导致录制进程被冻结后静默失效（无 traceback、无日志）。

**守护进程如何工作？**
- 极简外层循环：不开浏览器、不轮询 API，**只看 `record.log` 的 mtime**
- 录制器每 5 分钟写一条心跳日志，supervisor 检测到心跳 **>6 分钟未更新**就判定冻死
- 自动 kill 整个进程树（recorder + ffmpeg + chromium）并重新启动
- 调用 `SetThreadExecutionState` 主动阻止系统睡眠

**三层防御**：
1. `powercfg` 永久禁用系统睡眠（治本）
2. supervisor `SetThreadExecutionState` 阻止睡眠（双保险）
3. 心跳监控（万一进程冻死，6 分钟内自动重拉）

**实测**（2026-06-30，14 小时无故障）：
- 录满两场直播（100 min + 273 min = 6h+ 直播内容）
- supervisor **零重启**（进程全程健康，未触发兜底）
- silence 占比 1~2.4%，全是源端抖动（CDN 同时 hiccup），非本地故障

### 4. 鲁棒性机制

- **快速恢复**：ffmpeg 崩溃 <1s 检测、stderr 报错（403/5xx/reset）实时检测、卡死 12s 检测
- **强化 reconnect**：网络错误 / 5xx 由 ffmpeg 自愈，不触发重启
- **HLS URL 续签**：录制中每 30s 预刷新签名 URL；某路空段崩溃时主动立即刷新
- **WebSocket 弹幕**：Playwright 嗅探 IM WS 帧；监督线程在页面崩溃 / WS 关闭 / 超长静默（120s）时自动重新打开页面
- **主循环防死锁**：每 5 分钟一条心跳 INFO 日志，外层 try/except 兜底捕获未处理异常

### 5. 中断可审计（gaps 归因）

每次录制的 `meta.json` 包含 `gaps` 字段，记录每段 silence 的**绝对时刻 + 时长**：

```json
{
  "audio_duration": 5968.83,
  "source_breakdown": {
    "primary_A": 5944.9,
    "backup_B": 14.7,
    "silence": 141.4
  },
  "gaps": [
    {"at": 5959.7, "dur": 141.4}
  ]
}
```

录完后对照 `record.log` 的两路 restart 时间戳，逐一确认"那一刻 A 和 B 确实都在重启/无数据"。

## 实测数据（头部房间，47 万热度）

| 场次 | 时长 | silence | 占比 | 备注 |
|---|---|---|---|---|
| 2026-06-27 23:55 | 258 min | 11s | **0.07%** | 双 HLS 热备 + 独立 cookie + 轮换 |
| 2026-06-30 01:40 | 100 min | 141s | 2.4% | 尾部 silence（主播下播前） |
| 2026-06-30 23:08 | **273 min** | 173s | **1.06%** | 4 个 gap，全是源端 hiccup |

残余的 <1% silence 来自**源端相关性故障**（CDN 同时对所有连接 hiccup，或主播推流端卡顿），两路即使用不同 cookie、不同协议也无法完全规避。这是该数据源的物理天花板。

## 配置

### 环境变量

| 变量 | 说明 | 默认 |
|---|---|---|
| `MAOER_ROOM_ID` | 直播间 ID | 868802213 |
| `MAOER_BASE_DIR` | 录制根目录 | `recordings` |
| `FFMPEG_PATH` | ffmpeg 可执行（缺省自动定位） | 自动 |
| `MAOER_DUAL_RECORD` | 双路热备（0 关闭） | 1 |
| `MAOER_SPARE_COOKIES` | 备用 cookie 数（池大小 = 控制 + 双路 + 备用） | 2 |
| `MAOER_HETERO_LANES` | 异构双路（A=HLS, B=FLV，实测 FLV 不稳定，默认关闭） | 0 |
| `MAOER_WORKER_B_DELAY` | B 路错开启动秒数 | 8 |
| `MAOER_MAX_NO_DATA` | ffmpeg 卡死判定（秒） | 12 |
| `MAOER_MAX_WS_SILENT` | WS 静默超时（秒） | 120 |
| `MAOER_IDLE_POLL` | 空闲检测开播间隔（秒） | 8 |
| `MAOER_ACTIVE_POLL` | 录制中刷新 URL 间隔（秒） | 30 |
| `MAOER_LOG_LEVEL` | 日志级别 | INFO |

## 架构

- **游客模式**：HTTP API、HLS 流、WebSocket 弹幕都不需要登录。Playwright 开临时无头浏览器，页面加载后自动获得必需的临时 cookie，录制全程无需人工介入。
- **无状态**：不保存 profile，每次启动都是干净的临时 context。
- **Cookie 池**：启动时预热 N 个独立游客身份（每个都是完整的浏览器 context），录制时按需分配，用完归还，烧毁后轮换。
- **时间对齐**：所有时间戳（弹幕 `t_audio`、段 `t_start`、session 墙钟）共享同一 `time.time()` 时钟，合并时按时间轴走，缺口补 silence 保证对齐。

## 故障排查

### 录制器静默死亡（无日志）

**症状**：`record.log` 某个时刻后完全无新行，进程 PID 已消失或僵死。  
**原因**：系统睡眠冻结进程。  
**解决**：
1. 使用 `supervisor.bat` 启动（自动监控并重启）
2. 确认系统睡眠已禁用：
   ```cmd
   powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE
   REM 输出应为 0x00000000（从不睡眠）
   ```

### 两路频繁 restart、silence 很多

**症状**：`record.log` 里每秒都有 `[A] restart` / `[B] restart`，最终成品音频 silence 占比 >10%。

**原因**：Cookie 被平台节流（共用 cookie 或池太小）。  
**解决**：
- 确认 `cookie pool: 5 independent guest identities ready`（默认配置）
- 若仍频繁，增加备用数：`set MAOER_SPARE_COOKIES=3`

### 成品音频时长远小于实际直播时长

**症状**：主播播了 2 小时，`final.m4a` 只有 30 分钟（旧场次可能是 `final.mp3`）。

**原因**：录制中途崩溃，未触发 finalize；或 supervisor 重启后创建了新 session。  
**排查**：
- 查 `recordings/<room>_<creator>/` 下所有 `202*` 目录，每个是一场 session
- 若有多个且都有 `final.m4a`（或旧版的 `final.mp3`），说明中途重启过（多段分开了），需手动拼接
- 若某个目录只有 `.ts` 没有 `final.m4a` / `final.mp3`，说明该场未正常结束；段文件仍在，可用 ffmpeg 手动合并（见"工具脚本 → 手动合并"）

### 弹幕与音频不对齐

**症状**：弹幕 `t_audio=120` 的内容在成品音频的 150 秒处出现。

**原因**：`meta.json` 里 `timeline_aligned: false`（旧版录制或手动合并错误）。  
**确认**：当前版本所有 session 的 `meta.json` 都应有 `"timeline_aligned": true`。若为 `false` 或缺失该字段，说明是旧版遗留。

## 工具脚本

```bash
# 分析两路覆盖率：精确统计"两路同时空窗"的时刻与时长（需先录制生成 session）
python analyze_coverage.py recordings/<room>_<creator>/<timestamp>/

# 网络健康探针：区分本地 vs 远端网络波动（录制期间并行运行）
python net_probe.py <cdn_host>
```

### 手动合并 .ts 段

若某场 session 只剩 `.ts` 段未合并（如中途崩溃未 finalize），可直接用 ffmpeg 按顺序拼接（仅救急，不含时间轴对齐 / 双路去重）：

```bash
cd recordings/<room>_<creator>/<timestamp>/
for f in audio_A_*.ts; do echo "file '$f'"; done > concat.txt
ffmpeg -f concat -safe 0 -i concat.txt -map 0:a:0 -vn -c:a aac -profile:a aac_low -b:a 128k -ar 48000 -ac 2 -movflags +faststart recovered.m4a
```

完整的双路去重 + 时间轴对齐合并由 `maoer/recorder.py` 的 `finalize()` 完成，它接受
运行时的 `RecordSession` 对象（不是路径），正常由主循环在直播结束时自动调用，无需手动干预。
