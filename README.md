# 票据识别与结构化回填系统

项目基于 PaddleOCR-VL-1.6 实现票据信息的结构化抽取与回填
PaddleOCR-VL-1.6 项目地址：https://aistudio.baidu.com/modelsdetail/49710/intro

面向银行电子回单（pdf / jpg / png）的端到端处理系统：**预处理 → OCR 识别 → 结构化 → 人工校正（复原视图）→ 按规则 LLM 抽取回填 → 批量导出**。以 Web 服务交付，业务人员通过浏览器完成全流程，支持批量作业，并可 Docker 一键部署。

## 功能总览

### 1. 票据识别
- 多文件上传（`.pdf` `.jpg` `.jpeg` `.png`），后台队列逐页识别，进度实时可见。
- 可选预处理（按任务开关，默认开启）：
  - **水印擦除**（格点模板匹配，低分辨率细笔画找回配套 `text_det_thresh=0.2`）；
  - **印章擦除**（红色像素集 + 孔洞检测，先水印后印章，互不干扰）。
- OCR 采用 PP-OCRv6 检测/识别管线（paddleocr 3.7，关闭文档方向分类/扭曲矫正/行方向分类以省内存提速）。
- 识别结果**结构化**为逐页 JSON：`text_lines`（阅读序文本行）、`tables`（行聚类 + 表格切分）、`kv`（冒号键值对拆分），每个元素带稳定 `id` 与 `box` 坐标——这是前端高亮联动与抽取的元素基础。

### 2. 复原视图（人工校正）
- 左栏原图逐页预览 + bbox overlay，右栏结构化结果；文本行与键值对双向点击联动高亮。
- 支持就地编辑识别文本，保存后写为**修正版**（`*_corrected.json`），后续下载/导出自动以修正版优先。

### 3. 规则抽取（结构化回填）
- **21 字段默认规则模板**（银行账号、交易日期、金额、收付方向、流水号、用途、摘要、回单编号等，含 2 个系统保留字段），面向银企对账回填场景。
- 规则**按任务隔离**：每个任务持有自己的 `rules.json`，互不影响。
- 规则文件 **Excel 化**：下载 `字段编码|中文名|必填|类型|长度|回填策略` 表头的规则表格，Excel 填写后上传（`.xlsx` / `.json` 均可），校验错误精确到工作表行号；支持行内编辑、添加字段、恢复默认。
- 抽取流程：`group_page` 按重复表头把每页切分为多个"回单组" → 逐组构造 prompt 调用 LLM（OpenAI 兼容接口，temperature=0，JSON 模式，失败自动重试 1 次）→ `transform_fields` **确定性归一化兜底**（中文大写金额、日期/时间格式、借贷方向、账号去分隔符、币种标准化等）→ 落盘 `extracted.json`。
- 抽取结果前端可编辑并保存；单任务可下载扁平 JSON。

### 4. 批量操作
- 任务列表批量选择（全选行 + 完成任务复选框），侧栏底部手风琴式批量操作区，无弹窗交互。
- **批量抽取**：批量上传规则 + 一键确认抽取；服务端单守护线程**顺序**执行（LLM 调用不宜并发），任务级失败隔离，前端 2s 轮询把"抽取中/成功/失败"叠加显示在任务行上。
- **批量导出**：所选任务打包 zip——识别结果（修正版优先）或抽取结果（扁平格式），各附一份 **Excel 汇总表**（识别：`文件名|页码|字段名|字段值`；抽取：一行一个回单组，跨任务字段取并集）；无抽取结果的任务经 `X-Skipped` 响应头报告跳过数。

### 5. Docker 一键部署

1. 准备环境变量：`cp .env.example .env`，填入 `BAILIAN_API_KEY`（规则抽取需要；仅识别可留空）
2. 启动：`docker compose up -d --build`
3. 访问：`http://localhost:8000`（端口可用 `.env` 的 `BILL_PORT` 修改）

数据与模型缓存全部位于 `/qn-server-data`（容器同名挂载）：重建容器不丢数据、不重下模型。
全新机器（无 cosfs 挂载）上数据目录自动创建，OCR 模型在首次识别时自动下载。
约束：**单 worker 运行**（OCR 模型常驻约 1.5GB）；腾讯镜像源加速构建（约 2 分钟）。

## 项目结构

| 文件 | 职责 |
|------|------|
| `main.py` | 识别管线核心：页面加载、水印/印章擦除、OCR 构建、`parse_page` 结构化 |
| `extract.py` | 抽取核心：默认规则模板、回单分组 `group_page`、prompt 构造、LLM 应答解析、确定性归一化 `transform_fields`、规则校验 `validate_rules` |
| `app.py` | FastAPI Web 服务：任务队列、识别/校正/抽取/批量/导出全部端点、模型配置解析 |
| `bulk.py` | 批量工件：规则 Excel 解析/生成、抽取结果扁平化、zip + Excel 汇总组装 |
| `static/` | 前端（原生 JS 无框架）：任务列表、复原视图、规则抽取工作台、批量操作区 |
| `tests/` | 后端 pytest（38 个用例，经 `BILL_DATA_ROOT` 隔离到临时目录） |
| `input_doc/` | 基准样例票据：东亚银行.jpg、大连银行.png、成都农商行.pdf |
| `docs/compose/` | 批量操作 + Docker 部署的设计规格、实现计划与最终报告 |

## 数据布局

运行时数据全部落在数据根（`BILL_DATA_ROOT`，默认 `/qn-server-data/bill-recognition`）：

```
<DATA_ROOT>/
├── tasks.json                      # 任务索引（重启后恢复任务列表；批量批次不持久化）
├── config.json                     # 抽取模型配置（model / base_url / api_key）
├── tasks/{task_id}/
│   ├── input.{ext}                 # 原始上传件
│   ├── page_{i}.png                # 逐页预览图（擦除前保存）
│   ├── result.json                 # 识别结果（人工保存修正后就地更新）
│   ├── rules.json                  # 该任务的抽取规则
│   └── extracted.json              # 抽取结果 {model, groups:[{page,index,fields}]}
└── output/
    ├── {name}_structured.json      # 自动识别结果副本
    └── {name}_corrected.json       # 人工修正版（存在则下载/导出优先取它）
```

注：cosfs 对刚写入文件存在分钟级负缓存，识别/抽取/规则一律**内存缓存优先、文件兜底**，文件只是持久化副本。

## 运行

```bash
# 生产 / 部署（推荐走上面的 Docker 流程）
docker compose up -d --build

# 本地开发
uv sync
BILL_DATA_ROOT=/tmp/bill-data uv run uvicorn app:app --port 61739   # 单 worker，勿加 --workers

# CLI 单文件识别（无 Web，直读本地文件 → output/*_structured.json）
uv run python main.py

# 测试
uv run pytest tests/ -q
```

### 配置项

| 配置 | 来源与优先级 |
|------|-------------|
| API key | `BAILIAN_API_KEY` 环境变量 > `DATA_ROOT/config.json` > 本机 mimocode auth（仅开发机回退，容器内无此项） |
| 模型 | `BAILIAN_MODEL` > `config.json` > 默认 `deepseek-v4-flash-0731`（DashScope 兼容模式端点，可用 `BAILIAN_BASE_URL` / `config.json` 覆盖） |
| 数据根 | `BILL_DATA_ROOT`（默认 `/qn-server-data/bill-recognition`） |
| Web 端口 | `BILL_PORT`（compose，默认 8000） |

前端"模型设置"区块也可直接改模型/端点（写入 `config.json`）。

## API 摘要

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/upload` | 上传（form：files + dewm + deseal），创建识别任务 |
| GET | `/api/tasks` `/api/tasks/{id}` `/api/tasks/{id}/result` | 任务列表 / 详情 / 识别结果 |
| POST | `/api/tasks/{id}/save` | 保存人工修正（写 corrected） |
| GET | `/api/tasks/{id}/pages/{i}` | 逐页预览图 |
| GET/PUT | `/api/tasks/{id}/rules` | 任务规则读写；另有 `/rules.xlsx`（下载）、`/rules/upload`（上传 .xlsx/.json） |
| GET | `/api/rules-default(.xlsx)` | 默认 21 字段模板（批量区"模板下载"） |
| POST | `/api/batch/rules/upload` | 批量上传规则（file + task_ids） |
| POST/GET | `/api/batch/extract`、`/api/batch/{id}` | 创建批量抽取作业 / 查询进度 |
| POST | `/api/export` | 批量导出 zip（`kind: result \| extracted`） |
| POST/GET/PUT | `/api/tasks/{id}/extract`、`/extracted` | 单任务抽取 / 结果读写 |
| GET/PUT | `/api/extract-config` | 模型配置查看 / 修改 |

## 效果评估指南

供后续对本系统识别/抽取质量做量化评估时参考。系统自身的 38 个 pytest 只保证**功能正确性**，效果评估需按下述方法与人工金标对比。

### 评估对象：两级链路，务必分开度量

```
原图 ──预处理(水印/印章擦除)──▶ OCR ──▶ 结构化 JSON ──▶ [人工校正] ──▶ LLM 规则抽取 ──▶ 回填字段表
        ①字符识别层                ②结构层                  ③分组层                ④字段抽取层
```

- **纯自动链路**用 `*_structured.json`（未经人工修正）与其对应的抽取结果评估；含人工修正的 `*_corrected.json` 只反映"人机协同终态"。批量导出默认修正版优先，评估自动效果时请取 `done` 且 `has_corrected=false` 的任务，或直接读服务端原始 `result.json`。
- 归因建议分层报告：字段错误要先判归因于 OCR 错字、分组错误还是 LLM 抽取错误，否则无法指导优化。

### 评估数据获取

- **批量导出**：UI 勾选任务 → 批量导出。识别结果 zip 内含每任务结构化 JSON + `识别结果汇总.xlsx`（kv 扁平行，便于人工标金标）；抽取结果 zip 内含每任务 `{name}_extracted.json`（`{_meta:{filename,model,extracted_at,groups}, results:[{字段编码:值}]}`，一行一个回单组）+ `抽取结果汇总.xlsx`（一行一个回单组 × 全字段列）。
- 也可直接读 `DATA_ROOT` 下文件（布局见上节）。评估环境建议用 `BILL_DATA_ROOT` 起独立实例，勿动生产数据根。

### 建议指标

| 层 | 指标 | 口径建议 |
|----|------|---------|
| ① 字符识别 | CER / 行准确率 | 以金标文本行为集合，按 bbox IoU≥0.5 匹配后逐行比对；报字符错误率与完全匹配行占比 |
| ② 结构 | 表格检出 P/R、kv 检出 P/R | kv 以 `key:value` 语义对为金标单位；换行断裂、表头误入 kv 均计 FP |
| ③ 分组 | 回单组数一致率 | `extracted.json` 的 groups 数 vs 该票据实际回单数；多切/漏切都要报 |
| ④ 字段 | 逐字段准确率、必填字段完整率、端到端通过率 | 逐字段与金标比对（见下述归一口径）；"端到端通过"= 一条回单的全部必填字段同时正确（真正决定回填可用性）。缺值/`ERROR_MSG` 标注单独归类统计 |

字段比对前的归一口径（与 `transform_fields` 输出一致）：金额转 float 后比较（容忍千分位/符号差异，中文大写折数字）；日期 `YYYYMMDD`、时间 `YYYY-MM-DD HH:MM:SS` 精确匹配；收付方向限枚举"支出/收入"；账号去空格/分隔符后逐字符比较；币种取标准名称。

### 可复现性与已知偏差

- LLM 调用 `temperature=0` + JSON 模式，抽取结果可复现；但**每次评估必须记录所用模型**（存于 `extracted.json.model` 与 `_meta.model`），换模型即换基线。
- 网络失败自动重试 1 次仍失败则该组报"抽取失败"——统计时区分**抽取失败**（无字段产出）与**抽取错误**（有产出但比对不符）。
- 水印/印章擦除按任务开关：对比实验请固定 `dewm`/`deseal`，或专门做一版消融对照。
- 当前回归样例仅 `input_doc/` 三种票据（东亚银行、大连银行、成都农商行），版式覆盖有限；正式效果评估应扩充银行/版式样例集并冻结一份标注金标（建议按"图 × 回单组 × 字段"三元组建表）。
- 单 worker、内存受限环境下批量抽取为顺序执行，大样本评估注意耗时（每组一次 LLM 调用，超时 60s）。

## 相关文档

- 批量操作与 Docker 部署设计规格：`docs/compose/specs/2026-08-24-batch-operations-docker-design.md`
- 最终交付报告（含验证记录与设计决策）：`docs/compose/reports/batch-operations-docker.md`
