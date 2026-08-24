# 批量操作与 Docker 一键部署设计

日期：2026-08-24
状态：已与用户逐轮确认（批量选择 → 上传模板+批量抽取 → 导出 zip+Excel → 规则 Excel 化 → 规则按任务隔离 → 无弹窗渐进式交互）

## [S1] 背景与目标

现有系统所有操作均为单任务粒度：单个识别、单个规则抽取、单个下载。业务人员需要：

1. 在任务列表中**批量选择**任务，**批量上传规则模板**并**批量抽取**，**批量导出结果**（识别/抽取 JSON 打包 + Excel 汇总）。
2. 规则模板 **Excel 化**——业务人员不懂 JSON，用 Excel 填写抽取规则。
3. 规则**按任务隔离**——取消全局规则模板，每个任务持有自己的规则。
4. **Docker 一键部署**——替代当前手工 uvicorn 启动。

## [S2] 现状与约束

- 技术栈：FastAPI（`app.py`，562 行）+ 原生 JS（`static/`），无前端框架、无前端依赖。
- 数据根：`/qn-server-data/bill-recognition`（cosfs 对象存储挂载），`lifespan` 强制 `os.path.ismount("/qn-server-data")` 校验，未挂载拒绝启动。
- 任务数据：`tasks/{id}/`（input、page_*.png、result.json、extracted.json）；导出副本在 `output/{output_name}_structured.json` / `_corrected.json`；索引 `tasks.json`。
- OCR 模型常驻约 1.5GB，**必须单 worker**；本机内存仅 3.6GB。
- 模型缓存：`~/.paddlex/official_models` → `/qn-server-data/paddlex-models`（软链），fonts 同理。
- 现有全局规则模板：`DATA_ROOT/rule_template.json` + `/api/rule-template`（GET/PUT/DELETE），本设计将其移除。
- `extract.py` 的 `validate_rules` 已产生行级错误信息（"第 N 行…"），可直接复用于 Excel 解析报错。
- Docker 29.6.1 + Compose v5.3.1 已就绪；当前有一个裸跑 uvicorn 实例（端口 61739）。

## [S3] 批量选择与渐进式交互（无弹窗）

全部批量交互在左侧任务列表内完成，**不使用任何弹窗**，渐进展开保持页面清爽：

```
┌─ 任务列表 ────────────────┐
│ ☑ 全选                  │ ← 常驻顶端单独一行
│ ☑ 大连银行      完成      │ ← 完成任务行带复选框
│ ☑ 东亚银行      完成      │
│   成都农商行    识别中     │ ← 未完成任务无复选框
│ ┌─────────────────────┐ │
│ │ 已选 2 个  [取消选择]   │ │ ← 勾选 ≥1 才出现
│ │ [批量抽取] [批量导出]   │ │ ← 手风琴标题，点击展开
│ │ ▼ 批量抽取            │ │
│ │   [模板下载] [上传规则]  │ │
│ │   [确认抽取]           │ │
│ └─────────────────────┘ │
└─────────────────────────┘
```

规则：

- **全选行**：常驻顶端单独一行，勾选 = 选中所有"完成"任务。
- **任务复选框**：仅 `done` 状态任务显示；点击复选框不触发打开任务（stopPropagation），点击行体仍打开任务。
- **批量操作区**：勾选 ≥1 时出现于侧栏底部。
- **手风琴**：点击 `[批量抽取]` 展开 `模板下载 / 上传规则 / 确认抽取`；点击 `[批量导出]` 展开 `☑识别结果 ☑抽取结果 / 确认导出`；两者互斥（展开一个收起另一个）。
- **执行状态直接进任务列表**（见 [S6]），无进度弹窗。

## [S4] 规则按任务隔离

- 存储：`TASKS_DIR/{id}/rules.json`，内存缓存 `RULES: dict[str, list]`（与 `EXTRACTED` 同模式，应对 cosfs 负缓存）。
- 初始种子：现有全局 `rule_template.json` 若存在且有效则沿用，否则 `DEFAULT_RULE_TEMPLATE`；惰性生成（首次保存时写入）。
- "规则模板"页签改为编辑**当前任务**的规则，顶部提示"规则仅对当前任务生效"；切换任务重新加载。
- 页签按钮：`上传规则` `下载规则表格` `恢复默认` `保存规则`。
- "按照规则抽取"使用当前任务规则；前端仍把编辑器当前内容作为 `payload.rules` 传递（保留未保存可抽取的现有行为）。
- 移除 `/api/rule-template`（GET/PUT/DELETE）。
- 删除任务时 `rules.json` 随任务目录一并删除（现有 rmtree 已覆盖）。

## [S5] 规则文件 Excel 化

- **Excel 格式**：表头（第 1 行）`字段编码 | 中文名 | 必填 | 类型 | 长度 | 回填策略`；第 2 行起一行一条规则；全空行跳过；"必填"列取值 必填/可选/系统保留（其余按 `validate_rules` 规则归并为 可选）。
- **上传**：接受 `.xlsx`（openpyxl 解析）与 `.json`（直接校验），统一过 `validate_rules`；失败返回 422 + 行级错误，整体不落盘。
- **下载**：`GET /api/tasks/{id}/rules.xlsx` 生成当前任务规则的 Excel（即可作为上传模板复用）。
- **默认模板**：`GET /api/rules-default` 返回 `DEFAULT_RULE_TEMPLATE`（供"恢复默认"与种子）。
- **批量区"模板下载"**：`GET /api/rules-default.xlsx`——默认 21 字段模板生成的 Excel（表头 + 21 行），业务人员下载后增删改再经"上传规则"提交；需要以某任务现有规则为底稿时，用规则模板页签的"下载规则表格"。
- **批量上传**：`POST /api/batch/rules/upload`（multipart：file + task_ids），校验通过后写入所有选中任务，toast"已更新 N 个任务的规则"。

## [S6] 批量抽取

- `POST /api/batch/extract {task_ids}` → `{batch_id}`；非 done 任务整体拒绝（422 列出）；已有批次进行中 → 409。
- 服务端后台线程**顺序**执行（LLM 调用不宜并发），使用各任务自己的规则；复用现有单任务抽取逻辑（从 `app.py` 的 `extract_task` 中提取为可复用函数）。
- 每任务完成即写 `extracted.json` + `EXTRACTED` 缓存；单任务失败记录错误、不阻塞其余。
- `GET /api/batch/{batch_id}` → `{status: running|done, items: [{task_id, status: pending|processing|done|failed, error}]}`。
- 前端 2s 轮询，任务行状态叠加：抽取中 → "抽取中…"；批次结束后显示 "抽取成功" / "抽取失败：原因"（红色），保留 5 秒后恢复原状态文字；toast 汇总 "X 成功 / Y 失败"。

## [S7] 批量导出

- `POST /api/export {task_ids, kind: "result"|"extracted"}` → zip 附件（内存 BytesIO + zipfile）。
- **识别结果 zip**（kind=result）：
  - 每任务一个 JSON：优先 `{output_name}_corrected.json`（has_corrected 或文件存在），否则 `{output_name}_structured.json`——与现有单任务下载同逻辑，内存缓存优先。
  - 附 `识别结果汇总.xlsx`：一行一个键值对，列 `文件名 | 页码 | 字段名 | 字段值`（取自各页 kv；表格/文本行不做扁平）。
- **抽取结果 zip**（kind=extracted）：
  - 有抽取结果的任务：`{output_name}_extracted.json`，内容为扁平格式 `{_meta: {filename, model, extracted_at, groups}, results: [{字段编码: 值}]}`——与单任务"下载抽取 JSON"一致。
  - 无抽取结果的任务跳过；跳过数经响应头 `X-Skipped` 返回（前端以 fetch + blob 下载，可读响应头），toast 提示 "N 个任务无抽取结果已跳过"。
  - 附 `抽取结果汇总.xlsx`：一行一个回单组，列 `文件名 | 组序号 | 页码 | 字段…`；字段列表头为中文名，跨任务取字段编码并集。
- 前端两个复选框默认全勾；无任何任务有抽取结果时"抽取结果"禁用；两项都勾则依次请求、依次下载两个 zip。

## [S8] API 清单

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/tasks/{id}/rules` | 当前任务规则（无则返回种子） |
| PUT | `/api/tasks/{id}/rules` | 保存规则 JSON |
| POST | `/api/tasks/{id}/rules/upload` | 上传 .xlsx/.json 规则 |
| GET | `/api/tasks/{id}/rules.xlsx` | 下载当前任务规则 Excel |
| GET | `/api/rules-default` | 默认规则模板（JSON） |
| GET | `/api/rules-default.xlsx` | 默认规则模板 Excel（批量区"模板下载"） |
| POST | `/api/batch/rules/upload` | 批量上传规则（file + task_ids） |
| POST | `/api/batch/extract` | 创建批量抽取作业 |
| GET | `/api/batch/{batch_id}` | 查询批量作业进度 |
| POST | `/api/export` | 导出 zip（kind=result/extracted） |

移除：`/api/rule-template`（GET/PUT/DELETE）。

## [S9] Docker 一键部署

- **Dockerfile**：`python:3.12-slim` → 装 uv → 拷贝代码 → `uv sync`（清华源已在 pyproject 配置）→ 建立模型缓存软链（`~/.paddlex/official_models → /qn-server-data/paddlex-models`、`fonts → /qn-server-data/paddlex-fonts`）→ `EXPOSE 8000` → `CMD uvicorn app:app --host 0.0.0.0 --port 8000`（单 worker）。
- **docker-compose.yml**：`build: .`；`ports: "${BILL_PORT:-8000}:8000"`；`volumes: /qn-server-data:/qn-server-data`；`env_file: .env`；`restart: unless-stopped`。
- **.env.example**：`BAILIAN_API_KEY=`、`BILL_PORT=8000`。
- **.dockerignore**：`.venv` `.git` `input_doc` `output` `__pycache__` `.mimocode` `.playwright-cli` `docs`。
- bind mount 满足 `os.path.ismount` 校验，**无需改动挂载检查代码**。
- 模型缓存经软链复用：容器重建不重新下载模型；无 cosfs 的全新机器上数据目录自动创建，模型首次识别时自动下载。
- 一键流程：`cp .env.example .env`（填 key）→ `docker compose up -d --build`。README 增加部署章节。

## [S10] 错误处理与边界

- Excel 解析/校验失败 → 422 + 行号错误，整体不落盘。
- 批量抽取含非 done 任务 → 422 列出非法任务；批次进行中再提交 → 409。
- 导出：无 done 任务 → 422；kind=extracted 且全部任务无抽取结果 → 422 提示。
- cosfs 负缓存：规则/抽取/识别一律内存缓存优先、文件兜底（沿用现有模式）。
- 前端：无完成任务 → 批量操作区不出现；"抽取结果"复选框在无可导出抽取结果时禁用。

## [S11] 测试与验证

- 后端：本地 uvicorn + curl/httpx 验证各新端点（规则 CRUD、Excel 上传成功/失败、批量抽取进度、导出 zip 内容与 Excel 行）。
- 前端：playwright-cli 走批量流程（选择、手风琴展开、上传规则、抽取状态、导出下载）。
- Docker：`docker compose build` + `up`，容器内验证上传识别/规则/抽取/导出。
- 注意：本机内存 3.6GB，现有裸跑 uvicorn（61739）已占 OCR 模型；Docker 验证前需先停掉该实例（执行前与用户确认）。

## [S12] 文件变更清单

- `app.py`：新增规则/批量/导出端点；移除全局模板端点；单任务抽取逻辑提取为可复用函数。
- `bulk.py`（新增）：规则 Excel 解析/生成、导出 zip + xlsx 组装。
- `extract.py`：不变（复用 `validate_rules`、`DEFAULT_RULE_TEMPLATE` 等）。
- `static/index.html` / `app.js` / `style.css`：任务复选框、全选行、批量操作区手风琴、状态叠加。
- `pyproject.toml`：+ `openpyxl`。
- 新增 `Dockerfile`、`docker-compose.yml`、`.env.example`、`.dockerignore`；README 增加部署章节。
