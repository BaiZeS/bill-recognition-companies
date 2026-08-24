---
feature: batch-operations-docker
status: delivered
specs:
  - docs/compose/specs/2026-08-24-batch-operations-docker-design.md
plans:
  - docs/compose/plans/2026-08-24-batch-operations-docker.md
branch: master
commits: 14b6aea..07cca60
---

# 批量操作与 Docker 一键部署 — 最终报告

## What Was Built

票据识别系统从"单任务操作"升级为"批量化作业"：左侧任务列表支持批量选择（顶端常驻全选行 + 完成任务复选框），勾选后侧栏底部渐进展开操作区，以手风琴形式提供**批量抽取**（模板下载 / 上传规则 / 确认抽取）与**批量导出**（识别结果 / 抽取结果复选 + 确认导出）两组动作，全程无弹窗，执行状态直接叠加在任务行上。

规则模板完成两项改造：**Excel 化**——业务人员下载规则表格（表头 `字段编码|中文名|必填|类型|长度|回填策略`），用 Excel 填写后上传（.xlsx/.json 均可），错误提示精确到工作表行号；**按任务隔离**——取消全局规则模板，每个任务持有自己的规则（`tasks/{id}/rules.json`），旧全局模板作为惰性种子迁移。批量导出将所选任务打包为 zip：识别结果（修正版优先）或抽取结果（与单任务下载同构的扁平格式），各附一份 Excel 汇总表。

部署侧新增 Docker 一键部署：`cp .env.example .env`（填百炼 API key）→ `docker compose up -d --build` 即可启动。容器直挂 `/qn-server-data` 数据卷（满足既有挂载校验），模型缓存经软链复用（重建容器不重下模型），腾讯镜像源加速构建（约 2 分钟），单 worker 运行（1.5GB OCR 模型 / 3.6GB 内存约束）。

## Architecture

**后端**（`app.py`，FastAPI 单文件服务）：
- 规则存储：`TASKS_DIR/{id}/rules.json` + `RULES` 内存缓存（内存优先、文件兜底，应对 cosfs 负缓存）；`_seed_rules()` 惰性迁移旧全局模板；`_get_rules()` 加载时过 `validate_rules` 校验，损坏回退种子
- 批量抽取：`POST /api/batch/extract` 创建批次（422 非 done 列出 / 503 无 key / 409 互斥），守护线程 `_batch_worker` **顺序**调用 `_run_extract`（单任务抽取核心，写 `extracted.json` + `EXTRACTED` 缓存），任务级失败隔离；`GET /api/batch/{id}` 供前端 2s 轮询；批次注册表封顶 20 个已完成批次；批次仅内存态（重启丢失，前端 404 静默复位）
- 批量导出：`POST /api/export {task_ids, kind}` 服务端组装 zip（`bulk.build_zip`）；识别 zip 修正版优先（`_result_doc`，缺文件 404），抽取 zip 扁平格式（`bulk.flatten_extracted`）+ `X-Skipped` 响应头报跳过数
- 规则文件：`bulk.load_rules_file`（.xlsx/.json 统一过 `validate_rules`，行号重映射回工作表行）、`bulk.rules_to_xlsx`；端点 `/api/tasks/{id}/rules(.xlsx|/upload)`、`/api/rules-default(.xlsx)`、`/api/batch/rules/upload`；全局 `/api/rule-template` 已移除

**前端**（`static/`，原生 JS 无框架）：`state.selected`（选中集）+ `state.batch`（批次轮询、状态叠加、导出复选标志）；`renderBatchPanel` 渐进显示与禁用联动；`pollBatch` 2s 轮询生成任务行叠加状态（抽取中…/抽取成功/抽取失败红字，5 秒恢复）；`batchExport` fetch+blob 依次下载并读取 `X-Skipped`；所有批量 fetch 路径带网络失败复位与进行中守卫

**部署**：`Dockerfile`（python:3.12-slim + 腾讯源 apt/pip + `libgl1/libglib2.0-0/libgomp1` + uv 依赖层前置于源码 + `~/.paddlex` 软链数据卷 + `exec .venv/bin/python -m uvicorn` 单 worker 优雅停机）、`docker-compose.yml`（`BILL_PORT` 可配、`/qn-server-data` bind mount、`restart: unless-stopped`）

### Design Decisions

- **无弹窗、侧栏渐进展开**：批量操作作用于多任务，入口归任务列表区；手风琴互斥保持页面清爽（用户多轮交互确认的最终形态）
- **顺序执行批量抽取**：LLM 调用不宜并发，单守护线程逐任务执行 + 409 互斥，失败隔离不阻塞其余
- **批次不持久化**：批量抽取可重新发起，重启丢失由前端 404 静默复位兜底，避免引入持久化作业系统（YAGNI）
- **导出复选框用状态标志而非 DOM 状态**：禁用联动只改 `disabled`，用户手动取消勾选在选集变化后仍保留
- **Docker 直挂 `/qn-server-data`**：bind mount 满足 `os.path.ismount` 校验，零代码改动；模型缓存软链落数据卷，重建不重下

## Usage

**批量流程**：勾选任务（或全选）→ 批量抽取区可先"模板下载"取规则表格、"上传规则"批量写入 → "确认抽取"（任务行显示进度）→ 批量导出区勾选识别/抽取结果 → "确认导出"下载 zip（含 Excel 汇总）。

**单任务规则**：打开任务 → 规则抽取 → 规则模板页签（"仅对当前任务生效"）：上传规则 / 下载规则表格 / 添加字段 / 恢复默认 / 保存规则。

**Docker 部署**：
```bash
cp .env.example .env   # 填入 BAILIAN_API_KEY（规则抽取必需；仅识别可留空）
docker compose up -d --build
# 访问 http://localhost:8000（.env 的 BILL_PORT 可改端口）
```
容器内无本机 mimocode auth 回退，抽取功能的 key 必须来自 `.env` 或数据根 `config.json`。

**测试**：`uv run pytest tests/ -q`（38 个测试，数据根经 `BILL_DATA_ROOT` 隔离到临时目录）。

## Verification

- 后端 38 个测试全绿：规则 Excel（解析/表头/行号/roundtrip）、任务规则（种子/持久化/损坏回退/payload 覆盖/重启读文件）、批量（校验/顺序/失败隔离/去重/409/503/404）、导出（corrected 优先/X-Skipped/422 边界/缺文件 404）
- 前端 playwright 浏览器端到端：批量选择/手风琴/键盘操作/规则页签按任务化/**真实 LLM 批量抽取**（抽取中→成功→结果可见）/双 zip 下载内容核验/网络失败复位
- Docker：镜像构建成功（2.12GB），容器冒烟通过（挂载校验、`/api/tasks`、静态页、优雅停机 0 秒、无启动联网补包），生产实例全程未受影响
- 最终集成审查（独立子代理）：前后端 24 处调用契约逐一核对 + 隔离真实数据实例实测全链路，规格 [S3]-[S9] 全覆盖，既有单任务流程零破坏
- 已知未验证项：全新机器（无 cosfs）首次识别自动下载模型——本机无法复现；容器内完整 OCR 识别——需停生产实例释放 1.5GB 内存（留待正式切换时执行）

## Journey Log

> 简要记录影响最终设计的关键节点，非必读。

- [pivot] 初始需求"复原视图/规则抽取同级弹窗批量下载"经多轮讨论演化为"任务列表批量选择 + 手风琴操作区 + 无弹窗"，规则模板进一步 Excel 化并按任务隔离——入口归属随操作对象（多任务 vs 单任务）而定
- [dead end] python:3.12-slim 镜像缺 OpenCV 系统库，容器启动即崩（`ImportError: libGL.so.1`）——需显式安装 `libgl1 libglib2.0-0 libgomp1`；规格审查的运行时冒烟捕获了构建成功但运行失败的缺口
- [lesson] `uv run` 每次全新容器启动会联网补装 dev 依赖组——镜像 CMD 应直跑 `.venv/bin/python`（顺带 `exec` 获得优雅停机）；依赖层须前置于源码 COPY 以保缓存
- [lesson] 校验错误消息必须携带位置信息：`validate_rules` 的"字段编码重复"无行号前缀，导致 Excel 行号映射链路断裂，事后补丁修复
- [lesson] zip 去重类测试必须断言原始 `namelist()`——`set` 会折叠重名成员，使测试失去判别力

## Source Materials

| File | Role | Notes |
|------|------|-------|
| `docs/compose/specs/2026-08-24-batch-operations-docker-design.md` | 设计规格（12 章节，经独立审查） | [S10] 错误边界分散在各任务测试中 |
| `docs/compose/plans/2026-08-24-batch-operations-docker.md` | 实现计划（7 任务，含完整代码） | 已完成；计划中两处代码缺口（_result_doc 守卫、复选框默认勾选）在实现/审查中修正 |
