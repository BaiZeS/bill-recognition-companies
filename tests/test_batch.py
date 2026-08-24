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


def test_batch_extract_failure_does_not_block_others(client, monkeypatch):
    c, appmod = client
    monkeypatch.setenv("BAILIAN_API_KEY", "k")
    ran = []

    def fake_run(task, rules, api_key, base_url, model):
        ran.append(task["id"])
        if task["filename"].startswith("坏任务"):
            raise RuntimeError("boom-first")
        data = {"model": model, "groups": []}
        appmod.EXTRACTED[task["id"]] = data
        return data

    monkeypatch.setattr(appmod, "_run_extract", fake_run)
    t1, _ = make_task(appmod, "坏任务")
    t2, _ = make_task(appmod, "好任务")
    batch_id = c.post("/api/batch/extract", json={"task_ids": [t1, t2]}).json()["batch_id"]
    data = wait_batch(c, appmod, batch_id)
    by_task = {i["task_id"]: i for i in data["items"]}
    assert by_task[t1]["status"] == "failed"
    assert by_task[t2]["status"] == "done"
    assert ran == [t1, t2]  # 失败后继续执行


def test_batch_extract_dedupes_task_ids(client, monkeypatch):
    c, appmod = client
    monkeypatch.setenv("BAILIAN_API_KEY", "k")
    ran = []

    def fake_run(task, rules, api_key, base_url, model):
        ran.append(task["id"])
        return {"model": model, "groups": []}

    monkeypatch.setattr(appmod, "_run_extract", fake_run)
    t1, _ = make_task(appmod, "去重")
    batch_id = c.post("/api/batch/extract", json={"task_ids": [t1, t1]}).json()["batch_id"]
    data = wait_batch(c, appmod, batch_id)
    assert len(data["items"]) == 1
    assert ran == [t1]


def test_batch_unknown_id_404(client):
    c, _ = client
    assert c.get("/api/batch/nonexist").status_code == 404
