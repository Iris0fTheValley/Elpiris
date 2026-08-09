# god-news 前后端接口契约

> 本文只描述工作台核心工作流，并非端点穷举。机器事实来源是
> `frontend/openapi.json` 与 `frontend/src/api/generated.ts`；素材发现、审批和内容读取等
> 新增端点应以生成契约为准。

更新时间：2026-07-15

本文件描述工作台实际调用的后端能力。`frontend/openapi.json` 与
`frontend/src/api/generated.ts` 由 FastAPI 导出，是可生成的唯一事实来源；本文件只补充业务语义和使用边界。

```powershell
pnpm --dir frontend generate:openapi
pnpm --dir frontend check
```

## 故事与审核

| 前端能力 | API | 语义 |
| --- | --- | --- |
| 队列与状态筛选 | `GET /api/v1/stories` | 默认不返回归档故事；`status=ARCHIVED` 显式查看归档。 |
| 工作台 | `GET /api/v1/stories/{story_id}` | 可读取归档故事及其历史证据。 |
| 创建 | `POST /api/v1/stories` | URL 或上传文本进入抓取、翻译和初审管线；创建界面只收集来源与目标语言，播报偏好在初审提交。 |
| 编辑元数据 | `PATCH /api/v1/stories/{story_id}` | 需要 `expected_story_version`；只允许标题、风格与目标时长，不能篡改来源快照。 |
| 归档 | `DELETE /api/v1/stories/{story_id}` | 软归档为 `ARCHIVED`，保留来源、版本、审核和状态迁移证据。 |
| 重开终审 | `POST /api/v1/stories/{story_id}/reopen` | 仅 `DONE → PENDING_SECOND_REVIEW`。 |
| 初审 | `POST /api/v1/stories/{id}/reviews/first` | 可在此提交 `preferences` 覆盖（播报风格、目标时长、默认角色、情绪、语速等）；批准后只生成口播脚本，不启动 TTS。 |
| 口播脚本审核 | `POST /api/v1/stories/{id}/reviews/script` | 仅 `SCRIPT_READY` 可用。批准后进入 `PENDING_TTS`；`request_changes` 可带 `revised_script`，仍停留在脚本审核门。 |
| 单段口播重生成 | `POST /api/v1/stories/{id}/script/segments/{segment_id}/regenerate-spoken` | 人工编辑可读字幕后，按该字幕与所选口播语言重新生成单段 TTS 文本。仅返回预览，不直接写入故事，避免覆盖页面中其他未保存修订；正式保存仍走脚本审核接口。 |
| 手动语音合成 | `POST /api/v1/stories/{id}/synthesize` | 请求体只有 `{expected_story_version}`。仅 `PENDING_TTS` 可用，先持久化为 `PROCESSING_TTS`，成功后进入终审；失败安全回退 `PENDING_TTS` 并写入 `last_failure`。 |
| 终审 | `POST /api/v1/stories/{id}/reviews/second` | 仅 `PENDING_SECOND_REVIEW` 可用。终审携带 `revised_script` 时会清空音频并返回 `SCRIPT_READY`，必须重新审核脚本后手动合成。 |

生产状态路径为 `FETCHED → TRANSLATED → PENDING_FIRST_REVIEW → PROCESSING_SCRIPT → SCRIPT_READY → PENDING_TTS → PROCESSING_TTS → PENDING_SECOND_REVIEW → DONE`。
`ARCHIVED` 是不参与生产进度条的显式终态，可从任一非归档状态进入。

`POST /api/v1/stories/{id}/resume` 只恢复 `FETCHED`、`TRANSLATED`、`PROCESSING_SCRIPT` 和 `PROCESSING_TTS` 的中断工作；不会从 `SCRIPT_READY` 或 `PENDING_TTS` 自动开始高能耗合成。

脚本段 API 使用结构化 `spoken_text`、`spoken_language` 与 `captions[]`；`captions` 区分 `verbatim` 和 `translation`，且逐字段校验逐字字幕必须与实际 TTS 输入完全一致。旧持久化数据中的 `text` / `language` 仍可读取，但新响应和新写入只输出结构化字段。脚本段同时保留 `speaker_id`、`emotion`、`speed`、`pitch`、`visual_hint`；其中 `emotion` 为 `happiness`、`sadness`、`anger`、`disgust`、`like`、`surprise`、`fear` 之一，`scene_transition` 为 `black`、`crossfade`、`slide`、`wipe`、`mood_shift` 之一。无效 LLM 输出分别回退为初审偏好情绪和 `black`；`visual_hint`、`pitch` 即使当前 UI 隐藏也保持 API 兼容。
`target_duration_seconds` 只约束 AI 口播，默认 20 秒并允许 5–600 秒；它不约束后续原始来源视频模块的素材时长或整条故事编译时长。

## 源视频证据

| 前端能力 | API | 语义 |
| --- | --- | --- |
| 已采集证据 | `GET /api/v1/stories/{story_id}/source-media` | 返回不可变的来源、署名、权利、SHA-256、大小与 ffprobe 媒体快照；绝不返回主机存储路径。 |
| 手动采集 | `POST /api/v1/stories/{story_id}/source-media/acquire` | 需要 `expected_story_version`、来源媒体索引和操作方标识；同一故事与媒体索引幂等。只允许已标准化为视频的来源素材，下载受出站 URL 策略、大小上限、MP4 签名和 ffprobe 解码校验约束。 |
| 审核播放 | `GET /api/v1/stories/{story_id}/source-media/{artifact_id}/content` | 只按故事作用域和制品 ID 读取；发送前重新核对大小和 SHA-256，缺失、越界或被篡改均拒绝。 |
| 字幕任务 | `POST /api/v1/stories/{story_id}/source-media/{artifact_id}/transcriptions`、`GET .../transcriptions` | 本地 faster-whisper 在一次性子进程中生成原文和时间戳；跨语言字幕按有界批次交给 LLM 翻译。任务状态、尝试次数、检测语言、失败证据均持久化，服务重启不会伪装成仍在处理。 |
| 字幕取消 | `POST /api/v1/stories/{story_id}/source-media/{artifact_id}/transcriptions/{transcription_id}/cancel` | 终止受监督的本地识别任务并持久化 `operator_cancelled`；服务关闭使用独立的 `service_shutdown` 证据。 |
| 字幕人工审核 | `POST /api/v1/stories/{story_id}/source-media/{artifact_id}/transcriptions/{transcription_id}/review` | 只允许待审核字幕批准或驳回。编辑者可修订原文/译文，但 cue ID、顺序和时间戳必须与 ASR 证据完全一致。未经批准的字幕不能进入正式节目计划。 |

采集成功不代表获得转载权。`publish_eligible` 只能由采集时冻结的权利字段推导；Reddit 等权利未知或要求人工确认的素材始终显示为“仅供审核”，不能因下载成功自动进入可发布节目。源文件保存在输出根目录内的独立 `source-media` 子树，并加入留存保护，避免仍被数据库引用时遭到清理。

源视频 ASR 默认关闭，启用前需要安装 `asr` 可选依赖并设置 `GOD_NEWS_SOURCE_MEDIA_ASR_ENABLED=true`。默认 `cpu/int8` 是资源隔离策略，避免与近满载的本地 LLM/TTS GPU 争抢显存；模型、设备、计算类型和缓存路径均由强类型配置控制。

## 角色

| 前端能力 | API | 语义 |
| --- | --- | --- |
| 列表/详情 | `GET /api/v1/roles`、`GET /api/v1/roles/{profile_id}` | 支持 `enabled` 筛选。 |
| 新建 | `POST /api/v1/roles` | 创建旁白或主持人档案。 |
| 替换 | `PUT /api/v1/roles/{profile_id}` | 需要 `expected_version` 乐观锁。 |
| 停用 | `DELETE /api/v1/roles/{profile_id}` | 软停用并返回新版本；历史故事和成片保留引用。 |

启用本地 TTS 的角色须提交 `tts_enabled=true`、成对的 `gpt_weights_path` / `sovits_weights_path`、`tts_model_profile` 和完整七项 `emotion_refs`；`default_spoken_language` 是脚本初审时使用的默认口播语言，`reference_language` 仅用于 GPT-SoVITS 参考文本，两者不得混用（DSakiko 日语参考配置会导入为 `all_ja`）。合成器按脚本段的 `speaker_id` 与 `emotion` 选择参考材料，并且只读取 `spoken_text`，翻译字幕不会进入 TTS；同一权重相邻段只切参考音频，不重载模型。不同权重会严格顺序切换：先杀净旧 loopback 子进程，再启动下一套，因此任一时刻只驻留一套重型权重。

## 采集运行

| 前端能力 | API | 语义 |
| --- | --- | --- |
| 可用采集器 | `GET /api/v1/sources/collectors` | 返回四个源的配置与授权就绪度。 |
| 来源健康 | `GET /api/v1/sources/health?probe_network=false` | `enabled`、`configured`、`authorized`、`contract_ok` 与 `reachable` 是独立事实；默认不联网，只有显式 `probe_network=true` 才做轻量探测。 |
| 来源凭据诊断 | `POST /api/v1/sources/{source}/diagnostics` | 显式运行来源能力诊断。Reddit 只执行 OAuth client-credentials 换取并返回脱敏证据，不拉取帖子；凭据有效与内容转载授权仍是两个独立事实。 |
| 自动采集控制 | `GET /api/v1/source-schedule`、`POST /api/v1/source-schedule/start`、`POST /api/v1/source-schedule/stop` | 默认关闭；操作方只控制启停。后端固定轮询与采集间隔不进入公共响应，也不在前端提供频率设置。启停意图持久化，重启后恢复；停止不会强制取消已经开始的 run。 |
| 启动 | `POST /api/v1/source-runs` | 返回 `202` 和持久化 run；前端不暴露采集频率。每个来源的网络采集间隔和全局并发均由后端固定策略控制。 |
| 列表/详情 | `GET /api/v1/source-runs`、`GET /api/v1/source-runs/{run_id}` | 包含降级层尝试、标准化导入结果、错误证据，以及运行中当前导入序号、标题、外部 ID 和已移除敏感 query/fragment/userinfo 的展示 URL；终态会清空“当前对象”。`filtered_count` 和条目级 `excluded_topic_politics` / `excluded_topic_sports` 表示内容在创建故事前被服务端准入策略拦截，不计为抓取或导入失败。 |
| 取消 | `POST /api/v1/source-runs/{run_id}/cancel` | 协作式停止；记录 `operator_cancelled`，不删除已完成项目。 |

采集保护是服务端策略，不是可调 UI：同一来源上次网络采集完成后至少等待 30 秒，所有来源合计最多 2 个网络采集同时进行。随仓库启动命令固定为单 worker；多进程部署必须先引入持久化租约协调器，不能依赖内存锁。

政治与体育是产品级排除主题：Guardian 查询先使用官方搜索语法减少无效结果，所有来源随后仍须经过统一、确定性的元数据与编辑标题准入检查。该检查位于故事创建和 LLM 调用之前，不能只靠前端隐藏。

服务关闭导致的停止会独立记录为 `service_shutdown`，不能与操作员取消混淆。

## 视频批次与 BGM

| 前端能力 | API | 语义 |
| --- | --- | --- |
| 本地 BGM 目录 | `GET /api/v1/video/bgm` | 仅返回 `track_id`、`display_name`、`relative_path`、`size_bytes`；当前没有媒体流/试听接口。 |
| 新建批次 | `POST /api/v1/video/batches` | 从未占用的 `DONE` 故事建立不可变快照；LLM 只输出故事顺序、注册场景、原视频策略和相邻故事串联词，确定性编译器复用原始已审核段落；可选择本地 BGM。 |
| 列表/详情 | `GET /api/v1/video/batches`、`GET /api/v1/video/batches/{batch_id}` | 返回不可变来源脚本快照、`direction` 节目导演计划、节目口播、审核、输入资产快照和版本。导演计划显式区分来源段与相邻故事串联段。 |
| 审核节目口播 | `POST /api/v1/video/batches/{batch_id}/narration-review` | `APPROVE` / `REJECT` 仅初始 `PENDING_NARRATION_REVIEW` 可用；携带 `revised_script` 的 `REVISE` 也可在 `PENDING_BATCH_TTS` 或 `PENDING_TIMELINE_REVIEW` 提交，会清除派生音频和时间轴并返回口播审核门。修订必须保留导演计划拥有的段落 ID 与顺序。 |
| 手动合成节目口播 | `POST /api/v1/video/batches/{batch_id}/narration/synthesize` | 仅 `PENDING_BATCH_TTS` 可用；独立本地 TTS 成功后，按导演计划将已批准原视频交错插入故事位置；若启用了 Live2D，会按最终脚本段角色在隔离子进程中预渲染透明角色媒体；全部成功才进入 `PENDING_TIMELINE_REVIEW`。 |
| 试听节目旁白 | `GET /api/v1/video/batches/{batch_id}/audio/{segment_id}` | 仅返回该批次已持久化的节目旁白段。服务端会校验段归属、输出目录边界与文件存在性；未合成、无效或越界路径均返回 `409`，不会暴露本地文件。 |
| 审阅时间轴 | `POST /api/v1/video/batches/{batch_id}/timeline-review` | 节目口播音频、导演场景计划和 Manifest 就绪后才可审；批准后进入 `READY_TO_RENDER`。 |
| 渲染 | `POST /api/v1/video/batches/{batch_id}/render` | 需要 `expected_batch_version`。启用 `GOD_NEWS_VIDEO_RENDERER_ENABLED` 后，本地进程适配器会从同一语义快照原子生成抖音竖屏与 Bilibili 横屏两件制品，包含配置化片头、场景转场和片尾，并用 Remotion 自带 ffprobe 验证。 |
| 播放成片 | `GET /api/v1/video/batches/{batch_id}/outputs/{profile_id}` | 只接受平台注册 ID，不接受文件路径。服务端在同一已打开文件句柄上核对大小与 SHA-256 后流式返回；旧版单输出制品只读保留，不伪装成双平台成片。 |
| 取消/删除 | `POST /api/v1/video/batches/{batch_id}/cancel`、`DELETE /api/v1/video/batches/{batch_id}` | 未渲染批次可释放故事；渲染中无安全取消契约，已渲染批次是不可变审计证据。 |

批次 TTS 完成并生成 Manifest 时会记录音频和 BGM 的内容哈希。渲染适配器按该证据创建一次可信 staging，横竖屏都只读同一份字节。审阅后发现输入文件变化时不能直接复用旧审批；应取消/删除该批次并创建新批次重新审阅。服务重启会把孤立的 `RENDERING` 批次恢复为可重试 `FAILED`。列表会隔离不符合当前 schema 的旧批次并记录其 ID，避免一条历史坏记录拖垮整个页面；按 ID 读取该记录仍明确返回冲突，不会自动改写审计数据。

## 运维

| 前端能力 | API | 语义 |
| --- | --- | --- |
| 操作历史 | `GET /api/v1/operations/runs` | 返回 `running`、`succeeded` 或 `failed` 的留存清理记录及结果。 |
| 调度状态 | `GET /api/v1/operations/schedules` | 返回启用状态、间隔、下次运行与最近状态。 |
| 手动留存清理 | `POST /api/v1/operations/retention/runs` | 请求为 `{operation: "retention_cleanup", dry_run, requested_by}`。前端的确认操作使用 `dry_run: false`，会物理删除符合保留规则的文件。 |

## 已知边界

- 真实四源采集仍取决于合法授权、凭据和站点条款；离线测试不替代现场运行证据。
- GPT-SoVITS 多角色与七情绪参考音频、严格 `EpisodePlan`、审核后原始视频和 DSakiko Cubism 2 Live2D 预渲染均已通过可替换边界接入。Live2D 模型只允许从后端配置的可信本地根解析；API 返回角色版本和哈希证据，不暴露模型或生成媒体的本地路径。节目导演的故事重排、串联词与更丰富证据场景仍是后续纵向切片。
- 任何前端请求应通过 `frontend/src/api/client.ts`，不得自行拼接未写入 OpenAPI 的接口。
