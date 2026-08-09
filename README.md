# god-news

自动抓取全球正面新闻 → AI 翻译/脚本 → 两阶段人工审核 → 本地 TTS 语音合成 → 短视频产出。

## 快速开始

```powershell
git clone git@github.com:Iris0fTheValley/god-news.git
cd god-news

# 后端
py -3.11 -m venv .venv && .\.venv\Scripts\Activate.ps1
uv sync --extra dev --extra fetchers --extra asr --locked
Copy-Item .env.example .env   # 编辑填入 API Key

# 前端
pnpm install
pnpm --filter @god-news/frontend dev

# 一键启动
.\start.cmd
```

离线演示（不需要 API Key、网络或 GPU）：

```powershell
.\scripts\dev\start.ps1 -OfflineDemo
```

## 功能

- 四个固定来源自动抓取（大众新闻、Reddit、Guardian、Pikabu），三层 URL 抓取降级
- LLM 翻译 + 摘要 + AI 内容分类，人工初审可修订
- LLM 固定前缀优先以提高 DeepSeek 上下文缓存命中，并对已验证的完全相同请求做有界内存缓存与并发合并；人工显式重生成始终绕过旧响应
- 初审批准后自动生成口播脚本；脚本人工审核批准后，才可显式启动本地 GPT-SoVITS 合成语音
- 人工终审通过后产出 `ProductionManifest` 时间轴，并创建带输入资产快照的可审阅视频批次
- 软归档（ARCHIVED）、重开终审、故事编辑，完整审计追踪
- 角色管理、采集运行日志、视频批次编排、BGM 目录浏览、运维清理

## 管线

```
抓取 → 翻译/AI分类 → 人工初审 → 生成口播 → 人工审口播 → 手动 TTS → 人工终审 → DONE
                                                          ↑ 仅此处显式消耗本地 GPU
                                             终审改稿 ───┘
任一非归档状态 → ARCHIVED（软归档）
```

## 审核与手动合成

生产状态路径为：

`FETCHED → TRANSLATED → PENDING_FIRST_REVIEW → PROCESSING_SCRIPT → SCRIPT_READY → PENDING_TTS → PROCESSING_TTS → PENDING_SECOND_REVIEW → DONE`。

`SCRIPT_READY` 是口播脚本的人工审核门。批准后故事进入 `PENDING_TTS`；客户端必须以当前乐观锁版本显式发起合成，服务才会启动本地 TTS：

```http
POST /api/v1/stories/{story_id}/reviews/script
Content-Type: application/json

{"expected_story_version": 5, "decision": "approve", "reviewer_id": "script-editor"}
```

```http
POST /api/v1/stories/{story_id}/synthesize
Content-Type: application/json

{"expected_story_version": 6}
```

TTS 失败会安全回到 `PENDING_TTS` 并保留 `last_failure`，需要使用新版本再次手动触发；`/resume` 不会越过 `SCRIPT_READY` 或 `PENDING_TTS` 自动合成。

## 技术栈

| 层 | 技术 |
|---|---|
| 后端框架 | FastAPI + Uvicorn（全异步） |
| 数据模型 | Pydantic v2 强类型领域模型 + 状态机 |
| 持久化 | SQLAlchemy 异步 + SQLite，乐观并发 |
| LLM | DeepSeek V4 Flash（可选 LM Studio 本地）；前缀缓存友好、精确响应 LRU、相同并发请求合并 |
| 记忆 | ChromaDB 本地嵌入式持久化 |
| TTS | GPT-SoVITS v2Pro，单个串行 loopback 子进程；按段切换角色与七情绪参考材料 |
| 前端 | React + Vite + TanStack Query，OpenAPI 自动生成类型 |
| 视频 | 可替换的本地 Remotion 适配器；同一 `ProductionManifest 2.0` / `EpisodePlan 1.0` 生成 9:16 与 16:9，经 ffprobe 和 SHA-256 验证 |

## 开发

```powershell
# 后端
python -m pytest --cov=god_news --cov-fail-under=75
python -m ruff check .
python -m mypy src/god_news

# 前端
pnpm --filter @god-news/frontend check
pnpm --filter @god-news/frontend test

# 视频
pnpm --filter @god-news/video check
pnpm --filter @god-news/video test

# 清理可再生缓存、日志、测试报告和前端静态暂存（保留 data/outputs/依赖）
.\scripts\maintenance\clean_workspace.ps1
```

前端接口契约、已实现能力与诚实的运行边界见
[`docs/architecture/frontend-api-mapping.md`](docs/architecture/frontend-api-mapping.md)。
仓库文件职责和测试/文档/正式代码分区见
[`docs/project-layout.md`](docs/project-layout.md)。

## 真实双比例端到端演示

以下命令使用当前配置的 GPT-SoVITS、DSakiko Cubism 2 角色和真实 Remotion 渲染器，生成一套内容驱动的 9:16 / 16:9 技术演示。当前固定演示使用五条来自 Guardian、Pikabu 和大众网的真实新闻快照、一段项目生成的有限长度源视频及经过权利标注的来源页证据截图；新闻输入严格限制为 Guardian、Reddit、Pikabu、大众网四个注册来源。总时长由实际 TTS、源视频和转场编译得出，不会循环素材或填充到虚假的目标时长。E2E 的 SQLite、Chroma 和媒体目录按运行隔离，不会污染正式故事库；外部素材也不会在权利状态未知时被标记为可发布：

```powershell
$env:GOD_NEWS_E2E_DSAKIKO_ROOT = "J:\path\to\DSakiko"
python scripts/quality/run_e2e_video.py
```

脚本通过正式的故事持久化、节目导演、口播审核、批次 TTS、时间轴审核和渲染状态流运行；生产审核门不会被删除。每次运行输出：

- 1080×1920 抖音 MP4；
- 1920×1080 Bilibili MP4；
- 每个故事开始、中间、结束以及所有转场、片头、片尾的代表帧和 contact sheet；
- `artifact-report.json`，包含导演计划、口播/字幕、TTS 与 Live2D 哈希、源视频权利状态和双输出媒体信息。

角色路径、模型、参考音频和密钥不会进入 Git。渲染子进程若以明确的 `FAILED` 状态结束，E2E 驱动最多执行一次有界重试；所有上游审核快照保持不变。

## 模板实验室

前端 `/template-lab` 使用与生产渲染相同的 Remotion Composition、模板快照、布局编译器和场景注册表。它可以切换 `world_warmth@1.1.0` 的语义场景、视觉变体、横竖屏 profile 与真实 fixture，并提供播放、逐帧、关键帧跳转、安全区/素材/主持人/字幕诊断层、可复现 URL 和 validated props 导出。

```powershell
pnpm --filter @god-news/frontend dev
# 打开 http://127.0.0.1:5173/template-lab

# Microsoft Edge 真实媒体、控制台与字幕溢出回归
$env:GOD_NEWS_TEMPLATE_LAB_HOST_VIDEO = "I:\path\to\reviewed-live2d-host.webm"
pnpm --dir frontend exec playwright test --project=desktop template-lab.visual.spec.ts
```

Template Lab 的 Live2D 预览必须提供真实预渲染 WebM；缺少媒体时会明确停止，不会画假主持人。角色 WebM 来自用户本地 Live2D 模型，因此被 Git 忽略，不随仓库重新分发；可在 URL 参数或页面输入框中提供本地开发服务器可访问的预渲染媒体。视觉回归会从 `GOD_NEWS_TEMPLATE_LAB_HOST_VIDEO` 将已审核文件临时复制到 public 目录，并在测试结束后删除；变量缺失时仅明确跳过两项主持人用例，其余真实媒体检查仍运行。页面中的截图和视觉回归操作会复制真实可执行命令，不伪造浏览器内截图能力。

历史上未保存模板版本的旧批次仍可审计读取，但不会静默套用当前模板重新渲染。新生产批次必须冻结模板 ID、版本、能力、变体、布局与设计令牌。

## 当前运行边界

- 四个真实内容源只有在对应凭据、用途授权和站点条款均确认后才会启用；离线演示不访问真实网络。
- 视频批次、时间轴审阅、审核输入快照、严格类型的 `ProgramDirectorPlan` / `EpisodePlan`、版本化模板、类型化视觉素材、审核通过的原始视频和真实 Remotion 双格式渲染已接入。节目导演只排列不可变的已审核故事、选择注册语义场景、决定已批准原视频是否在故事后插入，并为相邻故事生成显式串联段；模板与确定性编译器负责视觉变体、布局和时间轴。`GOD_NEWS_VIDEO_RENDERER_ENABLED=false` 仍是安全默认值。
- DSakiko 兼容的 Cubism 2 Live2D 可选适配器会在最终批次 TTS 后，按每段最终 `speaker_id` 在一次性 OpenGL 子进程中生成透明 VP9 WebM。离线渲染使用与 SDK 同源的可注入帧时钟，不依赖子进程实际耗时。生产默认由 SDK 独占动作、姿态和物理，只在最后覆盖确定性眨眼、平滑眼神与嘴型；使用稳定 `idle` 动作且关闭运行时会大幅摆动头身的自动呼吸。需要更强表演时可显式配置 `GOD_NEWS_VIDEO_LIVE2D_MOTION_POLICY=emotion_once` 和 `GOD_NEWS_VIDEO_LIVE2D_SDK_AUTO_BREATH=true`。审核快照记录角色版本、模型树哈希、音频哈希和角色视频哈希；Remotion 不加载 Live2D SDK，并通过透明 `OffthreadVideo` 在渲染时逐帧提取主持人画面，避免浏览器视频时钟造成单帧错位。启用前须配置 `GOD_NEWS_VIDEO_LIVE2D_PYTHON_EXECUTABLE` 与 `GOD_NEWS_VIDEO_LIVE2D_TRUSTED_ASSET_ROOTS`，模型文件不进入仓库。
- Live2D 生产适配器要求单一参数所有权、完整逐帧 JSONL 轨迹以及参数级和图像级动态门禁全部通过；当前运行边界与质量检查见 [`docs/quality/live2d-motion.md`](docs/quality/live2d-motion.md)，历史实验记录归档在 `docs/archive/`。
- 生产视频质量门需要功能完整的 FFmpeg（支持 `blackdetect` 与 `freezedetect`），通过 `GOD_NEWS_VIDEO_QUALITY_FFMPEG_COMMAND` 配置。Remotion 自带的裁剪版 FFmpeg 仍用于封装，但不会被误当作视觉质量分析器。
- 已启用 TTS 的角色使用独立的权重对、七组情绪参考音频/文本与可选参考语言；合成器按段选择角色和情绪。为保护显存，不同权重永不并存，切换时会先终止旧本地子进程。
- 固定内容源的网络采集节奏由后端强制：同一来源两次网络采集完成之间不少于 30 秒，全局最多两个采集请求并行。前端不提供频率设置；随仓库启动命令固定为单 worker，若未来部署多进程/多实例，须先替换为持久化租约适配器。
