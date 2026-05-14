# YT Creator Archive

> 本地自用的 YouTube 博主数据采集工具。一条命令或一次 URL 粘贴，自动产出"最新 5 + 播放最多 5"视频、音频（≤10MiB 智能切分）、4 语种独立字幕 md、博主本人照片。

数据全部保存到本地 `data/` 目录，JSONL 索引 + 每 job 一份 `manifest.json`，无数据库，无认证。

---

## 功能

| 阶段 | 产出 |
|------|------|
| **解析频道** | channel_id、标题、头像、横幅 |
| **选视频** | 最新 N 条 + 播放最多 N 条（按 `video_id` 去重；默认过滤 Shorts） |
| **下载视频** | ≤1080p mp4 + info.json + 缩略图，文件名 `<标题> [<video_id>].mp4` |
| **提取音频** | AAC 64k mono；>10 MiB 时按"章节 > 字幕间隙 > 静音 > 硬切"四级 fallback 切分，每段 ≤10 MiB 且尽可能长 |
| **字幕** | 每个视频生成 `<标题>.en.md` / `.zh.md` / `.ja.md` / `.ko.md`，手动字幕 > 自动字幕；缺失语种不创建文件 |
| **ASR 兜底**（可选） | **只对完全无字幕的视频**调用 faster-whisper 转录音频"母语"；已有任何字幕的视频直接跳过（Whisper 不能跨语种翻译，跑了也是白跑） |
| **博主照片 3-5 张** | 频道头像 + 横幅 + InsightFace 人脸匹配的视频缩略图；头像不是真人脸时进入半自动确认（Web UI 点选） |

---

## 安装

**系统要求**：macOS / Linux，Python 3.10+，[`ffmpeg`](https://ffmpeg.org/)（macOS: `brew install ffmpeg`）

```bash
git clone <repo>
cd YouTubeDownload
./run.sh          # 首次会建虚拟环境 + 装核心依赖 (~30s)
```

启动后访问 [http://127.0.0.1:8000](http://127.0.0.1:8000)。

### 可选增强

```bash
.venv/bin/pip install -r requirements-extras.txt
```

包含：

- **`insightface` + `onnxruntime`** — 人脸识别，自动筛选博主本人的视频缩略图。不装则降级到 avatar + banner + 高播放缩略图（标 `unverified_`）。首次会下载 ~300MB 的 `buffalo_l` 模型。
- **`faster-whisper`** — Whisper ASR 兜底。前端高级选项里勾"ASR fallback"或 CLI 加 `--asr` 启用。
  **关键限制**：Whisper 只能转录**音频本身的语种**（韩语视频 → `ko.md`），不能跨语种翻译。
  **智能跳过**：对每个视频，只有在它**完全没有任何字幕**（en/zh/ja/ko 全部缺失）时才会触发 ASR。一旦视频有任意一个字幕（比如英文频道有 en 手动字幕），说明音频"母语"就是那个 — 此时跑 Whisper 必然只能再产出同一个语种，对填补 missing 的中日韩 0 价值，所以直接跳过整个 Whisper 调用，节省几分钟到几十分钟。
  可选模型：tiny / base / small / medium / large-v3（越大越准但越慢）。

---

## 用法

### A. Web UI

```bash
./run.sh
# 浏览器打开 http://127.0.0.1:8000
```

操作流程：

1. 顶部输入频道 URL（`@handle` / `/channel/UC...` / 任意视频 URL 都接受）
2. 点 **Begin collection**；展开 **Advanced options** 可改 latest/popular 数量、4 语种、音频码率、ASR 模型
3. 实时看 6 阶段清单进度，每行视频直接显示 EN/ZH/JA/KO + VIDEO + AUDIO + 文件夹按钮，点击 **reveal in Finder**
4. 出错或卡死任务：状态徽章变为 `failed · retry ↻`，一键续跑（已下载文件不重复下）
5. 服务重启会自动把所有 running 任务标为 failed，方便发现 + retry

### B. CLI

```bash
python -m backend.cli <channel_url> [options]
```

最常用：

```bash
# 默认 5+5，4 语种，64k 音频
python -m backend.cli @TomScottGo

# 只要 3 个最新的、英文字幕
python -m backend.cli https://www.youtube.com/@mkbhd --latest 3 --popular 0 --langs en

# 启用 ASR 兜底，medium 模型（适合中日韩内容）
python -m backend.cli @01coder30 --asr --asr-model medium

# 续跑某个 failed/interrupted 的 job
python -m backend.cli IGNORED --retry job_20260514_041709_TomScottGo

# 无颜色 + 安静模式（适合写入日志文件）
python -m backend.cli @mkbhd -q --no-color > run.log
```

**所有参数**：

```
positional:
  channel_url           频道 URL、@handle、或 UC... ID

options:
  --latest N            最新视频数（默认 5）
  --popular N           播放最多视频数（默认 5）
  --langs en,zh,ja,ko   字幕语种（逗号分隔）
  --bitrate 64k         音频码率
  --max-audio-mib 10    单段音频上限 MiB
  --quality 1080        视频最大分辨率
  --scan-size 80        为选 popular 扫描多少最近视频
  --include-shorts      不过滤 Shorts
  --asr                 启用 Whisper ASR 兜底（需先装 faster-whisper）
  --asr-model small     模型大小: tiny/base/small/medium/large-v3
  --retry JOB_ID        续跑指定 job
  -q / --quiet          安静模式
  --no-color            禁用 ANSI 颜色
```

CLI 和 Web UI **共享同一份 data/ 目录**：CLI 跑的任务会在 Web UI 历史列表里出现；反之亦然。

---

## 输出结构

```
data/
├── jobs.jsonl                          # 所有 job 索引（追加写）
└── jobs/
    └── job_20260514_103022_mkbhd/
        ├── manifest.json                # 完整元数据
        ├── status.json                  # 实时状态
        ├── log.txt                      # 运行日志
        └── output/
            ├── videos/
            │   ├── <title> [vid].mp4
            │   └── <title> [vid].info.json
            ├── thumbnails/
            │   └── <title> [vid].jpg
            ├── transcripts/
            │   ├── <title> [vid].en.md
            │   ├── <title> [vid].zh.md       # 若该语种有字幕
            │   ├── <title> [vid].ja.md
            │   ├── <title> [vid].ko.md
            │   └── raw/                       # 原始 VTT 全部保留
            │       └── <title> [vid].en.vtt
            ├── audio/
            │   ├── full/
            │   │   └── <title> [vid].m4a
            │   └── segments/                  # 仅当 full > 10 MiB
            │       ├── <title> [vid].part01.m4a
            │       └── <title> [vid].part02.m4a
            └── creator_images/
                ├── 01_avatar.jpg
                ├── 02_banner.jpg
                ├── 03_thumb_score0.83.jpg
                ├── reference_face.npy         # 该频道的人脸 embedding（确认后缓存）
                └── candidates/                # 半自动确认时的候选脸 crop
```

每个文件可以通过 Web UI 点 `reveal` 按钮在 Finder 里高亮显示。

---

## 配置

复制 `.env.example` 为 `.env` 修改：

```bash
cp .env.example .env
```

主要参数：

| 变量 | 默认 | 说明 |
|------|------|------|
| `HOST` / `PORT` | `127.0.0.1` / `8000` | 监听地址（默认仅本地） |
| `DEFAULT_LATEST_COUNT` | `5` | UI 默认最新视频数 |
| `DEFAULT_POPULAR_COUNT` | `5` | UI 默认播放最多数 |
| `DEFAULT_AUDIO_BITRATE` | `64k` | 默认音频码率 |
| `DEFAULT_MAX_AUDIO_MIB` | `10` | 默认单段音频上限 |
| `DEFAULT_LANGUAGES` | `en,zh,ja,ko` | 默认字幕语种 |
| `DEFAULT_SKIP_SHORTS` | `true` | 默认过滤 Shorts |
| `YOUTUBE_API_KEY` |（空）| 留作未来精确模式 |

---

## 半自动博主脸确认

当频道头像是 Logo / 卡通 / 检测不到真人脸时：

1. 任务状态会进入 **`awaiting input`**（橙色徽章）
2. 进详情页 → 点击徽章 → 弹出候选脸网格（从视频缩略图聚类出的 12-18 张脸）
3. 点选哪张是博主本人 → 系统用该脸的 embedding 重跑图片筛选
4. 被确认的 embedding 保存在 `creator_images/reference_face.npy`，下次同频道直接复用

CLI 模式下，状态会停在 `needs_face_confirmation`，开 Web UI 完成即可。

---

## FAQ

**Q：下载失败 HTTP 429？**
YouTube 限流。一会儿再试，或减少 `--scan-size`。yt-dlp 也会随版本失效，定期升级：
```bash
.venv/bin/pip install -U yt-dlp
```

**Q：某语种字幕没生成？**
说明该视频在 YouTube 上既无人工字幕也无该语种自动字幕。需要分两种情况：

- 该视频**还有其它语种**的字幕（比如有英文但缺中日韩）→ ASR 帮不上。Whisper 只能转录音频原语种，不能跨语种翻译。这种情况已经是设计内行为，工具会跳过 ASR、不报错，只在 log.txt 里写一行说明。
- 该视频**完全没有任何字幕**（4 个语种全空）→ 启用 `--asr` 后会跑 Whisper 转录，至少能补出原语种的 md。第一次跑会下载模型（tiny ~75MB / small ~500MB / medium ~1.5GB）。

如果想要"英语视频也产出中日韩字幕"，那是**翻译任务**（不在本工具范围）— 你需要在 ASR 之后再接一层翻译模型，比如 NLLB-200 / LLM API / DeepL API。

**Q：服务卡死或意外退出后任务还显示 running？**
启动时会自动扫描并把这类"僵尸"任务标记为 `failed`，Web UI 上直接出现 retry 按钮。点一下从断点续跑（已下文件不重复下）。

**Q：磁盘塞满怎么清理？**
首页卡片右侧 hover 出现垃圾桶按钮，一键删除整个 job + 所有文件。或者直接 `rm -rf data/jobs/<job_id>/` + 编辑 `data/jobs.jsonl`。

**Q：跨平台？**
后端跨平台。`open -R`（Finder reveal）走 macOS；Linux 用 `xdg-open` 打开父目录；Windows 用 `explorer /select,`。已经做了兼容。

---

## 合规

仅供个人合规研究测试使用。请遵守 [YouTube 服务条款](https://www.youtube.com/static?template=terms)。不要下载未授权内容、绕过付费墙、绕过年龄/地区限制。

---

## 技术栈

- **后端**: FastAPI · uvicorn · yt-dlp · ffmpeg · Pydantic
- **可选**: InsightFace（人脸识别）· faster-whisper（ASR）
- **前端**: 单页 HTML + Tailwind CDN + Alpine.js（零构建）
- **存储**: JSONL 索引 + per-job JSON manifest（无数据库）
