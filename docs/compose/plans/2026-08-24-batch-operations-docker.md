# 批量操作与 Docker 一键部署 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use compose:subagent (recommended) or compose:execute to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为票据识别系统增加任务批量选择、批量上传规则模板（Excel 化、按任务隔离）、批量抽取、批量导出（zip + Excel 汇总），以及 Docker 一键部署能力。

**Architecture:** 后端在现有 FastAPI 单文件服务上新增按任务规则存储（`tasks/{id}/rules.json` + 内存缓存）、批量作业（后台线程顺序执行、内存批次注册表）与导出端点（服务端 zipfile + openpyxl 组装）；Excel/zip 纯函数集中在新增 `bulk.py`。前端保持原生 JS，在左侧任务列表内以渐进展开（全选行常驻、勾选后出现手风琴操作区）完成全部批量交互，无弹窗。Docker 用 bind mount 直挂 `/qn-server-data`，复用现有挂载校验与模型缓存软链。

**Tech Stack:** Python 3.12 / FastAPI / uvicorn / openpyxl（新增）/ pytest（dev）/ 原生 JS / Docker + Compose

**规格文档:** `docs/compose/specs/2026-08-24-batch-operations-docker-design.md`（下称 spec，[Sn] 为章节锚点）

## Global Constraints

- 必须单进程单 worker 运行（OCR 模型常驻约 1.5GB，本机 3.6GB 内存会 OOM）——uvicorn 不带 `--workers`
- 数据根默认 `/qn-server-data/bill-recognition`，可由环境变量 `BILL_DATA_ROOT` 覆盖；`os.path.ismount("/qn-server-data")` 启动校验保持不动
- 规则文件一律过 `extract.validate_rules` 校验，错误信息必须带行号（Excel 行号，不是规则序号）
- 识别结果取值顺序：内存缓存优先、文件兜底（cosfs 负缓存）；识别 JSON 导出优先修正版（`_corrected.json`）
- 前端保持原生 JS，不引入任何框架/构建工具；后端仅新增 openpyxl 一个运行时依赖
- 界面文案用中文，与 spec [S3] 一致；样式沿用现有浅色主题 CSS 变量（`--accent`、`--line`、`--paper` 等）
- 测试不得触碰生产数据根：一律通过 `BILL_DATA_ROOT` 指向临时目录

> 规格覆盖说明：[S1]（背景）与 [S2]（现状）为上下文章节，其约束已抄入上方 Global Constraints；[S12]（文件变更清单）为各任务 Files 块的汇总，无独立任务。[S10]（错误边界）由 Task 3/4 的测试用例覆盖，[S11]（测试与验证）分散在各任务的测试/浏览器/Docker 验证步骤中。

---

### Task 1: 测试基建 + bulk.py 规则 Excel 函数

**Covers:** [S5]

**Files:**
- Create: `bulk.py`
- Create: `tests/conftest.py`
- Create: `tests/test_rules_excel.py`
- Modify: `pyproject.toml`（uv add 自动完成）

**Interfaces:**
- Consumes: `extract.validate_rules(payload) -> list[dict]`（失败抛 ValueError，消息形如"第 N 行…"）
- Produces:
  - `bulk.RULE_HEADERS: list[str]` = `["字段编码", "中文名", "必填", "类型", "长度", "回填策略"]`
  - `bulk.load_rules_file(filename: str, data: bytes) -> list[dict]`（validate_rules 规范化后的规则；失败抛 ValueError，Excel 错误已映射为工作表行号）
  - `bulk.rules_to_xlsx(rules: list[dict]) -> bytes`
  - `tests/conftest.py` 提供 `client` fixture（返回 `(TestClient, app模块对象)`，数据根隔离到 tmp）与 `make_task(appmod, name, extracted=None, has_corrected=False) -> (task_id, doc)` 帮助函数

- [ ] **Step 1: 添加依赖**

```bash
uv add openpyxl
uv add --dev pytest
```

- [ ] **Step 2: 写测试基建 `tests/conftest.py`**

```python
import json
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """隔离数据根的 FastAPI 测试客户端。yield (TestClient, app 模块)。"""
    monkeypatch.setenv("BILL_DATA_ROOT", str(tmp_path))
    for mod in list(sys.modules):
        if mod == "app":
            del sys.modules[mod]
    import app as appmod
    from fastapi.testclient import TestClient

    with TestClient(appmod.app) as c:
        yield c, appmod


def make_task(appmod, name, extracted=None, has_corrected=False):
    """直接注入一个 done 状态任务（绕过上传/OCR），返回 (task_id, 识别文档)。"""
    tid = uuid.uuid4().hex[:10]
    task = {
        "id": tid, "filename": f"{name}.pdf", "ext": ".pdf", "status": "done",
        "error": None, "output_name": name, "page_count": 1, "pages_done": 1,
        "dewm": True, "deseal": True, "has_corrected": has_corrected, "created": time.time(),
    }
    appmod.TASKS[tid] = task
    doc = [{
        "page": 0, "size": [100, 100], "tables": [], "text_lines": [],
        "kv": [{"id": "p0k0", "key": "金额", "value": "100.00",
                "box": [0, 0, 10, 10], "line": "p0l0"}],
    }]
    tdir = appmod.TASKS_DIR / tid
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "result.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    (appmod.OUTPUT_DIR / f"{name}_structured.json").write_text(
        json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    appmod.RESULTS[tid] = doc
    if extracted is not None:
        (tdir / "extracted.json").write_text(
            json.dumps(extracted, ensure_ascii=False), encoding="utf-8")
        appmod.EXTRACTED[tid] = extracted
    return tid, doc
```

- [ ] **Step 3: 写失败测试 `tests/test_rules_excel.py`**

```python
import io

import pytest
from openpyxl import Workbook

import bulk

HEADER = bulk.RULE_HEADERS


def make_xlsx(rows):
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_parse_valid_xlsx():
    data = make_xlsx([
        HEADER,
        ["TX001", "交易流水号", "必填", "字符", 120, "原样保留"],
        [],
        ["TX002", "金额", "", "浮点", "", ""],
    ])
    rules = bulk.load_rules_file("rules.xlsx", data)
    assert [r["code"] for r in rules] == ["TX001", "TX002"]
    assert rules[0]["required"] == "必填"
    assert rules[0]["length"] == 120
    assert rules[1]["required"] == "可选"
    assert rules[1]["length"] is None


def test_bad_header():
    data = make_xlsx([["编码", "名称"], ["a", "b"]])
    with pytest.raises(ValueError, match="表头"):
        bulk.load_rules_file("rules.xlsx", data)


def test_empty_code_reports_sheet_row():
    # 表头=第1行，合法行=第2行，空行=第3行，坏行=第4行
    data = make_xlsx([
        HEADER,
        ["TX001", "流水号", "必填", "字符", "", ""],
        [],
        ["", "空编码", "必填", "字符", "", ""],
    ])
    with pytest.raises(ValueError, match="第 4 行"):
        bulk.load_rules_file("rules.xlsx", data)


def test_roundtrip():
    rules = [{"code": "A", "name": "甲", "required": "必填",
              "type": "字符", "length": 10, "strategy": "s"}]
    parsed = bulk.load_rules_file("r.xlsx", bulk.rules_to_xlsx(rules))
    assert parsed == rules


def test_load_json():
    data = b'{"rules": [{"code": "A", "name": "甲", "required": "必填", "type": "字符", "length": null, "strategy": ""}]}'
    rules = bulk.load_rules_file("r.json", data)
    assert rules[0]["code"] == "A"


def test_unsupported_ext():
    with pytest.raises(ValueError, match="仅支持"):
        bulk.load_rules_file("r.txt", b"x")
```

- [ ] **Step 4: 运行测试确认失败**

Run: `uv run pytest tests/test_rules_excel.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'bulk'`）

- [ ] **Step 5: 实现 `bulk.py`（规则部分）**

```python
# 规则文件 Excel 解析/生成与批量导出文件组装。
import io
import json
import re
import zipfile
from datetime import datetime, timezone

from openpyxl import Workbook, load_workbook

from extract import validate_rules

RULE_HEADERS = ["字段编码", "中文名", "必填", "类型", "长度", "回填策略"]
_ROW_RE = re.compile(r"第 (\d+) 行")


def _cell_str(c):
    if c is None:
        return ""
    if isinstance(c, float) and c.is_integer():
        return str(int(c))
    return str(c).strip()


def parse_rules_xlsx(data: bytes):
    """解析规则 Excel。返回 (行字典列表, 对应工作表行号)；结构错误抛 ValueError。"""
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True)
    except Exception as e:  # noqa: BLE001 - 任何坏文件统一报校验错误
        raise ValueError(f"Excel 解析失败：{e}") from None
    rows = list(wb.active.iter_rows(values_only=True))
    wb.close()
    if not rows:
        raise ValueError("Excel 为空")
    header = [_cell_str(c) for c in rows[0]][:6]
    if header != RULE_HEADERS:
        raise ValueError(f"表头应为：{' | '.join(RULE_HEADERS)}")
    rules, row_map = [], []
    for i, row in enumerate(rows[1:], start=2):
        cells = ([_cell_str(c) for c in row] + [""] * 6)[:6]
        if not any(cells):
            continue
        rules.append({
            "code": cells[0], "name": cells[1], "required": cells[2] or "可选",
            "type": cells[3], "length": cells[4] or None, "strategy": cells[5],
        })
        row_map.append(i)
    if not rules:
        raise ValueError("Excel 中没有规则行")
    return rules, row_map


def _remap_row_errors(msg: str, row_map: list[int]) -> str:
    def repl(m):
        idx = int(m.group(1)) - 1
        return f"第 {row_map[idx]} 行" if 0 <= idx < len(row_map) else m.group(0)
    return _ROW_RE.sub(repl, msg, count=1)


def load_rules_file(filename: str, data: bytes) -> list[dict]:
    """按扩展名解析 .xlsx/.json 规则文件，返回 validate_rules 规范化结果；失败抛 ValueError。"""
    name = filename.lower()
    if name.endswith(".xlsx"):
        rules, row_map = parse_rules_xlsx(data)
        try:
            return validate_rules(rules)
        except ValueError as e:
            raise ValueError(_remap_row_errors(str(e), row_map)) from None
    if name.endswith(".json"):
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ValueError(f"JSON 解析失败：{e}") from None
        if isinstance(payload, dict) and isinstance(payload.get("rules"), list):
            payload = payload["rules"]
        return validate_rules(payload)
    raise ValueError("仅支持 .xlsx 或 .json 规则文件")


def rules_to_xlsx(rules: list[dict]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "规则"
    ws.append(RULE_HEADERS)
    for r in rules:
        ws.append([r["code"], r["name"], r["required"], r["type"],
                   r["length"] if r.get("length") is not None else "",
                   r.get("strategy", "")])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
```

- [ ] **Step 6: 运行测试确认通过**

Run: `uv run pytest tests/test_rules_excel.py -v`
Expected: 6 passed

- [ ] **Step 7: 提交**

```bash
git add bulk.py tests/conftest.py tests/test_rules_excel.py pyproject.toml uv.lock
git commit -m "Add rule Excel parsing/generation with isolated test scaffolding"
```

---

### Task 2: 按任务规则端点（隔离 + Excel 上传下载 + 移除全局模板）

**Covers:** [S4, S5, S8]

**Files:**
- Modify: `app.py`（新增 `RULES` 缓存与规则端点、移除 `/api/rule-template` 三端点与 `_load_rule_template`、`extract_task` 改用任务规则并抽出 `_run_extract`）
- Test: `tests/test_task_rules.py`

**Interfaces:**
- Consumes: `bulk.load_rules_file`、`bulk.rules_to_xlsx`（Task 1）；`conftest.make_task`
- Produces:
  - `app.RULES: dict[str, list]`、`app._get_rules(task_id) -> list`、`app._seed_rules() -> list`
  - `app._run_extract(task: dict, rules: list, api_key: str, base_url: str, model: str) -> dict`（写 `extracted.json` + `EXTRACTED` 缓存；LLM 失败抛 RuntimeError 带"第 N 页第 M 组"）
  - `app.XLSX_MT = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"`
  - 端点：`GET/PUT /api/tasks/{id}/rules`、`POST /api/tasks/{id}/rules/upload`、`GET /api/tasks/{id}/rules.xlsx`、`GET /api/rules-default`、`GET /api/rules-default.xlsx`

- [ ] **Step 1: 写失败测试 `tests/test_task_rules.py`**

```python
import io

from conftest import make_task
from openpyxl import Workbook, load_workbook

import bulk

XLSX_MT = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def xlsx_bytes(rows):
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_rules_seed_default(client):
    c, appmod = client
    tid, _ = make_task(appmod, "种子任务")
    resp = c.get(f"/api/tasks/{tid}/rules")
    assert resp.status_code == 200
    assert len(resp.json()["rules"]) == 21


def test_rules_seed_from_legacy_global(client, tmp_path):
    c, appmod = client
    (tmp_path / "rule_template.json").write_text(
        '{"rules": [{"code": "OLD1", "name": "旧字段", "required": "必填",'
        ' "type": "字符", "length": null, "strategy": ""}]}',
        encoding="utf-8")
    tid, _ = make_task(appmod, "迁移任务")
    rules = c.get(f"/api/tasks/{tid}/rules").json()["rules"]
    assert [r["code"] for r in rules] == ["OLD1"]


def test_put_rules_persists(client):
    c, appmod = client
    tid, _ = make_task(appmod, "保存规则")
    payload = {"rules": [{"code": "A1", "name": "甲", "required": "必填",
                          "type": "字符", "length": None, "strategy": ""}]}
    assert c.put(f"/api/tasks/{tid}/rules", json=payload).status_code == 200
    assert (appmod.TASKS_DIR / tid / "rules.json").exists()
    appmod.RULES.clear()  # 强制走文件读取
    assert c.get(f"/api/tasks/{tid}/rules").json()["rules"][0]["code"] == "A1"


def test_put_rules_duplicate_code(client):
    c, appmod = client
    tid, _ = make_task(appmod, "重复编码")
    rule = {"code": "A1", "name": "甲", "required": "必填", "type": "字符",
            "length": None, "strategy": ""}
    resp = c.put(f"/api/tasks/{tid}/rules", json={"rules": [rule, dict(rule)]})
    assert resp.status_code == 422


def test_upload_rules_xlsx(client):
    c, appmod = client
    tid, _ = make_task(appmod, "上传规则")
    data = xlsx_bytes([bulk.RULE_HEADERS, ["U1", "上传字段", "必填", "字符", "", ""]])
    resp = c.post(f"/api/tasks/{tid}/rules/upload",
                  files={"file": ("r.xlsx", data, XLSX_MT)})
    assert resp.status_code == 200
    assert c.get(f"/api/tasks/{tid}/rules").json()["rules"][0]["code"] == "U1"


def test_upload_rules_bad_header(client):
    c, appmod = client
    tid, _ = make_task(appmod, "坏表头")
    data = xlsx_bytes([["编码"], ["x"]])
    resp = c.post(f"/api/tasks/{tid}/rules/upload",
                  files={"file": ("r.xlsx", data, XLSX_MT)})
    assert resp.status_code == 422


def test_download_rules_xlsx(client):
    c, appmod = client
    tid, _ = make_task(appmod, "下载规则")
    resp = c.get(f"/api/tasks/{tid}/rules.xlsx")
    assert resp.status_code == 200
    wb = load_workbook(io.BytesIO(resp.content))
    ws = wb.active
    assert ws["A1"].value == "字段编码"
    assert ws.max_row == 22  # 表头 + 21 默认字段


def test_rules_default_endpoints(client):
    c, _ = client
    assert len(c.get("/api/rules-default").json()["rules"]) == 21
    assert c.get("/api/rules-default.xlsx").status_code == 200


def test_global_template_endpoints_removed(client):
    c, _ = client
    assert c.get("/api/rule-template").status_code == 404


def test_extract_uses_task_rules(client, monkeypatch):
    c, appmod = client
    monkeypatch.setenv("BAILIAN_API_KEY", "test-key")
    calls = []

    def fake_llm(system, user, api_key, base_url, model):
        calls.append((system, user))
        return '{"fields": {}}'

    monkeypatch.setattr(appmod, "_call_llm", fake_llm)
    tid, _ = make_task(appmod, "按任务规则抽取")
    c.put(f"/api/tasks/{tid}/rules", json={"rules": [
        {"code": "MYFIELD", "name": "我的字段", "required": "必填",
         "type": "字符", "length": None, "strategy": ""}]})
    resp = c.post(f"/api/tasks/{tid}/extract", json={})
    assert resp.status_code == 200
    assert any("MYFIELD" in s + u for s, u in calls)
    assert (appmod.TASKS_DIR / tid / "extracted.json").exists()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_task_rules.py -v`
Expected: FAIL（404/405，端点不存在）

- [ ] **Step 3: 修改 `app.py`**

3a. 顶部导入区：`from fastapi.responses import ...` 下新增一行 `import bulk`（放在 `from extract import (...)` 之后）。在 `EXTRACTED: dict[str, dict] = {}` 行后新增：

```python
RULES: dict[str, list] = {}
XLSX_MT = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
```

3b. 删除 `_load_rule_template` 函数与 `get_rule_template` / `put_rule_template` / `delete_rule_template` 三个端点（原 app.py:367-371、449-470）。

3c. 在 `# ---------- 按规则抽取 ----------` 区块内、`_read_json` 之后加入：

```python
def _seed_rules() -> list:
    # 兼容迁移：优先沿用旧全局模板文件，之后各任务独立演化
    data = _read_json(RULE_TEMPLATE_PATH)
    if isinstance(data, dict) and isinstance(data.get("rules"), list) and data["rules"]:
        try:
            return validate_rules(data["rules"])
        except ValueError:
            pass
    return DEFAULT_RULE_TEMPLATE


def _get_rules(task_id: str) -> list:
    if task_id in RULES:
        return RULES[task_id]
    data = _read_json(TASKS_DIR / task_id / "rules.json")
    if isinstance(data, dict) and isinstance(data.get("rules"), list):
        RULES[task_id] = data["rules"]
        return data["rules"]
    return _seed_rules()


@app.get("/api/tasks/{task_id}/rules")
def get_task_rules(task_id: str):
    _get_task(task_id)
    return {"rules": _get_rules(task_id)}


@app.put("/api/tasks/{task_id}/rules")
def put_task_rules(task_id: str, payload: dict = Body(...)):
    task = _get_task(task_id)
    if task["status"] != "done":
        raise HTTPException(422, "任务未完成，无法保存规则")
    try:
        rules = validate_rules(payload.get("rules"))
    except ValueError as e:
        raise HTTPException(422, str(e))
    _write_json(TASKS_DIR / task_id / "rules.json", {"rules": rules})
    RULES[task_id] = rules
    return {"saved": len(rules)}


@app.post("/api/tasks/{task_id}/rules/upload")
async def upload_task_rules(task_id: str, file: UploadFile):
    task = _get_task(task_id)
    if task["status"] != "done":
        raise HTTPException(422, "任务未完成，无法保存规则")
    data = await file.read()
    try:
        rules = bulk.load_rules_file(file.filename or "", data)
    except ValueError as e:
        raise HTTPException(422, str(e))
    _write_json(TASKS_DIR / task_id / "rules.json", {"rules": rules})
    RULES[task_id] = rules
    return {"saved": len(rules)}


@app.get("/api/tasks/{task_id}/rules.xlsx")
def download_task_rules_xlsx(task_id: str):
    task = _get_task(task_id)
    body = bulk.rules_to_xlsx(_get_rules(task_id))
    name = f"{task['output_name']}_rules.xlsx"
    return Response(
        body, media_type=XLSX_MT,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
    )


@app.get("/api/rules-default")
def get_rules_default():
    return {"rules": DEFAULT_RULE_TEMPLATE}


@app.get("/api/rules-default.xlsx")
def get_rules_default_xlsx():
    body = bulk.rules_to_xlsx(DEFAULT_RULE_TEMPLATE)
    return Response(
        body, media_type=XLSX_MT,
        headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote("规则模板.xlsx")},
    )
```

3d. 把 `extract_task` 端点重写为（抽出 `_run_extract` 供 Task 3 批量复用；规则来源改为 payload 覆盖或任务规则）：

```python
def _run_extract(task: dict, rules: list, api_key: str, base_url: str, model: str) -> dict:
    task_id = task["id"]
    if task_id in RESULTS:
        doc = RESULTS[task_id]
    else:
        result_path = TASKS_DIR / task_id / "result.json"
        if not result_path.exists():
            raise FileNotFoundError("结果未就绪")
        doc = json.loads(result_path.read_text(encoding="utf-8"))
    out_groups = []
    for pi, page in enumerate(doc):
        for gi, group in enumerate(group_page(page)):
            system, user = build_prompt(rules, group, task["filename"])
            try:
                text = _call_llm(system, user, api_key, base_url, model)
                raw = parse_llm_response(text)
            except (RuntimeError, json.JSONDecodeError) as e:
                raise RuntimeError(f"第 {pi + 1} 页第 {gi + 1} 组：{e}") from None
            fields = transform_fields(raw, rules, group, task["filename"])
            out_groups.append({"page": pi, "index": gi, "fields": fields})
    data = {"model": model, "groups": out_groups}
    _write_json(TASKS_DIR / task_id / "extracted.json", data)
    EXTRACTED[task_id] = data
    return data


@app.post("/api/tasks/{task_id}/extract")
def extract_task(task_id: str, payload: dict | None = Body(default=None)):
    task = _get_task(task_id)
    if task["status"] != "done":
        raise HTTPException(404, "结果未就绪")
    api_key = _resolve_api_key()
    if not api_key:
        raise HTTPException(
            503,
            "未配置百炼 API key：请设置环境变量 BAILIAN_API_KEY，"
            "或在数据根 config.json 中填写 api_key",
        )
    if payload and payload.get("rules"):
        try:
            rules = validate_rules(payload["rules"])
        except ValueError as e:
            raise HTTPException(422, str(e))
    else:
        rules = _get_rules(task_id)
    try:
        return _run_extract(task, rules, api_key, _resolve_base_url(), _resolve_model())
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except (RuntimeError, json.JSONDecodeError) as e:
        raise HTTPException(502, f"抽取失败：{e}")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/ -v`
Expected: 全部通过（含 Task 1 的 6 个）

- [ ] **Step 5: 提交**

```bash
git add app.py tests/test_task_rules.py
git commit -m "Isolate extraction rules per task with Excel upload/download"
```

---

### Task 3: 批量上传规则 + 批量抽取作业

**Covers:** [S6, S8, S10]

**Files:**
- Modify: `app.py`（新增 `BATCH` 注册表、`/api/batch/rules/upload`、`/api/batch/extract`、`/api/batch/{batch_id}`、`_batch_worker`）
- Test: `tests/test_batch.py`

**Interfaces:**
- Consumes: `app._run_extract`、`app._get_rules`（Task 2）；`bulk.load_rules_file`（Task 1）
- Produces:
  - `app.BATCH: dict[str, dict]`（`{"status": "running"|"done", "items": {task_id: {"task_id","status","error"}}}`）
  - `POST /api/batch/rules/upload`（multipart：`file` + `task_ids` 逗号分隔）→ `{"updated": N}`
  - `POST /api/batch/extract {task_ids: [...]}` → `{"batch_id"}`；422 非 done / 503 无 key / 409 已有批次
  - `GET /api/batch/{batch_id}` → `{"status", "items": [...]}`

- [ ] **Step 1: 写失败测试 `tests/test_batch.py`**

```python
import io
import time

from conftest import make_task
from openpyxl import Workbook

import bulk

XLSX_MT = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def xlsx_bytes(rows):
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def wait_batch(c, appmod, batch_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = c.get(f"/api/batch/{batch_id}").json()
        if data["status"] == "done":
            return data
        time.sleep(0.05)
    raise AssertionError("批量作业超时")


def test_batch_rules_upload(client):
    c, appmod = client
    t1, _ = make_task(appmod, "批量甲")
    t2, _ = make_task(appmod, "批量乙")
    data = xlsx_bytes([bulk.RULE_HEADERS, ["B1", "批字段", "必填", "字符", "", ""]])
    resp = c.post("/api/batch/rules/upload",
                  data={"task_ids": f"{t1},{t2}"},
                  files={"file": ("r.xlsx", data, XLSX_MT)})
    assert resp.json()["updated"] == 2
    assert c.get(f"/api/tasks/{t1}/rules").json()["rules"][0]["code"] == "B1"
    assert c.get(f"/api/tasks/{t2}/rules").json()["rules"][0]["code"] == "B1"


def test_batch_rules_upload_invalid_keeps_old(client):
    c, appmod = client
    t1, _ = make_task(appmod, "校验优先")
    before = c.get(f"/api/tasks/{t1}/rules").json()["rules"]
    bad = xlsx_bytes([["坏表头"], ["x"]])
    resp = c.post("/api/batch/rules/upload",
                  data={"task_ids": t1},
                  files={"file": ("r.xlsx", bad, XLSX_MT)})
    assert resp.status_code == 422
    assert c.get(f"/api/tasks/{t1}/rules").json()["rules"] == before


def test_batch_extract_runs(client, monkeypatch):
    c, appmod = client
    monkeypatch.setenv("BAILIAN_API_KEY", "k")
    done = []

    def fake_run(task, rules, api_key, base_url, model):
        done.append(task["id"])
        data = {"model": model, "groups": []}
        appmod.EXTRACTED[task["id"]] = data
        return data

    monkeypatch.setattr(appmod, "_run_extract", fake_run)
    t1, _ = make_task(appmod, "批抽一")
    t2, _ = make_task(appmod, "批抽二")
    resp = c.post("/api/batch/extract", json={"task_ids": [t1, t2]})
    assert resp.status_code == 200
    data = wait_batch(c, appmod, resp.json()["batch_id"])
    assert {i["status"] for i in data["items"]} == {"done"}
    assert set(done) == {t1, t2}


def test_batch_extract_failure_recorded(client, monkeypatch):
    c, appmod = client
    monkeypatch.setenv("BAILIAN_API_KEY", "k")

    def fake_run(task, rules, api_key, base_url, model):
        raise RuntimeError("boom")

    monkeypatch.setattr(appmod, "_run_extract", fake_run)
    t1, _ = make_task(appmod, "失败任务")
    batch_id = c.post("/api/batch/extract", json={"task_ids": [t1]}).json()["batch_id"]
    data = wait_batch(c, appmod, batch_id)
    item = data["items"][0]
    assert item["status"] == "failed"
    assert "boom" in item["error"]


def test_batch_extract_rejects_non_done(client, monkeypatch):
    c, appmod = client
    monkeypatch.setenv("BAILIAN_API_KEY", "k")
    tid, _ = make_task(appmod, "进行中")
    appmod.TASKS[tid]["status"] = "processing"
    assert c.post("/api/batch/extract", json={"task_ids": [tid]}).status_code == 422


def test_batch_extract_conflict(client, monkeypatch):
    c, appmod = client
    monkeypatch.setenv("BAILIAN_API_KEY", "k")
    appmod.BATCH["fake"] = {"status": "running", "items": {}}
    try:
        tid, _ = make_task(appmod, "冲突")
        assert c.post("/api/batch/extract", json={"task_ids": [tid]}).status_code == 409
    finally:
        appmod.BATCH.pop("fake", None)


def test_batch_extract_no_key(client, monkeypatch):
    c, appmod = client
    monkeypatch.delenv("BAILIAN_API_KEY", raising=False)
    monkeypatch.setattr(appmod, "MIMO_AUTH_PATH", appmod.DATA_ROOT / "noauth.json")
    tid, _ = make_task(appmod, "无key")
    assert c.post("/api/batch/extract", json={"task_ids": [tid]}).status_code == 503
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_batch.py -v`
Expected: FAIL（404/405，端点不存在）

- [ ] **Step 3: 修改 `app.py`**

在 `RULES` 定义附近新增批次注册表：

```python
BATCH: dict[str, dict] = {}
BATCH_LOCK = threading.Lock()
```

在文件末尾 `app.mount(...)` 之前新增批量区块：

```python
# ---------- 批量操作 ----------


@app.post("/api/batch/rules/upload")
async def batch_rules_upload(file: UploadFile, task_ids: str = Form(...)):
    ids = [i for i in task_ids.split(",") if i.strip()]
    tasks = []
    for tid in ids:
        t = TASKS.get(tid)
        if t is None or t["status"] != "done":
            raise HTTPException(422, f"任务不存在或未完成：{tid}")
        tasks.append(t)
    if not tasks:
        raise HTTPException(422, "未选择任务")
    data = await file.read()
    try:
        rules = bulk.load_rules_file(file.filename or "", data)
    except ValueError as e:
        raise HTTPException(422, str(e))
    for t in tasks:
        _write_json(TASKS_DIR / t["id"] / "rules.json", {"rules": rules})
        RULES[t["id"]] = rules
    return {"updated": len(tasks)}


@app.post("/api/batch/extract")
def batch_extract(payload: dict = Body(...)):
    ids = payload.get("task_ids") or []
    if not ids:
        raise HTTPException(422, "未选择任务")
    tasks, bad = [], []
    for tid in ids:
        t = TASKS.get(tid)
        if t is None or t["status"] != "done":
            bad.append(str(tid))
        else:
            tasks.append(t)
    if bad:
        raise HTTPException(422, f"以下任务未完成，无法抽取：{'、'.join(bad)}")
    if not _resolve_api_key():
        raise HTTPException(503, "未配置百炼 API key")
    with BATCH_LOCK:
        if any(b["status"] == "running" for b in BATCH.values()):
            raise HTTPException(409, "已有批量抽取进行中")
        batch_id = uuid.uuid4().hex[:10]
        BATCH[batch_id] = {
            "status": "running",
            "items": {t["id"]: {"task_id": t["id"], "status": "pending", "error": None}
                      for t in tasks},
        }
    threading.Thread(target=_batch_worker, args=(batch_id, tasks), daemon=True).start()
    return {"batch_id": batch_id}


def _batch_worker(batch_id: str, tasks: list):
    batch = BATCH[batch_id]
    api_key = _resolve_api_key()
    base_url, model = _resolve_base_url(), _resolve_model()
    for t in tasks:
        item = batch["items"][t["id"]]
        item["status"] = "processing"
        try:
            _run_extract(t, _get_rules(t["id"]), api_key, base_url, model)
            item["status"] = "done"
        except Exception as e:  # noqa: BLE001 - 任务级兜底
            item["status"] = "failed"
            item["error"] = f"{type(e).__name__}: {e}"
    batch["status"] = "done"


@app.get("/api/batch/{batch_id}")
def get_batch(batch_id: str):
    batch = BATCH.get(batch_id)
    if batch is None:
        raise HTTPException(404, "批次不存在")
    return {"status": batch["status"], "items": list(batch["items"].values())}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/ -v`
Expected: 全部通过

- [ ] **Step 5: 提交**

```bash
git add app.py tests/test_batch.py
git commit -m "Add batch rule upload and sequential batch extraction job"
```

---

### Task 4: 批量导出（zip + Excel 汇总）

**Covers:** [S7, S8, S10]

**Files:**
- Modify: `bulk.py`（新增导出组装函数）
- Modify: `app.py`（`_public` 增加 `has_extracted`、`_result_doc`、`POST /api/export`）
- Test: `tests/test_export.py`

**Interfaces:**
- Consumes: `conftest.make_task`；`app.RESULTS/OUTPUT_DIR/EXTRACTED`
- Produces:
  - `bulk.flatten_extracted(data: dict, filename: str) -> dict`（`{_meta: {filename, model, extracted_at, groups}, results: [{code: value}]}`，与前端单任务下载一致）
  - `bulk.result_summary_xlsx(rows: list[dict]) -> bytes`（rows 键：filename/page/key/value）
  - `bulk.extracted_summary_xlsx(rows: list[dict], columns: list[dict]) -> bytes`（rows 键：filename/group/page + 各字段编码；columns：`[{"code","name"}]`）
  - `bulk.build_zip(entries: list[tuple[str, bytes]]) -> bytes`
  - `app._result_doc(t) -> (doc, "corrected"|"structured")`
  - `POST /api/export {task_ids, kind}` → zip；kind=extracted 时响应头 `X-Skipped`
  - `_public` 输出新增 `has_extracted: bool`

- [ ] **Step 1: 写失败测试 `tests/test_export.py`**

```python
import io
import json
import zipfile

from conftest import make_task
from openpyxl import load_workbook

EXTRACTED_SAMPLE = {
    "model": "m1",
    "groups": [{
        "page": 0, "index": 0,
        "fields": [
            {"code": "tran_amt", "name": "金额", "value": "100.00",
             "required": "必填", "source_ids": []},
            {"code": "bankseqno", "name": "银行交易流水号", "value": "N1",
             "required": "必填", "source_ids": []},
        ],
    }],
}


def zip_names(body):
    return set(zipfile.ZipFile(io.BytesIO(body)).namelist())


def zip_read(body, name):
    return zipfile.ZipFile(io.BytesIO(body)).read(name)


def test_export_result_zip(client):
    c, appmod = client
    t1, _ = make_task(appmod, "导出甲")
    t2, _ = make_task(appmod, "导出乙")
    resp = c.post("/api/export", json={"task_ids": [t1, t2], "kind": "result"})
    assert resp.status_code == 200
    assert zip_names(resp.content) == {
        "导出甲_structured.json", "导出乙_structured.json", "识别结果汇总.xlsx"}
    ws = load_workbook(io.BytesIO(zip_read(resp.content, "识别结果汇总.xlsx"))).active
    assert [r[0] for r in ws.iter_rows(min_row=2, values_only=True)] == ["导出甲.pdf", "导出乙.pdf"]


def test_export_result_prefers_corrected(client):
    c, appmod = client
    tid, doc = make_task(appmod, "修正任务", has_corrected=True)
    corrected = [dict(doc[0])]
    corrected[0]["kv"] = [dict(doc[0]["kv"][0], value="999")]
    appmod.RESULTS[tid] = corrected
    resp = c.post("/api/export", json={"task_ids": [tid], "kind": "result"})
    body = resp.content
    assert "修正任务_corrected.json" in zip_names(body)
    saved = json.loads(zip_read(body, "修正任务_corrected.json"))
    assert saved[0]["kv"][0]["value"] == "999"


def test_export_extracted_zip_and_skip(client):
    c, appmod = client
    t1, _ = make_task(appmod, "有抽取", extracted=EXTRACTED_SAMPLE)
    t2, _ = make_task(appmod, "无抽取")
    resp = c.post("/api/export", json={"task_ids": [t1, t2], "kind": "extracted"})
    assert resp.status_code == 200
    assert resp.headers["X-Skipped"] == "1"
    flat = json.loads(zip_read(resp.content, "有抽取_extracted.json"))
    assert flat["_meta"]["filename"] == "有抽取.pdf"
    assert flat["results"] == [{"tran_amt": "100.00", "bankseqno": "N1"}]
    ws = load_workbook(io.BytesIO(zip_read(resp.content, "抽取结果汇总.xlsx"))).active
    header = [c2.value for c2 in ws[1]]
    assert header == ["文件名", "组序号", "页码", "金额", "银行交易流水号"]
    row = [c2.value for c2 in ws[2]]
    assert row == ["有抽取.pdf", 1, 1, "100.00", "N1"]


def test_export_extracted_all_skipped(client):
    c, appmod = client
    tid, _ = make_task(appmod, "都没抽取")
    resp = c.post("/api/export", json={"task_ids": [tid], "kind": "extracted"})
    assert resp.status_code == 422


def test_export_no_done_tasks(client):
    c, appmod = client
    resp = c.post("/api/export", json={"task_ids": ["nonexist"], "kind": "result"})
    assert resp.status_code == 422


def test_tasks_list_has_extracted(client):
    c, appmod = client
    t1, _ = make_task(appmod, "未抽取")
    t2, _ = make_task(appmod, "已抽取", extracted=EXTRACTED_SAMPLE)
    tasks = {t["id"]: t for t in c.get("/api/tasks").json()["tasks"]}
    assert tasks[t1]["has_extracted"] is False
    assert tasks[t2]["has_extracted"] is True
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_export.py -v`
Expected: FAIL（404/422 与缺字段）

- [ ] **Step 3: 扩展 `bulk.py`**

在文件末尾追加：

```python
def flatten_extracted(data: dict, filename: str) -> dict:
    """存储格式 {model, groups} → 前端单任务下载同款 {_meta, results} 扁平格式。"""
    results = []
    for g in data.get("groups", []):
        obj = {}
        for f in g.get("fields", []):
            obj[f.get("code", "")] = f.get("value", "")
        results.append(obj)
    return {
        "_meta": {
            "filename": filename,
            "model": data.get("model", ""),
            "extracted_at": datetime.now(timezone.utc).isoformat(),
            "groups": len(results),
        },
        "results": results,
    }


def result_summary_xlsx(rows: list[dict]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "识别结果汇总"
    ws.append(["文件名", "页码", "字段名", "字段值"])
    for r in rows:
        ws.append([r["filename"], r["page"], r["key"], r["value"]])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def extracted_summary_xlsx(rows: list[dict], columns: list[dict]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "抽取结果汇总"
    ws.append(["文件名", "组序号", "页码"] + [c["name"] or c["code"] for c in columns])
    for r in rows:
        ws.append([r["filename"], r["group"], r["page"]]
                  + [r.get(c["code"], "") for c in columns])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_zip(entries: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries:
            zf.writestr(name, content)
    return buf.getvalue()
```

- [ ] **Step 4: 修改 `app.py`**

4a. `_public` 增加 `has_extracted`：

```python
def _public(t: dict) -> dict:
    tid = t.get("id")
    pub = {k: t.get(k) for k in (
        "id", "filename", "status", "error", "output_name",
        "page_count", "pages_done", "created",
    )}
    pub["has_extracted"] = tid in EXTRACTED or (TASKS_DIR / tid / "extracted.json").exists()
    return pub
```

4b. 在批量区块内新增：

```python
def _result_doc(t: dict):
    """(识别文档, corrected|structured)；取值顺序与单任务下载一致，内存优先。"""
    corrected = OUTPUT_DIR / f"{t['output_name']}_corrected.json"
    if t.get("has_corrected") or corrected.exists():
        if t["id"] in RESULTS:
            return RESULTS[t["id"]], "corrected"
        if corrected.exists():
            return json.loads(corrected.read_text(encoding="utf-8")), "corrected"
    structured = OUTPUT_DIR / f"{t['output_name']}_structured.json"
    return json.loads(structured.read_text(encoding="utf-8")), "structured"


@app.post("/api/export")
def export_batch(payload: dict = Body(...)):
    kind = payload.get("kind")
    if kind not in ("result", "extracted"):
        raise HTTPException(422, "kind 必须为 result 或 extracted")
    ids = payload.get("task_ids") or []
    tasks = [TASKS[i] for i in ids if i in TASKS and TASKS[i]["status"] == "done"]
    if not tasks:
        raise HTTPException(422, "没有可导出的完成任务")
    if kind == "result":
        entries, rows = [], []
        for t in tasks:
            doc, suffix = _result_doc(t)
            entries.append((
                f"{t['output_name']}_{suffix}.json",
                json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8"),
            ))
            for page in doc:
                for k in page.get("kv", []):
                    rows.append({"filename": t["filename"],
                                 "page": page.get("page", 0) + 1,
                                 "key": k.get("key", ""), "value": k.get("value", "")})
        entries.append(("识别结果汇总.xlsx", bulk.result_summary_xlsx(rows)))
        return Response(
            bulk.build_zip(entries), media_type="application/zip",
            headers={"Content-Disposition":
                     "attachment; filename*=UTF-8''" + quote("识别结果.zip")},
        )
    entries, rows, skipped = [], [], 0
    columns: dict[str, str] = {}
    for t in tasks:
        data = EXTRACTED.get(t["id"]) or _read_json(TASKS_DIR / t["id"] / "extracted.json")
        if not isinstance(data, dict) or not isinstance(data.get("groups"), list):
            skipped += 1
            continue
        entries.append((
            f"{t['output_name']}_extracted.json",
            json.dumps(bulk.flatten_extracted(data, t["filename"]),
                       ensure_ascii=False, indent=2).encode("utf-8"),
        ))
        for g in data["groups"]:
            row = {"filename": t["filename"],
                   "group": g.get("index", 0) + 1, "page": g.get("page", 0) + 1}
            for f in g.get("fields", []):
                code = f.get("code", "")
                row[code] = f.get("value", "")
                columns.setdefault(code, f.get("name") or code)
            rows.append(row)
    if not entries:
        raise HTTPException(422, "所选任务均无抽取结果")
    entries.append(("抽取结果汇总.xlsx", bulk.extracted_summary_xlsx(
        rows, [{"code": c, "name": n} for c, n in columns.items()])))
    return Response(
        bulk.build_zip(entries), media_type="application/zip",
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote("抽取结果.zip"),
            "X-Skipped": str(skipped),
        },
    )
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/ -v`
Expected: 全部通过

- [ ] **Step 6: 提交**

```bash
git add bulk.py app.py tests/test_export.py
git commit -m "Add batch export with zip packaging and Excel summaries"
```

---

### Task 5: 前端——批量选择 UI + 规则页签按任务化

**Covers:** [S3, S4, S5, S11]

**Files:**
- Modify: `static/index.html`（侧栏结构：全选行、批量操作区骨架、隐藏文件输入）
- Modify: `static/app.js`（选择状态、渲染、手风琴、规则页签按任务加载/上传/下载/恢复默认）
- Modify: `static/style.css`（侧栏批量区样式）

**Interfaces:**
- Consumes: `GET /api/tasks`（含 `has_extracted`）、`GET/PUT /api/tasks/{id}/rules`、`POST /api/tasks/{id}/rules/upload`、`GET /api/tasks/{id}/rules.xlsx`、`GET /api/rules-default`（Task 2/4）
- Produces: `state.selected: Set`、`state.batch = {id, timer, overlay}`、`renderBatchPanel()`、`toggleBatchBody(which)`——Task 6 在此基础上挂批量动作处理器

- [ ] **Step 1: 修改 `static/index.html` 侧栏**

`<aside class="sidebar">` 整体替换为：

```html
  <aside class="sidebar">
    <div class="sidebar-title">任务列表</div>
    <label class="select-all"><input id="selectAll" type="checkbox"> 全选</label>
    <ul id="taskList" class="task-list"></ul>
    <div id="taskEmpty" class="side-empty" hidden>暂无任务，点击右上角上传</div>
    <div id="batchPanel" class="batch-panel" hidden>
      <div class="batch-head">
        <span>已选 <span id="batchCount">0</span> 个</span>
        <button id="batchClear" class="btn btn-ghost">取消选择</button>
      </div>
      <button id="batchExtractHead" class="batch-acc-head">批量抽取</button>
      <div id="batchExtractBody" class="batch-body" hidden>
        <div class="batch-row">
          <button id="batchTplBtn" class="btn">模板下载</button>
          <button id="batchRulesBtn" class="btn">上传规则</button>
          <input id="batchRulesInput" type="file" accept=".xlsx,.json" hidden>
        </div>
        <button id="batchExtractBtn" class="btn btn-primary batch-confirm">确认抽取</button>
      </div>
      <button id="batchExportHead" class="batch-acc-head">批量导出</button>
      <div id="batchExportBody" class="batch-body" hidden>
        <div class="batch-row">
          <label class="check"><input id="exportResultChk" type="checkbox" checked> 识别结果</label>
          <label class="check"><input id="exportExtractedChk" type="checkbox" checked> 抽取结果</label>
        </div>
        <button id="batchExportBtn" class="btn btn-primary batch-confirm">确认导出</button>
      </div>
    </div>
  </aside>
```

并在 `<input id="fileInput" ...>` 后追加一个隐藏文件输入（规则页签上传用）：

```html
    <input id="rulesFileInput" type="file" accept=".xlsx,.json" hidden>
```

- [ ] **Step 2: 修改 `static/app.js` 状态与初始化**

2a. `state` 对象末尾追加两个字段：

```js
  selected: new Set(),
  batch: { id: null, timer: null, overlay: {} },
```

2b. `init()` 内追加绑定（放在 `$("#viewTabs")...` 之前）：

```js
  $("#selectAll").addEventListener("change", (e) => {
    const done = state.tasks.filter((t) => t.status === "done");
    state.selected = e.target.checked ? new Set(done.map((t) => t.id)) : new Set();
    renderTaskList();
    renderBatchPanel();
  });
  $("#batchClear").addEventListener("click", () => {
    state.selected.clear();
    renderTaskList();
    renderBatchPanel();
  });
  $("#batchExtractHead").addEventListener("click", () => toggleBatchBody("extract"));
  $("#batchExportHead").addEventListener("click", () => toggleBatchBody("export"));
  $("#batchTplBtn").addEventListener("click", () => {
    window.location.href = "/api/rules-default.xlsx";
  });
  $("#batchRulesBtn").addEventListener("click", () => $("#batchRulesInput").click());
  $("#batchRulesInput").addEventListener("change", (e) => {
    if (e.target.files.length) batchUploadRules(e.target.files[0]);
    e.target.value = "";
  });
  $("#rulesFileInput").addEventListener("change", uploadTaskRulesFile);
```

- [ ] **Step 3: `app.js` 任务列表渲染加复选框与批量状态叠加**

`renderTaskList()` 中，`li` 创建后、`dot` 创建前插入复选框；状态文字处叠加批量状态。改动后的关键片段：

```js
    if (t.status === "done") {
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.className = "task-check";
      cb.checked = state.selected.has(t.id);
      cb.addEventListener("click", (e) => e.stopPropagation());
      cb.addEventListener("change", (e) => {
        if (e.target.checked) state.selected.add(t.id);
        else state.selected.delete(t.id);
        renderTaskList();
        renderBatchPanel();
      });
      li.append(cb);
    }
```

状态文字部分替换为：

```js
    const ov = state.batch.overlay[t.id];
    st.textContent = ov
      ? ov.text
      : t.status === "failed" ? `失败：${t.error || ""}` : statusText(t);
    if (t.status === "failed") st.title = t.error || "";
    else if (ov) st.title = ov.text;
    if (ov && ov.error) st.classList.add("status-error");
```

- [ ] **Step 4: `app.js` 新增批量面板函数**

在 `renderTaskList` 之后新增：

```js
function renderBatchPanel() {
  const n = state.selected.size;
  $("#batchPanel").hidden = n === 0;
  $("#batchCount").textContent = n;
  const done = state.tasks.filter((t) => t.status === "done");
  const sel = $("#selectAll");
  sel.checked = done.length > 0 && done.every((t) => state.selected.has(t.id));
  sel.indeterminate = !sel.checked && done.some((t) => state.selected.has(t.id));
  const anyExtracted = done.some((t) => state.selected.has(t.id) && t.has_extracted);
  const chk = $("#exportExtractedChk");
  chk.disabled = !anyExtracted;
  if (!anyExtracted) chk.checked = false;
}

function toggleBatchBody(which) {
  const eb = $("#batchExtractBody");
  const xb = $("#batchExportBody");
  if (which === "extract") {
    eb.hidden = !eb.hidden;
    xb.hidden = true;
  } else {
    xb.hidden = !xb.hidden;
    eb.hidden = true;
  }
}

async function batchUploadRules(file) {
  const fd = new FormData();
  fd.append("file", file);
  fd.append("task_ids", [...state.selected].join(","));
  const resp = await fetch("/api/batch/rules/upload", { method: "POST", body: fd });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    showToast(`上传失败：${data.detail || resp.status}`);
    return;
  }
  showToast(`已更新 ${data.updated} 个任务的规则`);
  if (state.currentId && state.selected.has(state.currentId)) state.rules = null;
}

async function uploadTaskRulesFile(e) {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file || !state.currentId) return;
  const fd = new FormData();
  fd.append("file", file);
  const resp = await fetch(`/api/tasks/${state.currentId}/rules/upload`, {
    method: "POST",
    body: fd,
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    showToast(`上传失败：${data.detail || resp.status}`);
    return;
  }
  await loadRules();
  showToast("规则已更新");
}
```

`pollTasks()` 内 `renderTaskList();` 之后追加（清理失效选中并刷新面板）：

```js
  const doneIds = new Set(state.tasks.filter((t) => t.status === "done").map((t) => t.id));
  for (const id of [...state.selected]) {
    if (!doneIds.has(id)) state.selected.delete(id);
  }
  renderBatchPanel();
```

- [ ] **Step 5: `app.js` 规则页签按任务化**

5a. `resetExtractState()` 内追加 `state.rules = null;`（切换任务时强制重新加载规则）。

5b. `loadRules()` 改为：

```js
async function loadRules() {
  if (!state.currentId) return;
  const resp = await fetch(`/api/tasks/${state.currentId}/rules`);
  state.rules = resp.ok ? (await resp.json()).rules : [];
  if (!$("#rulesPanel").hidden) renderRulesEditor();
}
```

5c. `saveRules()` 的请求地址改为 `PUT /api/tasks/${state.currentId}/rules`（其余逻辑不变）。

5d. `renderRulesEditor()` 中：
- 标题：`head.textContent = "抽取规则（仅对当前任务生效）";`
- "恢复默认"按钮处理改为：

```js
  resetBtn.addEventListener("click", async () => {
    const resp = await fetch("/api/rules-default");
    if (!resp.ok) return;
    state.rules = structuredClone((await resp.json()).rules);
    renderRulesEditor();
    showToast("已载入默认规则，保存后生效");
  });
```

- `actions` 行在 `addBtn` 之前新增两个按钮：

```js
  const uploadBtn = document.createElement("button");
  uploadBtn.className = "btn";
  uploadBtn.textContent = "上传规则";
  uploadBtn.addEventListener("click", () => $("#rulesFileInput").click());
  const downloadBtn = document.createElement("button");
  downloadBtn.className = "btn";
  downloadBtn.textContent = "下载规则表格";
  downloadBtn.addEventListener("click", () => {
    window.location.href = `/api/tasks/${state.currentId}/rules.xlsx`;
  });
  actions.append(uploadBtn, downloadBtn, addBtn, resetBtn, saveBtn);
```

（原 `actions.append(addBtn, resetBtn, saveBtn);` 一行删除）

- [ ] **Step 6: `static/style.css` 末尾追加样式**

```css
/* ---------- 侧栏：全选与批量操作 ---------- */
.select-all {
  flex: none;
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0 16px 4px;
  padding: 6px 10px;
  border-bottom: 1px solid var(--line);
  font-size: 13px;
  cursor: pointer;
}

.select-all input { accent-color: var(--accent); }

.task-check { flex: none; accent-color: var(--accent); cursor: pointer; }

.batch-panel {
  flex: none;
  border-top: 1px solid var(--line);
  padding: 10px 12px 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  background: var(--paper);
}

.batch-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 13px;
}

.batch-acc-head {
  appearance: none;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--panel);
  color: var(--ink);
  font-size: 13px;
  font-weight: 600;
  font-family: inherit;
  padding: 7px 12px;
  text-align: left;
  cursor: pointer;
}

.batch-acc-head:hover { background: #eef1f5; }

.batch-body {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 2px 0 2px 8px;
  border-left: 2px solid var(--line);
}

.batch-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.batch-confirm { align-self: flex-start; }

.status-error { color: var(--st-failed); }
```

- [ ] **Step 7: 浏览器验证（隔离数据根，不碰生产数据）**

```bash
rm -rf /tmp/bill-test && mkdir -p /tmp/bill-test
cp -r /qn-server-data/bill-recognition/tasks /qn-server-data/bill-recognition/tasks.json /qn-server-data/bill-recognition/output /tmp/bill-test/
BILL_DATA_ROOT=/tmp/bill-test nohup uv run uvicorn app:app --port 8123 > /tmp/bill-test.log 2>&1 &
```

用 playwright-cli（先加载 `playwright` 技能获取用法）打开 `http://127.0.0.1:8123/` 逐项确认：
1. 侧栏顶端有"全选"行；3 个完成任务行各带复选框
2. 勾选 1 个任务 → 底部出现批量面板（已选 1 个 / 批量抽取 / 批量导出）
3. 点"批量抽取"标题展开"模板下载 / 上传规则 / 确认抽取"，再点"批量导出"切换展开（互斥）
4. 打开任一任务 → 规则抽取 → 规则模板：标题为"抽取规则（仅对当前任务生效）"，有 上传规则 / 下载规则表格 / 恢复默认 / 保存规则 按钮
5. 点"恢复默认"→ 表格变 21 字段；点"保存规则"→ toast 成功；刷新页面后规则仍在（已持久化）

验证完停掉测试服务：`pkill -f "uvicorn app:app --port 8123"`

- [ ] **Step 8: 提交**

```bash
git add static/index.html static/app.js static/style.css
git commit -m "Add batch selection UI and per-task rule editor"
```

---

### Task 6: 前端——批量抽取/导出接线与状态轮询

**Covers:** [S6, S7, S11]

**Files:**
- Modify: `static/app.js`（批量抽取提交与轮询、批量导出下载、状态叠加收尾）

**Interfaces:**
- Consumes: `POST /api/batch/extract`、`GET /api/batch/{batch_id}`、`POST /api/export`（Task 3/4）；Task 5 的 `state.batch`、`renderBatchPanel`、按钮元素
- Produces: 完整的批量抽取/导出前端闭环

- [ ] **Step 1: `app.js` `init()` 追加两个确认按钮绑定**

```js
  $("#batchExtractBtn").addEventListener("click", startBatchExtract);
  $("#batchExportBtn").addEventListener("click", batchExport);
```

- [ ] **Step 2: `app.js` 新增批量抽取提交与轮询**

在 `batchUploadRules` 附近新增：

```js
async function startBatchExtract() {
  if (state.batch.id) {
    showToast("已有批量抽取进行中");
    return;
  }
  const resp = await fetch("/api/batch/extract", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task_ids: [...state.selected] }),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    showToast(`批量抽取失败：${data.detail || resp.status}`);
    return;
  }
  state.batch.id = data.batch_id;
  pollBatch();
}

async function pollBatch() {
  clearTimeout(state.batch.timer);
  const resp = await fetch(`/api/batch/${state.batch.id}`);
  if (!resp.ok) {
    state.batch.id = null;
    return;
  }
  const data = await resp.json();
  const overlay = {};
  let ok = 0;
  let fail = 0;
  let finished = true;
  for (const it of data.items) {
    if (it.status === "pending" || it.status === "processing") {
      overlay[it.task_id] = { text: "抽取中…" };
      finished = false;
    } else if (it.status === "done") {
      overlay[it.task_id] = { text: "抽取成功" };
      ok += 1;
    } else {
      overlay[it.task_id] = { text: `抽取失败：${it.error || ""}`, error: true };
      fail += 1;
    }
  }
  state.batch.overlay = overlay;
  renderTaskList();
  if (!finished) {
    state.batch.timer = setTimeout(pollBatch, 2000);
    return;
  }
  showToast(`批量抽取完成：${ok} 成功 / ${fail} 失败`);
  setTimeout(() => {
    state.batch.overlay = {};
    state.batch.id = null;
    renderTaskList();
    pollTasks();
  }, 5000);
}
```

- [ ] **Step 3: `app.js` 新增批量导出**

```js
async function batchExport() {
  const kinds = [];
  if ($("#exportResultChk").checked) kinds.push("result");
  if ($("#exportExtractedChk").checked) kinds.push("extracted");
  if (!kinds.length) {
    showToast("请至少勾选一种结果");
    return;
  }
  const taskIds = [...state.selected];
  for (const kind of kinds) {
    showToast("正在生成…");
    const resp = await fetch("/api/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task_ids: taskIds, kind }),
    });
    if (!resp.ok) {
      showToast(`导出失败：${(await resp.json().catch(() => ({}))).detail || resp.status}`);
      continue;
    }
    const blob = await resp.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = kind === "result" ? "识别结果.zip" : "抽取结果.zip";
    a.click();
    URL.revokeObjectURL(a.href);
    const skipped = parseInt(resp.headers.get("X-Skipped") || "0", 10);
    if (skipped) showToast(`${skipped} 个任务无抽取结果已跳过`);
  }
}
```

- [ ] **Step 4: 浏览器端到端验证**

重启隔离测试服务（同 Task 5 Step 7 的 seeding 与启动命令）。用 playwright-cli：
1. 勾选 1 个最小任务（大连银行）→ 批量抽取 → 确认抽取（走真实 LLM，需 `BAILIAN_API_KEY` 或本机 mimocode auth 回退；约 1 分钟内完成）
2. 任务行出现"抽取中…"→ 结束后"抽取成功"，toast 汇总出现
3. 打开该任务 → 规则抽取页签能看到批量抽取产生的结果
4. 勾选 2 个任务 → 批量导出 → 两个复选框 → 确认导出：浏览器下载 2 个 zip
5. 用 curl 复核 zip 内容：

```bash
curl -s -X POST http://127.0.0.1:8123/api/export -H 'Content-Type: application/json' \
  -d "{\"task_ids\": [\"<任务id1>\", \"<任务id2>\"], \"kind\": \"extracted\"}" \
  -o /tmp/exp.zip -D - | grep -i x-skipped
unzip -l /tmp/exp.zip
```

验证完停掉测试服务：`pkill -f "uvicorn app:app --port 8123"`

- [ ] **Step 5: 提交**

```bash
git add static/app.js
git commit -m "Wire batch extraction polling and batch export downloads"
```

---

### Task 7: Docker 一键部署

**Covers:** [S9, S11]

**Files:**
- Create: `Dockerfile`
- Create: `docker-compose.yml`
- Create: `.env.example`
- Create: `.dockerignore`
- Modify: `README.md`（部署章节）

**Interfaces:**
- Consumes: 全部后端能力（镜像内运行 `app:app`）
- Produces: `docker compose up -d --build` 一键部署

- [ ] **Step 1: 写 `Dockerfile`**

```dockerfile
FROM python:3.12-slim

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY app.py extract.py main.py bulk.py ./
COPY static ./static

RUN uv sync --frozen --no-dev

# 模型缓存与字体指向数据卷；目标目录不存在时由启动命令补建（全新机器首次自动下载模型）
RUN mkdir -p /root/.paddlex \
 && ln -s /qn-server-data/paddlex-models /root/.paddlex/official_models \
 && ln -s /qn-server-data/paddlex-fonts /root/.paddlex/fonts

EXPOSE 8000

CMD ["sh", "-c", "mkdir -p /qn-server-data/paddlex-models /qn-server-data/paddlex-fonts && uv run uvicorn app:app --host 0.0.0.0 --port 8000"]
```

- [ ] **Step 2: 写 `docker-compose.yml` 与 `.env.example`、`.dockerignore`**

`docker-compose.yml`：

```yaml
services:
  bill-recognition:
    build: .
    ports:
      - "${BILL_PORT:-8000}:8000"
    volumes:
      - /qn-server-data:/qn-server-data
    env_file:
      - .env
    restart: unless-stopped
```

`.env.example`：

```
# 百炼 API key（规则抽取必需；仅用识别可留空）
BAILIAN_API_KEY=
# 宿主机映射端口
BILL_PORT=8000
```

`.dockerignore`：

```
.venv
.git
.codegraph
.mimocode
.playwright-cli
input_doc
output
__pycache__
docs
.env
```

- [ ] **Step 3: `README.md` 追加部署章节**

```markdown
## Docker 一键部署

1. 准备环境变量：`cp .env.example .env`，填入 `BAILIAN_API_KEY`（规则抽取需要；仅识别可留空）
2. 启动：`docker compose up -d --build`
3. 访问：`http://localhost:8000`（端口可用 `.env` 的 `BILL_PORT` 修改）

数据与模型缓存全部位于 `/qn-server-data`（容器同名挂载）：重建容器不丢数据、不重下模型。
全新机器（无 cosfs 挂载）上数据目录自动创建，OCR 模型在首次识别时自动下载。
```

- [ ] **Step 4: 构建镜像**

Run: `docker compose build`
Expected: 构建成功（首次约数分钟，paddlepaddle wheel 较大）

- [ ] **Step 5: 端到端验证（先与用户确认停掉裸跑实例）**

本机内存仅 3.6GB，OCR 模型 1.5GB——**必须先征得用户同意**再停掉裸跑的 uvicorn（端口 61739）：

```bash
# 用户同意后执行：
pkill -f "uvicorn app:app --host 0.0.0.0 --port 61739"
cp .env.example .env   # 若 .env 不存在；BAILIAN_API_KEY 可暂留空（用本机 mimocode auth 回退不可用于容器，容器内需 .env 或数据根 config.json）
docker compose up -d
docker compose ps      # 期望 running/healthy
curl -s http://localhost:8000/api/tasks | head -c 300   # 期望返回现有任务 JSON
```

OCR 端到端（模型从数据卷加载，约 1-2 分钟）：

```bash
curl -s -F "files=@input_doc/大连银行.png" http://localhost:8000/api/upload
# 轮询 /api/tasks 直至该任务 status=done（timeout 300s）
```

- [ ] **Step 6: 提交**

```bash
git add Dockerfile docker-compose.yml .env.example .dockerignore README.md
git commit -m "Add Docker one-command deployment"
```

（`.env` 不入库——含密钥；确认 `.gitignore` 已含 `.env`，若无则追加。）

