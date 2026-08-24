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
