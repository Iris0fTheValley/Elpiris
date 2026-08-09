# 文档导航

项目文档按用途分区，避免一次性实验记录与当前契约混在一起。

- `architecture/`：当前系统边界、接口契约和可替换适配器说明。
- `quality/`：当前有效的质量门、诊断方法与验收要求。
- `design/`：前端视觉和交互设计规范。
- `archive/`：带日期的历史审计与修复记录，仅供追溯，不代表当前实现。
- `project-layout.md`：仓库目录职责、生成物边界和新增文件放置规则。

API 的机器事实来源是 `frontend/openapi.json` 和
`frontend/src/api/generated.ts`；Markdown 只补充业务语义，不替代生成契约。

当前媒体质量文档：

- `quality/live2d-motion.md`：Live2D 连续帧稳定性与验收。
- `quality/source-evidence-and-media.md`：无广告证据卡、真实来源视频下载、断点续传与成片复现。
