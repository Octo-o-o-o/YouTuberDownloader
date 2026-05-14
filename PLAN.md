# YouTube 博主数据采集工具 — 最终方案

> 综合 Claude 方案 + ChatGPT Pro 方案 + 你的最新要求（4 语种独立字幕、Web 页面看历史、JSONL 存储、无认证）

---

## 0. 目标 & 边界

**目标**：输入一个 YouTube 博主链接，自动产出一个"测试数据包"：
1. 最新 5 条 + 播放最多 5 条视频（按 `video_id` 排重）
2. 3-5 张博主本人照片/封面（含频道头像/横幅 + 视频缩略图人脸筛选）
3. 每个视频按语言生成 **4 份独立的 md 字幕**：`<title>.en.md` / `<title>.zh.md` / `<title>.ja.md` / `<title>.ko.md`
4. 每个视频提取音频；> 10MiB 时按自然分隔切分，每段 ≤ 10MiB 且尽可能长

**边界**：仅供你自己合规测试用，不绕过认证/会员/地区/年龄限制。

---

## 1. 技术选型

| 模块 | 选型 | 理由 |
|------|------|------|
| 后端语言 | **Python 3.10+** | yt-dlp / InsightFace / faster-whisper / ffmpeg-python 全是 Python 生态 |
| Web 框架 | **FastAPI + uvicorn** | 轻量、异步、自动 OpenAPI、对 SSE/流式响应友好 |
| 任务执行 | **后台线程池**（`concurrent.futures.ThreadPoolExecutor`） | 单用户场景不需要 Celery/Redis |
| 数据存储 | **JSONL + JSON 文件**（无数据库） | `data/jobs.jsonl` 是历史索引；每个 job 单独 `manifest.json` |
| 视频下载 | **yt-dlp** | 事实标准 |
| 元数据/排序 | **yt-dlp `--flat-playlist`**，可选 **YouTube Data API v3**（设 `YOUTUBE_API_KEY` 启用 full_scan 精确模式） | 默认零配置可用；进阶用户配 API key 获得精确 Top 5 |
| 音频处理 | **ffmpeg**（系统二进制） | `silencedetect` + 切片 |
| 字幕解析 | **webvtt-py** + 自写 | 把 VTT 转 markdown |
| 字幕兜底（可选） | **faster-whisper** | 无字幕时本地转录 |
| 人脸检测/识别 | **InsightFace**（buffalo_l 模型） | 2026 年最快最准，ArcFace 99.4% LFW |
| 前端 | **单页 HTML + Tailwind CDN + Alpine.js** | 零构建、即开即用、清爽干净 |

**为什么不用 React/Next.js**：个人工具、单页面、不需要构建步骤；用 Tailwind CDN + Alpine.js 直接 FastAPI 静态托管即可，部署只需 `uvicorn main:app`。

---

## 2. 项目结构

```
YouTubeDownload/
├── PLAN.md                       # 本文件
├── README.md                     # 使用说明
├── requirements.txt
├── .env.example                  # YOUTUBE_API_KEY（可选）
├── run.sh                        # 一键启动
├── backend/
│   ├── main.py                   # FastAPI 入口 + 路由
│   ├── jobs.py                   # 后台任务调度 + 进度跟踪
│   ├── storage.py                # JSONL 读写
│   ├── collector/
│   │   ├── __init__.py
│   │   ├── channel.py            # 解析 channel URL → channel_id + 元信息
│   │   ├── selector.py           # 选最新 5 + 播放最多 5 + 去重
│   │   ├── downloader.py         # 下载视频 + info.json + 缩略图
│   │   ├── subtitles.py          # 拉字幕 + VTT → 4 份 md
│   │   ├── audio.py              # 提取音频 + 智能切分
│   │   ├── images.py             # 头像/横幅 + 缩略图人脸筛选
│   │   ├── face.py               # InsightFace 封装
│   │   └── utils.py              # 文件名清洗、日志、路径工具
│   └── models.py                 # Pydantic 数据模型
├── frontend/
│   ├── index.html                # 单页应用
│   └── assets/
│       ├── app.js
│       └── styles.css            # 自定义补充样式
└── data/
    ├── jobs.jsonl                # 所有 job 索引（追加写）
    └── jobs/
        └── <job_id>/             # 每个 job 一个目录
            ├── manifest.json     # 完整元数据
            ├── status.json       # 实时状态（运行时频繁更新）
            ├── log.txt           # 日志
            └── output/
                ├── videos/
                │   └── <title> [vid].mp4
                ├── thumbnails/
                │   └── <title> [vid].jpg
                ├── transcripts/
                │   ├── <title> [vid].en.md
                │   ├── <title> [vid].zh.md
                │   ├── <title> [vid].ja.md
                │   ├── <title> [vid].ko.md
                │   └── raw/                  # 原始 VTT 全部保留
                │       └── <title> [vid].xx.vtt
                ├── audio/
                │   ├── full/
                │   │   └── <title> [vid].m4a
                │   └── segments/
                │       └── <title> [vid].part01.m4a
                └── creator_images/
                    ├── 01_avatar.jpg
                    ├── 02_banner.jpg
                    ├── 03_thumb_score0.83.jpg
                    └── index.json
```

---

## 3. 数据模型

### `data/jobs.jsonl`（每行一个 job 摘要）
```json
{"job_id":"job_20260514_153022_mkbhd","channel_url":"https://...","channel_title":"MKBHD","created_at":"2026-05-14T15:30:22Z","status":"completed","video_count":10,"image_count":5,"duration_sec":423}
```

### `data/jobs/<job_id>/manifest.json`（详细 job 状态）
```json
{
  "job_id": "job_20260514_153022_mkbhd",
  "channel": {
    "url": "https://www.youtube.com/@mkbhd",
    "channel_id": "UCBJycsmduvYEL83R_U4JriQ",
    "title": "Marques Brownlee",
    "avatar_url": "...",
    "banner_url": "..."
  },
  "config": {
    "latest_count": 5,
    "popular_count": 5,
    "max_audio_mb": 10,
    "audio_bitrate": "64k",
    "languages": ["en", "zh", "ja", "ko"]
  },
  "videos": [
    {
      "video_id": "abc123",
      "title": "Video Title",
      "title_safe": "Video Title [abc123]",
      "url": "https://www.youtube.com/watch?v=abc123",
      "source": ["latest", "popular"],
      "view_count": 1234567,
      "published_at": "2026-05-01T00:00:00Z",
      "duration_sec": 845,
      "files": {
        "video": "output/videos/Video Title [abc123].mp4",
        "thumbnail": "output/thumbnails/Video Title [abc123].jpg",
        "info_json": "output/videos/Video Title [abc123].info.json",
        "transcripts": {
          "en": {"path": "output/transcripts/Video Title [abc123].en.md", "source": "manual"},
          "zh": {"path": "output/transcripts/Video Title [abc123].zh.md", "source": "auto"},
          "ja": null,
          "ko": null
        },
        "audio_full": "output/audio/full/Video Title [abc123].m4a",
        "audio_full_size_mib": 8.3,
        "audio_segments": []
      }
    }
  ],
  "creator_images": [
    {"file": "output/creator_images/01_avatar.jpg", "source": "channel_avatar", "score": null},
    {"file": "output/creator_images/02_banner.jpg", "source": "channel_banner", "score": null},
    {"file": "output/creator_images/03_thumb_score0.83.jpg", "source": "video_thumbnail", "from_video": "abc123", "score": 0.83}
  ],
  "face_reference": {
    "embedding_file": "creator_images/reference_face.npy",
    "source": "channel_avatar",
    "confirmed_by_user": false
  },
  "created_at": "...",
  "completed_at": "...",
  "errors": []
}
```

### `data/jobs/<job_id>/status.json`（运行中频繁更新，前端轮询）
```json
{
  "job_id": "...",
  "status": "running",
  "stage": "subtitles",
  "stage_progress": {"current": 3, "total": 10},
  "overall_progress": 0.45,
  "current_action": "Downloading subtitles for video abc123 (ko)",
  "updated_at": "..."
}
```

---

## 4. 详细流程

### 阶段 1：解析频道
**输入**：`https://www.youtube.com/@xxx` / `/channel/UC...` / `/c/xxx` / `/user/xxx` / 任意视频 URL

**处理**：
- 默认走 yt-dlp：`yt-dlp --skip-download --dump-single-json --playlist-items 0 <URL>` 取频道根元数据（channel_id、title、avatar、banner）
- 如设了 `YOUTUBE_API_KEY` 环境变量，改用 `channels.list(forHandle=...)` 或 `channels.list(id=...)` 获取 `uploads` playlist ID

**输出**：`channel_id`、`uploads_playlist_id`、`avatar_url`、`banner_url`

### 阶段 2：选视频

**默认模式（yt-dlp）**：
```bash
yt-dlp --flat-playlist --playlist-items 1:100 \
  --print "%(id)s|%(title)s|%(view_count)s|%(upload_date)s|%(duration)s" \
  "<channel>/videos"
```
- 注意：`--flat-playlist` 在某些频道下 `view_count` 可能缺失。若超过 30% 缺失，自动降级到不带 `--flat-playlist` 的"半详细"模式（慢，但元数据全）。
- 默认过滤 Shorts（`duration > 60`），可关闭。

**精确模式（设了 API Key）**：
- `playlistItems.list(playlistId=<uploads>)` 分页拿全部 video_id
- 每 50 个 ID 一批调 `videos.list(part=statistics,snippet,contentDetails)` 取 `viewCount`、`publishedAt`、`duration`
- 全频道排序

**排序去重**：
```python
latest_5 = sorted(videos, key=lambda v: v.published_at, reverse=True)[:5]
popular_5 = sorted(videos, key=lambda v: v.view_count, reverse=True)[:5]
final = dedupe_by_id(latest_5 + popular_5)   # 每个 video 记录 source: [latest|popular|both]
```
**结果**：6~10 个 video（去重后），每个标记 `source`。

### 阶段 3：下载视频 + 缩略图 + info.json

```bash
yt-dlp \
  -f "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best" \
  --merge-output-format mp4 \
  -o "output/videos/%(title).180B [%(id)s].%(ext)s" \
  --restrict-filenames \
  --windows-filenames \
  --write-info-json \
  --write-thumbnail \
  --convert-thumbnails jpg \
  --download-archive archive.txt \
  -- <video_ids>
```
缩略图同时写入 `output/thumbnails/`（通过后处理移动）。

**文件命名**：统一用 `<safe_title> [<video_id>]` 作为 stem，所有后续模块（字幕/音频/md）共用同一 stem，避免命名漂移。

### 阶段 4：字幕 → 4 份独立 md

**步骤 A：用 yt-dlp 一次性拉全部字幕**（手动 + 自动）：
```bash
yt-dlp --skip-download \
  --write-subs --write-auto-subs \
  --sub-langs "en,en-US,en-GB,zh,zh-Hans,zh-Hant,zh-CN,zh-TW,ja,ko" \
  --convert-subs vtt \
  -o "output/transcripts/raw/%(title).180B [%(id)s].%(ext)s" \
  -- <video_ids>
```

**步骤 B：按语言归并生成 4 份 md**：
- 优先级：手动字幕 > 自动字幕
- `en`: `en` > `en-US` > `en-GB`
- `zh`: `zh-Hans` > `zh-CN` > `zh` > `zh-Hant` > `zh-TW`
- `ja`: `ja` > `ja-JP`
- `ko`: `ko` > `ko-KR`
- 如果某语种完全没有字幕：**不创建该 md 文件**（在 manifest 中记 `null`）；若开启了 `--asr-fallback`，调用 faster-whisper 转录 + 翻译（先转录原文再 LLM 翻译，但默认关）

**md 格式**（每个语种独立文件）：
```markdown
# <视频标题>

- **Video ID**: abc123
- **URL**: https://www.youtube.com/watch?v=abc123
- **Published**: 2026-05-01
- **Duration**: 14:05
- **Language**: en
- **Subtitle Source**: manual

---

[00:00:00] Hello everyone, welcome back...
[00:00:04] Today we are going to talk about...
[00:00:09] ...
```

**VTT 解析关键点**：
- 用 `webvtt-py` 解析
- 去重：YouTube auto-sub 经常滚动重复（每句出现 2-3 次），用滑窗 + 文本相似度去重
- 合并：相邻 < 1.5s 间隔的短句合并为一行
- 时间戳锚点：每行保留 `[HH:MM:SS]` 前缀，方便对照视频

### 阶段 5：音频提取 + 智能切分

**步骤 A：提取音频**：
```bash
ffmpeg -i "<title> [<vid>].mp4" -vn -c:a aac -b:a 64k -ac 1 \
  "output/audio/full/<title> [<vid>].m4a"
```
- 默认 `aac 64k mono`：~0.5MB/分钟，10MiB ≈ 20 分钟
- 可通过 config 调整：`audio_bitrate`、`audio_channels`

**步骤 B：判断大小**：
- ≤ 10 MiB → 直接结束
- > 10 MiB → 进入切分

**步骤 C：切分（按优先级找自然断点）**：

1. **YouTube chapters**（最优）：从 info.json 的 `chapters` 字段拿章节边界
2. **字幕间隙**：分析 VTT 中相邻字幕之间 > 1.5s 的间隔点
3. **ffmpeg silencedetect**：
   ```bash
   ffmpeg -i audio.m4a -af "silencedetect=noise=-30dB:d=0.5" -f null - 2>&1 | grep silence_end
   ```
4. **硬切**（兜底）：上述都没有时，按 `max_segment_sec` 等分

**贪心算法**：
```
target_size = 9.5 MiB   # 留 5% 安全边际
hard_limit = 10 MiB
bytes_per_sec = file_size / total_duration
max_segment_sec = target_size / bytes_per_sec

candidates = chapters || subtitle_gaps || silence_ends || []
segments = []
start = 0
while start < total_duration:
    ideal_end = start + max_segment_sec
    # 在 [start, ideal_end] 之间找最靠近 ideal_end 的断点
    cut = max([c for c in candidates if c <= ideal_end], default=None)
    if cut is None or cut - start < max_segment_sec * 0.5:
        cut = min(ideal_end, total_duration)   # 硬切
    segments.append((start, cut))
    start = cut
```

**步骤 D：用 ffmpeg `-c copy` 无重编码切分**（秒级）：
```bash
ffmpeg -i full.m4a -ss <start> -to <end> -c copy "<title> [<vid>].part01.m4a"
```

**步骤 E：校验每段实际大小**：
- 若某段 > 10 MiB → 向前找更早断点重切；仍超 → 该段强制等分为两段；并在 manifest 记 warning

### 阶段 6：博主本人照片（3-5 张）

**第一层（保底 1-2 张）**：
- 下载频道 `avatar`（一定有）
- 下载频道 `banner`（如有）
- 保存为 `01_avatar.jpg`、`02_banner.jpg`

**第二层（缩略图人脸筛选）**：
1. 用 InsightFace 检测 avatar 中的人脸
   - 若检测到 → 提取 embedding 作为 reference face，自动模式启动
   - 若未检测到（Logo/卡通）→ 标记 `manual_required=true`，状态置为 `needs_face_confirmation`
2. **自动模式**：
   - 拉取该频道额外 30 个视频的缩略图（仅缩略图，不下视频）
   - 对每张缩略图：检测所有人脸 → 计算与 reference 的余弦相似度 → 取最大值
   - 阈值 0.4 以上保留，按分数排序取前 3-5 张
3. **半自动模式**（reference 缺失 / 用户在前端点了"重新挑选"）：
   - 把所有检测到的人脸 crop 出来（去重，按相似度聚类后取每簇代表）
   - 前端展示候选脸网格（10-20 张），用户点选哪个是博主本人
   - 选定后：把对应脸的 embedding 存为 `reference_face.npy`，重跑自动筛选
   - reference 后续同频道复用（按 channel_id 缓存）

**降级（InsightFace 装不上）**：跳过人脸匹配，直接取 `avatar + banner + 播放量前 3 视频缩略图`，文件名标 `unverified_`

---

## 5. Web 页面

### 页面 1：首页（`/`）
- 顶部：粘贴 channel URL 的输入框 + "开始采集" 按钮
- 输入框下方：可展开的高级选项（latest_count、popular_count、languages、bitrate、是否过滤 Shorts）
- 中部：**任务历史卡片列表**（从 `jobs.jsonl` 倒序加载）
  - 每张卡片：频道头像 + 频道名 + 创建时间 + 视频数 + 状态 badge（running/completed/failed/needs_confirmation）
  - 点击卡片进入详情

### 页面 2：任务详情（`/jobs/<job_id>`）
- 头部：频道信息 + 状态 + 进度条（运行中实时刷新）
- Tab 切换：
  - **视频**：表格列出 6-10 个视频，每行有 source 标签、播放量、时长，点击展开看 4 语种 md 链接和音频分段链接
  - **图片**：grid 展示 3-5 张博主照片；如需确认，显示候选人脸网格让用户点选
  - **日志**：滚动展示 log.txt
- 底部：导出整个 `output/` 为 zip 下载按钮

### 设计风格（由 `/frontend-design` 输出）
- 清爽、白色背景、深灰文字、单色调强调色
- Tailwind utility-first
- Alpine.js 做状态管理 + 轮询

---

## 6. API 设计

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/jobs` | 提交新 job（body: `{channel_url, options}`），返回 `job_id` |
| GET | `/api/jobs` | 列出所有 job（读 `jobs.jsonl`，倒序） |
| GET | `/api/jobs/{job_id}` | 取某 job 的 manifest.json + status.json |
| GET | `/api/jobs/{job_id}/status` | 仅返回 status.json（前端轮询用，每 2s） |
| GET | `/api/jobs/{job_id}/log` | 返回 log.txt 文本 |
| GET | `/api/jobs/{job_id}/files/{path}` | 静态文件托管（mp4/md/jpg/m4a 等） |
| POST | `/api/jobs/{job_id}/confirm-face` | 用户在候选脸网格里选了"本人"（body: `{face_id}`），后端用此 embedding 重跑图片筛选 |
| GET | `/api/jobs/{job_id}/zip` | 打包整个 output/ 下载 |
| DELETE | `/api/jobs/{job_id}` | 删除 job + 文件（前端有"删除"按钮） |

---

## 7. 配置 & 环境

`.env.example`：
```
# 可选：设置后启用 YouTube Data API v3 精确模式
YOUTUBE_API_KEY=

# 可选：默认值，可被请求覆盖
DEFAULT_LATEST_COUNT=5
DEFAULT_POPULAR_COUNT=5
DEFAULT_AUDIO_BITRATE=64k
DEFAULT_MAX_AUDIO_MIB=10
DEFAULT_LANGUAGES=en,zh,ja,ko
```

`requirements.txt`：
```
fastapi
uvicorn[standard]
yt-dlp
python-dotenv
webvtt-py
pydantic
httpx
Pillow
numpy
insightface
onnxruntime
google-api-python-client    # 仅在用 API Key 时需要
# 可选：
# faster-whisper
```

系统依赖：`ffmpeg`（`brew install ffmpeg`）

---

## 8. 实施顺序（MVP 优先级）

1. **P0** 项目骨架：FastAPI app + 静态托管 + JSONL 存储 + 后台任务调度
2. **P0** 频道解析 + 视频选择（yt-dlp 默认模式）
3. **P0** 视频/缩略图/info.json 下载
4. **P0** 字幕 4 语种 md 生成
5. **P0** 音频提取 + 智能切分
6. **P1** 博主照片：avatar + banner + 自动人脸匹配
7. **P1** 前端：首页 + 历史列表 + 任务详情（基础版）
8. **P2** 半自动人脸确认 UI
9. **P2** YouTube Data API 精确模式
10. **P2** faster-whisper 字幕兜底
11. **P3** 打包 zip 下载、删除任务、日志查看

---

## 9. 方案 Review — 完整性自检

| 需求 | 覆盖度 | 风险点 & 兜底 |
|------|--------|---------------|
| 最新 5 + 播放最多 5，排重，按视频名保存 | ✅ | 文件名用 `Title [vid_id]` 防冲突；`--restrict-filenames` 处理非法字符；shorts 默认过滤 |
| 3-5 张本人照片/封面 | ✅ | 头像无脸时降级到半自动确认（前端选）；最差情况只用 avatar+banner+热门缩略图（标 unverified） |
| 字幕保存到 4 份独立 md（en/zh/ja/ko） | ✅ | 该语种无字幕则不创建 md；可选 ASR 兜底（默认关） |
| 音频 ≤10MiB 智能切分 | ✅ | 章节 > 字幕间隙 > 静音 > 硬切四层兜底；切完校验大小，超限回退 |
| Web 页面看历史 | ✅ | JSONL 倒序加载，详情页轮询 status.json，前端单页 |
| 本地 JSONL 存储 | ✅ | jobs.jsonl + per-job manifest.json，无数据库 |
| 无认证 | ✅ | FastAPI 不挂任何 auth 中间件，仅监听 127.0.0.1 |

**仍存在的不完美点**：
1. **"博主本人"判断**：单人频道几乎 100% 自动通过；多人/访谈/团队频道仍可能误判 → 通过"半自动确认"UI 兜住，第一次点一下，之后同频道复用 reference face
2. **字幕质量**：YouTube 自动字幕中文/日文/韩文准确度低于英文 → 开启 ASR 兜底（faster-whisper medium 中文/日文较准），但首版不默认启用
3. **大频道扫描慢**：精确模式 full_scan 在视频 > 5000 的频道下首次扫描可能 5-10 分钟；做 channel_id → video_id+view_count 增量缓存

**整体闭环可行性**：✅ 端到端可跑通；MVP 估计 1200-1500 行 Python + 300 行前端

---

## 10. 一些关键决策记录

- **为什么默认不用 YouTube Data API**：零配置启动门槛低；API Key 申请有摩擦；yt-dlp 在 90% 频道下 view_count 够用
- **为什么选 Alpine.js 而非 React**：单页面、无路由复杂度、Tailwind CDN 直接用、零 npm；学习曲线 30 分钟
- **为什么 m4a/aac 64k 而非 mp3**：m4a 容器对 AAC 更原生；64k mono 在人声转录场景质量足够；mp3 在低码率下高频损失大
- **为什么 9.5 MiB 而非 10 MiB**：容器头/MOOV/封装开销 ~2-5%；切完 `-c copy` 后实际大小可能略增；留 5% 安全边际避免反复重切
- **为什么 InsightFace 而非 face_recognition / DeepFace**：InsightFace 速度 ~0.02s/张，DeepFace 在高分辨率下显著慢；buffalo_l 模型 ONNX 部署简单
