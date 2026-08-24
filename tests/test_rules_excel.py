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
    data = ('{"rules": [{"code": "A", "name": "甲", "required": "必填", '
            '"type": "字符", "length": null, "strategy": ""}]}').encode("utf-8")
    rules = bulk.load_rules_file("r.json", data)
    assert rules[0]["code"] == "A"


def test_unsupported_ext():
    with pytest.raises(ValueError, match="仅支持"):
        bulk.load_rules_file("r.txt", b"x")
