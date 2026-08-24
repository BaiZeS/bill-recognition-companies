# 票据识别 Web 服务。
# 注意：必须单进程运行（uvicorn 默认 1 worker，禁止 --workers N）——
# OCR 模型常驻约 1.5GB，多 worker 会在 3.6GB 内存的本机 OOM。
import gc
import json
import os
import queue
import shutil
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import Body, FastAPI, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from extract import (
    DEFAULT_RULE_TEMPLATE,
    build_prompt,
    group_page,
    parse_llm_response,
    transform_fields,
    validate_rules,
)
import bulk
from main import _erase_seals, _erase_watermarks, build_ocr, load_pages, parse_page

# 数据全部落对象存储（cosfs 挂载），本地仓库只留代码
DATA_ROOT = Path(os.environ.get("BILL_DATA_ROOT", "/qn-server-data/bill-recognition"))
MOUNT_POINT = "/qn-server-data"
TASKS_DIR = DATA_ROOT / "tasks"
OUTPUT_DIR = DATA_ROOT / "output"
INDEX_PATH = DATA_ROOT / "tasks.json"

ALLOWED_EXT = {".pdf", ".jpg", ".jpeg", ".png"}

TASKS: dict[str, dict] = {}
TASKS_LOCK = threading.Lock()
QUEUE: "queue.Queue[str]" = queue.Queue()
# 结果内存缓存：cosfs 对"刚写入的文件"存在分钟级负缓存（exists() 不可靠），
# 本进程写过的结果一律以内存态为准，文件只是持久化副本
RESULTS: dict[str, list] = {}
EXTRACTED: dict[str, dict] = {}
# 任务规则内存缓存：与 EXTRACTED 同模式，应对 cosfs 负缓存
RULES: dict[str, list] = {}
XLSX_MT = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_OCR = None


def _get_ocr():
    global _OCR
    if _OCR is None:
        _OCR = build_ocr()
    return _OCR


def _save_index():
    # cosfs 对象存储不支持可靠的 rename，直接写 + fsync；
    # 索引是尽力持久化，写失败不影响内存态任务
    with TASKS_LOCK:
        snapshot = list(TASKS.values())
    try:
        with open(INDEX_PATH, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        print(f"[warn] tasks.json 写入失败：{e}", flush=True)


def _load_index():
    if not INDEX_PATH.exists():
        return
    try:
        with open(INDEX_PATH, encoding="utf-8") as f:
            items = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[warn] tasks.json 读取失败，忽略：{e}", flush=True)
        return
    for t in items:
        if t["status"] in ("pending", "processing"):
            t["status"] = "failed"
            t["error"] = "服务重启中断"
        TASKS[t["id"]] = t


def _alloc_output_name(base: str) -> str:
    taken = {t["output_name"] for t in TASKS.values()}
    name, n = base, 2
    while (OUTPUT_DIR / f"{name}_structured.json").exists() or name in taken:
        name = f"{base}_{n}"
        n += 1
    return name


def _process(task: dict):
    task_dir = TASKS_DIR / task["id"]
    pages = load_pages(str(task_dir / f"input{task['ext']}"))
    task["page_count"] = len(pages)
    _save_index()

    # 擦除均原地修改数组，预览页图必须在任何擦除之前保存
    from PIL import Image

    for i, arr in enumerate(pages):
        Image.fromarray(arr).save(task_dir / f"page_{i}.png")

    ocr = _get_ocr()
    doc = []
    for i, arr in enumerate(pages):
        # 先水印后印章：水印门标定基于原图统计，且水印遍不扰动红章像素集
        cleaned = arr
        if task.get("dewm", True):
            cleaned = _erase_watermarks(cleaned)
        if task.get("deseal", True):
            cleaned = _erase_seals(cleaned)
        res = next(iter(ocr.predict([cleaned])))
        doc.append(parse_page(cleaned, res["rec_texts"], res["rec_boxes"], page_idx=i))
        task["pages_done"] = i + 1
        _save_index()

    with open(task_dir / "result.json", "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    with open(OUTPUT_DIR / f"{task['output_name']}_structured.json", "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    RESULTS[task["id"]] = doc
    del pages
    gc.collect()


def _worker():
    while True:
        task_id = QUEUE.get()
        with TASKS_LOCK:
            task = TASKS.get(task_id)
        # pending 任务被删除后不在 TASKS 中，直接跳过
        if task is None or task["status"] != "pending":
            continue
        task["status"] = "processing"
        _save_index()
        try:
            _process(task)
            task["status"] = "done"
        except Exception as e:  # noqa: BLE001 - 任务级兜底
            task["status"] = "failed"
            task["error"] = f"{type(e).__name__}: {e}"
        _save_index()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 防止 cosfs 挂载丢失时数据悄悄落到本地根盘
    if not os.path.ismount(MOUNT_POINT):
        raise RuntimeError(f"{MOUNT_POINT} 未挂载（对象存储不可用），拒绝启动")
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    probe = DATA_ROOT / ".write_probe"
    probe.write_text("ok")
    probe.unlink()
    _load_index()
    threading.Thread(target=_worker, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan)


def _public(t: dict) -> dict:
    return {k: t.get(k) for k in (
        "id", "filename", "status", "error", "output_name",
        "page_count", "pages_done", "created",
    )}


@app.post("/api/upload")
def upload(files: list[UploadFile], dewm: bool = Form(True), deseal: bool = Form(True)):
    if not files:
        raise HTTPException(400, "未收到文件")
    created = []
    for up in files:
        filename = os.path.basename(up.filename or "")
        ext = os.path.splitext(filename)[1].lower()
        if ext not in ALLOWED_EXT:
            raise HTTPException(400, f"不支持的文件类型：{filename}")
        task_id = uuid.uuid4().hex[:10]
        task_dir = TASKS_DIR / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        with open(task_dir / f"input{ext}", "wb") as f:
            shutil.copyfileobj(up.file, f)
        base = os.path.splitext(filename)[0] or task_id
        with TASKS_LOCK:
            task = {
                "id": task_id,
                "filename": filename,
                "ext": ext,
                "status": "pending",
                "error": None,
                "output_name": _alloc_output_name(base),
                "page_count": None,
                "pages_done": 0,
                "dewm": dewm,
                "deseal": deseal,
                "has_corrected": False,
                "created": time.time(),
            }
            TASKS[task_id] = task
        QUEUE.put(task_id)
        created.append(_public(task))
    _save_index()
    return {"tasks": created}


@app.get("/api/tasks")
def list_tasks(limit: int | None = Query(default=None, ge=1)):
    with TASKS_LOCK:
        items = sorted(TASKS.values(), key=lambda t: t["created"], reverse=True)
    if limit:
        items = items[:limit]
    return {"tasks": [_public(t) for t in items]}


def _get_task(task_id: str) -> dict:
    with TASKS_LOCK:
        task = TASKS.get(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    return task


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str):
    return _public(_get_task(task_id))


@app.get("/api/tasks/{task_id}/result")
def get_result(task_id: str):
    task = _get_task(task_id)
    if task["status"] != "done":
        raise HTTPException(404, "结果未就绪")
    if task_id in RESULTS:
        return JSONResponse(RESULTS[task_id])
    result_path = TASKS_DIR / task_id / "result.json"
    if not result_path.exists():
        raise HTTPException(404, "结果未就绪")
    return JSONResponse(json.loads(result_path.read_text(encoding="utf-8")))


@app.get("/api/tasks/{task_id}/pages/{i}")
def get_page(task_id: str, i: int):
    task = _get_task(task_id)
    task_dir = TASKS_DIR / task_id
    png = task_dir / f"page_{i}.png"
    if png.exists():
        return FileResponse(png, media_type="image/png")
    # 缺失（或 cosfs 负缓存暂不可见）：由原件重渲染，直接以内存字节返回
    try:
        pages = load_pages(str(task_dir / f"input{task['ext']}"))
    except Exception:  # noqa: BLE001
        raise HTTPException(404, "页不存在")
    if i < 0 or i >= len(pages):
        raise HTTPException(404, "页不存在")
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(pages[i]).save(buf, format="PNG")
    for j, arr in enumerate(pages):
        p = task_dir / f"page_{j}.png"
        if not p.exists():
            try:
                Image.fromarray(arr).save(p)
            except OSError:
                pass
    del pages
    return Response(buf.getvalue(), media_type="image/png")


@app.post("/api/tasks/{task_id}/save")
async def save_result(task_id: str, payload: list = Body(...)):
    task = _get_task(task_id)
    if task["status"] != "done":
        raise HTTPException(422, "任务未完成，无法保存")
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    (TASKS_DIR / task_id / "result.json").write_text(body, encoding="utf-8")
    corrected = OUTPUT_DIR / f"{task['output_name']}_corrected.json"
    corrected.write_text(body, encoding="utf-8")
    RESULTS[task_id] = payload
    task["has_corrected"] = True
    _save_index()
    return {"saved": str(corrected)}


@app.api_route("/api/tasks/{task_id}/download", methods=["GET", "HEAD"])
def download(task_id: str):
    task = _get_task(task_id)
    fname_base = task["output_name"]
    corrected = OUTPUT_DIR / f"{fname_base}_corrected.json"
    if task.get("has_corrected") and task_id in RESULTS:
        # 刚保存的修正可能还在 cosfs 负缓存窗口内，直接走内存
        body = json.dumps(RESULTS[task_id], ensure_ascii=False, indent=2)
        return Response(
            body,
            media_type="application/json",
            headers={
                "Content-Disposition":
                f"attachment; filename*=UTF-8''{quote(f'{fname_base}_corrected.json')}"
            },
        )
    if task.get("has_corrected") or corrected.exists():
        path = corrected
    else:
        path = OUTPUT_DIR / f"{fname_base}_structured.json"
        if not path.exists():
            raise HTTPException(404, "结果未就绪")
    return FileResponse(
        path,
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(path.name)}"},
    )


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: str):
    task = _get_task(task_id)
    if task["status"] == "processing":
        raise HTTPException(409, "任务识别中，无法删除")
    with TASKS_LOCK:
        TASKS.pop(task_id, None)
    RESULTS.pop(task_id, None)
    EXTRACTED.pop(task_id, None)
    RULES.pop(task_id, None)
    shutil.rmtree(TASKS_DIR / task_id, ignore_errors=True)
    _save_index()
    return {"deleted": task_id}


# ---------- 按规则抽取 ----------

RULE_TEMPLATE_PATH = DATA_ROOT / "rule_template.json"
EXTRACT_CONFIG_PATH = DATA_ROOT / "config.json"
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "deepseek-v4-flash-0731"
MIMO_AUTH_PATH = Path(
    os.environ.get("MIMO_AUTH_PATH", str(Path.home() / ".local/share/mimocode/auth.json"))
)


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_json(path, obj):
    # cosfs 无可靠 rename：直写 + fsync
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        print(f"[warn] {path.name} 写入失败：{e}", flush=True)


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
        try:
            rules = validate_rules(data["rules"])
        except ValueError:
            return _seed_rules()
        RULES[task_id] = rules
        return rules
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


def _extract_config():
    cfg = _read_json(EXTRACT_CONFIG_PATH)
    return cfg if isinstance(cfg, dict) else {}


def _resolve_api_key():
    key = os.environ.get("BAILIAN_API_KEY", "").strip()
    if key:
        return key
    key = str(_extract_config().get("api_key") or "").strip()
    if key:
        return key
    # 回退本机 mimocode auth（仅 alibaba 标准源 provider，不含 token-plan）
    auth = _read_json(MIMO_AUTH_PATH)
    if not isinstance(auth, dict):
        return ""
    providers = [
        p.strip()
        for p in os.environ.get("BAILIAN_KEY_PROVIDER", "alibaba-cn,bailian-alibaba").split(",")
        if p.strip()
    ]
    for name in providers:
        entry = auth.get(name)
        if isinstance(entry, dict):
            k = str(entry.get("key") or "").strip()
            if k:
                return k
    return ""


def _resolve_base_url():
    return (
        os.environ.get("BAILIAN_BASE_URL", "").strip()
        or str(_extract_config().get("base_url") or "").strip()
        or DEFAULT_BASE_URL
    )


def _resolve_model():
    return (
        os.environ.get("BAILIAN_MODEL", "").strip()
        or str(_extract_config().get("model") or "").strip()
        or DEFAULT_MODEL
    )


def _call_llm(system, user, api_key, base_url, model):
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 4096,
        # deepseek-v4-flash 默认开思考：思考过程会吃光 max_tokens 导致 content 为空
        "enable_thinking": False,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    last_err = None
    for _ in range(2):  # 失败自动重试 1 次
        try:
            with httpx.Client(timeout=60) as client:
                resp = client.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                last_err = f"HTTP {resp.status_code}: {resp.text[:300]}"
                continue
            return resp.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
            last_err = f"{type(e).__name__}: {e}"
    raise RuntimeError(last_err or "未知错误")


@app.get("/api/extract-config")
def get_extract_config():
    return {
        "configured": bool(_resolve_api_key()),
        "model": _resolve_model(),
        "base_url": _resolve_base_url(),
    }


@app.put("/api/extract-config")
def put_extract_config(payload: dict = Body(...)):
    cfg = _extract_config()
    for k in ("model", "base_url"):
        v = str(payload.get(k) or "").strip()
        if v:
            cfg[k] = v
        else:
            cfg.pop(k, None)
    _write_json(EXTRACT_CONFIG_PATH, cfg)
    return {"saved": True}


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


@app.get("/api/tasks/{task_id}/extracted")
def get_extracted(task_id: str):
    _get_task(task_id)
    if task_id in EXTRACTED:
        return EXTRACTED[task_id]
    data = _read_json(TASKS_DIR / task_id / "extracted.json")
    if not isinstance(data, dict) or not isinstance(data.get("groups"), list):
        raise HTTPException(404, "抽取结果未就绪")
    return data


@app.put("/api/tasks/{task_id}/extracted")
def put_extracted(task_id: str, payload: dict = Body(...)):
    task = _get_task(task_id)
    if task["status"] != "done":
        raise HTTPException(422, "任务未完成，无法保存")
    if not isinstance(payload.get("groups"), list):
        raise HTTPException(422, "抽取结果格式无效")
    _write_json(TASKS_DIR / task_id / "extracted.json", payload)
    EXTRACTED[task_id] = payload
    return {"saved": True}


app.mount("/", StaticFiles(directory="static", html=True), name="static")
