import json
import os

import cv2
import numpy as np

RED_MARGIN = 40
SEAL_DILATE = 25
SEAL_MIN_REGION_AREA = 500
# 腐蚀后红墨像素数门：伪区域（红字笔画交叉孤立残点）1-6px，真章 ≥1171
SEAL_MIN_RED_MASS = 100
# 封闭外周门：真章外周圆/椭圆/方封闭，9×9 闭运算 bridging 断口后封闭孔洞 ≥224；
# 红字行/红框线无封闭大孔（=0），补粗红印刷字过 mass 门的盲点（核不可调大：东亚盖印 15 核孔洞归零）
SEAL_CLOSE = 9
SEAL_MIN_HOLE = 50
# 子像素渲染(ClearType 类)给黑字边缘留 1-2px 红 fringe，R-max(G,B) 可达 80，
# 会被 _red_mask 当成印章红墨；腐蚀掉细 fringe，真章笔画(≥3px)保留
SEAL_ERODE = 3
# 擦除用 HSV 阈值：浅红章字体/边缘绝对色差小，RGB 差值阈值抓不到；
# 红相色域含品红侧（扫描件红墨常偏品红），低饱和度抓浅粉，亮度下限防误伤深色正文
ERASE_HUE_MAX = 12
ERASE_HUE_MIN = 150
ERASE_SAT_MIN = 15
ERASE_VAL_MIN = 100
# 亮区淡色偏：浅红残影 + 章笔画周围的 JPEG 互补振铃（青/绿相），仅限印章包围盒内
HALO_SAT_MIN = 8
HALO_VAL_MIN = 150
# 笔画光晕：JPEG 振铃在红笔画四周形成淡色晕，严格笔画的小膨胀范围内补擦更淡的色偏
RING_DILATE = 9
RING_SAT_MIN = 4
RING_VAL_MIN = 140
# 章压字保护：只擦离黑字核心 >1px 的红/晕像素，保住字形 1px 抗锯齿边
# （天府"贰佰元整"被啃蚀的修复）；半径 ≥2 空白处留章弧残桩致 det 拆行（青岛），否决
SEAL_TEXT_GUARD = 1
# 水印（斜向平铺浅灰文字）：低饱和、灰度落在浅带、且远离正文/表格线等暗锚点
WM_DARK_MAX = 100
WM_GRAY_MIN = 150
WM_GRAY_MAX = 235
# 水印主体 S p90=2；红章带内像素 S>40，饱和度上限是防误擦红章的唯一防线
WM_SAT_MAX = 40
# 距暗像素 L2>3 才擦：保护正文抗锯齿（dist>2 实验吃掉字形边缘，否决）
WM_DIST_MIN = 3
# 检测门：有水印实测 2.86% vs 无水印 ≤0.20%；上限防整页灰底等分布外场景
WM_GATE_MIN_RATIO = 0.01
WM_GATE_MAX_RATIO = 0.20
# 平铺水印呈周期重复：掩码自相关峰/原点 ≥0.15 才走模板擦除（东亚实测 0.262）；
# 不满足说明非平铺水印，跳过擦除——回退旧掩码会误吃浅灰正文，比不擦更糟
WM_LATTICE_MIN_STRENGTH = 0.15
WM_LATTICE_MIN_DIST = 40
# 结构化擦除：模板 W 的笔画位置（W<242）上，只擦亮度不低于 W-25 的像素
# （白底水印≈W 整块擦；黑正文/表格线/灰正文暗核远暗于 W 受保护）
WM_TMPL_STROKE_MAX = 242
WM_TMPL_MARGIN = 25


def _red_mask(img):
    img = img.astype(int)
    return img[:, :, 0] - np.maximum(img[:, :, 1], img[:, :, 2]) > RED_MARGIN


def _erase_mask(img):
    hsv = cv2.cvtColor(img, cv2.COLOR_RGB2HSV)
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    red = ((h <= ERASE_HUE_MAX) | (h >= ERASE_HUE_MIN)) & (s >= ERASE_SAT_MIN) & (v >= ERASE_VAL_MIN)
    halo = (s >= HALO_SAT_MIN) & (v >= HALO_VAL_MIN)
    stroke = cv2.dilate(
        _red_mask(img).astype(np.uint8), np.ones((RING_DILATE, RING_DILATE), np.uint8)
    ) > 0
    ring = stroke & (s >= RING_SAT_MIN) & (v >= RING_VAL_MIN)
    # 距黑字核心 >1px 才擦：章压字时保住笔画抗锯齿边，空白处章弧照擦
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    dist = cv2.distanceTransform((gray >= WM_DARK_MAX).astype(np.uint8), cv2.DIST_L2, 3)
    return (red | halo | ring) & (dist > SEAL_TEXT_GUARD)


def load_pages(path):
    if path.lower().endswith(".pdf"):
        import pypdfium2 as pdfium

        with pdfium.PdfDocument(path) as pdf:
            return [
                np.array(page.render(scale=2).to_pil().convert("RGB"))
                for page in pdf
            ]
    from PIL import Image

    return [np.array(Image.open(path).convert("RGB"))]


def _max_hole(closed):
    # 不触碰 bbox 四边的最大背景连通分量 = 外周封闭的孔洞
    bg = (closed == 0).astype(np.uint8)
    n, lab = cv2.connectedComponents(bg)
    touch = set(lab[0, :].tolist()) | set(lab[-1, :].tolist())
    touch |= set(lab[:, 0].tolist()) | set(lab[:, -1].tolist())
    return max((int((lab == i).sum()) for i in range(1, n) if i not in touch), default=0)


def _seal_regions(arr):
    # 先腐蚀去掉文字子像素红 fringe，再大核膨胀把分离的印章笔画/文字连成整块
    red = cv2.erode(
        _red_mask(arr).astype(np.uint8), np.ones((SEAL_ERODE, SEAL_ERODE), np.uint8)
    )
    mask = cv2.dilate(
        red * 255,
        np.ones((SEAL_DILATE, SEAL_DILATE), np.uint8),
    )
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    # 面积门算在膨胀后，对点噪声无判别力（单残点膨胀即 625>500），加双结构门：
    # mass 挡细笔画残点/红细框线；hole 挡无封闭外周的粗红印刷字
    closed = cv2.morphologyEx(
        _red_mask(arr).astype(np.uint8),
        cv2.MORPH_CLOSE,
        np.ones((SEAL_CLOSE, SEAL_CLOSE), np.uint8),
    )
    regions = []
    for c in contours:
        if cv2.contourArea(c) <= SEAL_MIN_REGION_AREA:
            continue
        x, y, w, h = cv2.boundingRect(c)
        if red[y : y + h, x : x + w].sum() < SEAL_MIN_RED_MASS:
            continue
        if _max_hole(closed[y : y + h, x : x + w]) < SEAL_MIN_HOLE:
            continue
        regions.append((x, y, w, h))
    return regions


def _erase_seals(arr):
    # 印章必为红色：定位印章区域后把区域内红色像素抹白，黑色正文保留
    for x, y, w, h in _seal_regions(arr):
        region = arr[y : y + h, x : x + w]
        region[_erase_mask(region)] = 255
    return arr


def _watermark_mask(arr):
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    sat = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)[:, :, 1]
    dist = cv2.distanceTransform((gray >= WM_DARK_MAX).astype(np.uint8), cv2.DIST_L2, 3)
    mask = (
        (gray >= WM_GRAY_MIN)
        & (gray <= WM_GRAY_MAX)
        & (sat <= WM_SAT_MAX)
        & (dist > WM_DIST_MIN)
    )
    return mask, float(mask.mean())


def _watermark_lattice(mask):
    # 平铺水印的掩码自相关在晶格向量处出峰；峰强/原点强度低于阈值视为非平铺
    m = mask.astype(np.float32)
    h, w = m.shape
    f = np.fft.rfft2(m, (2 * h, 2 * w))
    corr = np.fft.irfft2(f * np.conj(f), (2 * h, 2 * w))[:h, :w]
    origin = float(corr[0, 0])
    if origin <= 0:
        return [], 0.0
    corr[:12, :12] = 0
    peaks = []
    for k in np.argsort(corr.ravel())[::-1]:
        y, x = divmod(int(k), w)
        if np.hypot(y, x) < WM_LATTICE_MIN_DIST:
            continue
        if all(np.hypot(y - py, x - px) > WM_LATTICE_MIN_DIST for py, px in peaks):
            peaks.append((int(y), int(x)))
        if len(peaks) >= 2:
            break
    return peaks, float(corr[peaks[0]] / origin) if peaks else 0.0


def _watermark_template(gray, peaks):
    # 跨晶格平移取中位数得纯水印模板：多数平移落在白底+水印，中位数即水印本身
    h, w = gray.shape
    rng = range(-2, 3) if h * w <= 1_000_000 else range(-1, 2)
    v1, v2 = peaks[0], (peaks[1] if len(peaks) > 1 else (0, 0))
    stack = []
    for a in rng:
        for b in rng:
            dy, dx = a * v1[0] + b * v2[0], a * v1[1] + b * v2[1]
            if abs(dy) >= h or abs(dx) >= w:
                continue
            ys, ye = max(0, -dy), min(h, h - dy)
            xs, xe = max(0, -dx), min(w, w - dx)
            tmp = np.full((h, w), np.nan, np.float32)
            tmp[ys:ye, xs:xe] = gray[ys + dy : ye + dy, xs + dx : xe + dx]
            stack.append(tmp)
    return np.nanmedian(np.stack(stack), axis=0)


def _erase_watermarks(arr):
    # 检测门命中且水印呈平铺周期时才擦除；不命中原样返回，对无水印票据零副作用
    mask, ratio = _watermark_mask(arr)
    if not WM_GATE_MIN_RATIO <= ratio <= WM_GATE_MAX_RATIO:
        return arr
    peaks, strength = _watermark_lattice(mask)
    if strength < WM_LATTICE_MIN_STRENGTH:
        return arr
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY).astype(np.float32)
    tmpl = _watermark_template(gray, peaks)
    sat = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)[:, :, 1]
    erase = (
        (tmpl < WM_TMPL_STROKE_MAX)
        & (gray >= tmpl - WM_TMPL_MARGIN)
        & (gray <= 248)
        & (sat <= WM_SAT_MAX)
        & np.isfinite(tmpl)
    )
    arr[erase] = 255
    return arr


def _clean_page(arr):
    # 先水印后印章：水印遍不扰动红章像素集，门标定基于原图统计
    return _erase_seals(_erase_watermarks(arr))


def _cluster_rows(lines):
    heights = sorted(l["box"][3] - l["box"][1] for l in lines)
    median_h = heights[len(heights) // 2] if heights else 20
    rows = []
    for ln in sorted(lines, key=lambda l: (l["box"][1] + l["box"][3]) / 2):
        yc = (ln["box"][1] + ln["box"][3]) / 2
        for row in rows:
            if abs(yc - row["_yc"]) <= 0.75 * median_h:
                n = len(row["cells"]) + 1
                row["_yc"] = (row["_yc"] * (n - 1) + yc) / n
                row["cells"].append(ln)
                break
        else:
            rows.append({"_yc": yc, "cells": [ln]})
    return [sorted(r["cells"], key=lambda c: c["box"][0]) for r in rows], median_h


def _split_tables(rows, median_h):
    tables, text_rows, run, prev_yc = [], [], [], None
    # 连续 3+ 单元格的行视为表格行；行间距超过 2 倍行高则切断表格区
    for row in rows:
        yc = sum((c["box"][1] + c["box"][3]) / 2 for c in row) / len(row)
        if prev_yc is not None and yc - prev_yc > 2 * median_h:
            _flush_run(run, tables, text_rows)
            run = []
        prev_yc = yc
        if len(row) >= 3:
            run.append(row)
        else:
            _flush_run(run, tables, text_rows)
            run = []
            text_rows.append(row)
    _flush_run(run, tables, text_rows)
    return tables, text_rows


def _flush_run(run, tables, text_rows):
    if len(run) >= 2:
        tables.append(run)
    else:
        text_rows.extend(run)


def _reading_order(lines, page_w, gap=25):
    cover = np.zeros(int(page_w) + 1, dtype=bool)
    for ln in lines:
        cover[ln["box"][0] : ln["box"][2]] = True
    ranges, start, gap_start = [], 0, None
    for x, covered in enumerate(cover):
        if not covered and gap_start is None:
            gap_start = x
        elif covered and gap_start is not None:
            if 0 < gap_start and x - gap_start >= gap:
                ranges.append((start, gap_start))
                start = x
            gap_start = None
    ranges.append((start, page_w))
    ordered = []
    for c0, c1 in ranges:
        ordered += sorted(
            (l for l in lines if c0 <= (l["box"][0] + l["box"][2]) / 2 < c1),
            key=lambda l: l["box"][1],
        )
    return ordered


def _extract_kv(lines, page_idx):
    kv = []
    for i, ln in enumerate(lines):
        key, sep, value = ln["text"].replace("：", ":").partition(":")
        if sep and key and value:
            kv.append({
                "id": f"p{page_idx}k{len(kv)}",
                "key": key,
                "value": value,
                "box": ln["box"],
                "line": f"p{page_idx}l{i}",
            })
    return kv


def parse_page(arr, texts, tboxes, page_idx=0):
    lines = [
        {"text": t, "box": [int(v) for v in b]} for t, b in zip(texts, tboxes)
    ]
    rows, median_h = _cluster_rows(lines)
    tables, text_rows = _split_tables(rows, median_h)
    ordered = _reading_order([c for r in text_rows for c in r], arr.shape[1])
    return {
        "page": page_idx,
        "size": [int(arr.shape[1]), int(arr.shape[0])],
        "tables": [
            {
                "id": f"p{page_idx}t{ti}",
                "rows": [
                    [
                        {
                            "id": f"p{page_idx}t{ti}r{ri}c{ci}",
                            "text": c["text"],
                            "box": c["box"],
                        }
                        for ci, c in enumerate(r)
                    ]
                    for ri, r in enumerate(t)
                ],
            }
            for ti, t in enumerate(tables)
        ],
        "text_lines": [
            {"id": f"p{page_idx}l{i}", "text": l["text"], "box": l["box"]}
            for i, l in enumerate(ordered)
        ],
        "kv": _extract_kv(ordered, page_idx),
    }


def build_ocr():
    from paddleocr import PaddleOCR

    # PP-OCRv6 推荐参数。det/rec 各项与 paddlex OCR.yaml 默认一致，显式写出防漂移；
    # 票据为平整扫描件，关闭文档方向分类/扭曲矫正/文本行方向三个预处理（省时省内存）。
    # enable_mkldnn=False 为本机固定参数（paddle 3.3.1 oneDNN PIR bug）。
    # text_det_thresh 低于默认 0.3：低分辨率票据的细笔画（如"一"）在 0.3 时不激活，
    # 0.2 通用找回（0.25 实测仍漏）；代价个别 rec 空格波动。
    return PaddleOCR(
        enable_mkldnn=False,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        text_det_limit_type="min",
        text_det_limit_side_len=64,
        text_det_thresh=0.2,
        text_det_box_thresh=0.6,
        text_det_unclip_ratio=1.5,
        text_rec_score_thresh=0,
    )


def main():
    print("Hello from bill-recognition!")

    path = input("请输入要解析的文件：").strip()
    pages = [_clean_page(arr) for arr in load_pages(path)]

    ocr = build_ocr()
    results = list(ocr.predict(pages))

    doc = [
        parse_page(arr, res["rec_texts"], res["rec_boxes"], page_idx=i)
        for i, (arr, res) in enumerate(zip(pages, results))
    ]

    os.makedirs("output", exist_ok=True)
    name = os.path.splitext(os.path.basename(path))[0]
    out = os.path.join("output", f"{name}_structured.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    print(f"saved: {out}")


if __name__ == "__main__":
    main()
