#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实测转录比对工具 —— 原始实录 vs 整理稿

用纯程序的方式回答"整理过程到底动了什么"，供工作审查使用：
  · 差异定位：difflib.SequenceMatcher 字符级对齐，增/删/改逐字精确
  · 页码归属：由对齐结果自动推导，脚本不接收任何人工映射
  · 内部对齐检查：原文每个字符是否都被归属且仅归属一处、块的顺序是否被调换

检查只描述内部对齐，不能证明没有漏写、改序或编造。

自包含：只用 Python 标准库（difflib / unicodedata / re / html），不依赖任何
外部路径、外部包、外部配置。所有输入都来自命令行参数。

用法：
    python3 diff_transcript.py <原始实录> <整理稿> [选项]

选项：
    --out DIR          输出目录（默认：<整理稿同目录>/<整理稿名>_diff）
    --col N            指定整理稿中的实录列（从 1 开始）；默认自动识别
    --page-col N       指定整理稿中的页码列（从 1 开始）；默认自动识别
    --pages DIR        逐页截图目录（含 P001.jpg… 与可选的 _pages.json）。
                       给了就在报告里加一列 PPT 缩略图，把报告变成逐页工作台
    --no-inject        不把修改清单回填到整理稿
    --quiet            只输出结论

产出（写入 --out 目录）：
    index.html         可视化比对报告；带 --pages 时可展示 PPT、英文正文与逐页译稿
    changelog.md       完整修改清单（分类、带上下文）
    changes.diff       行级 unified diff
    pages/             从 --pages 拷来的逐页截图（报告目录整体可移动）

整理稿的两种形态都支持，自动识别：
    逐页对应模式 —— Markdown 表格，含页码列与实录列
    纯文字整理模式 —— 普通 Markdown，整篇与原文比对
"""

import argparse
import difflib
import html
import json
import os
import re
import shutil
import sys
import unicodedata

# 整理稿里用这对标记圈出修改清单的落位；脚本只替换标记之间的内容，幂等。
# 这对标记同时也用于「比对前剔除生成内容」——见 strip_generated_blocks()。
CHANGELOG_START = "<!-- CHANGELOG:START -->"
CHANGELOG_END = "<!-- CHANGELOG:END -->"

# ---------------------------------------------------------------------------
# 字数口径
# ---------------------------------------------------------------------------
# 中文排版领域常见的两种「字数」口径，本工具同时给出，避免对不上账：
#
#   ① 近似口径：汉字与中文标点各计 1，连续的字母数字串各计 1 个词。
#      这是本脚本的近似统计，不保证与办公软件一致。
#   ② 字符口径：去掉换行后的字符数，ASCII 每个字符算 1，空格与半角符号也计入。
#      内部对齐检查必须用它（包括被删字符的对齐归属）。
#
# 两种口径的换算关系：字符口径 - ASCII 折叠数 - 不计入的空格符号 = 近似口径。

# 近似统计计入的码位区间：CJK 标点、CJK 汉字（含扩展）、CJK 兼容、全角形式
CJK_RANGES = (
    (0x3000, 0x303F),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0xFF00, 0xFFEF),
    (0x20000, 0x2FA1F),
)
ASCII_WORD_RE = re.compile(r"[0-9A-Za-z]+")


def is_cjk(ch):
    o = ord(ch)
    return any(lo <= o <= hi for lo, hi in CJK_RANGES)


def wps_count(text):
    """近似口径字数。返回 (字数, 汉字与中文标点数, 字母数字词数)"""
    cjk = sum(1 for ch in text if is_cjk(ch))
    words = len(ASCII_WORD_RE.findall(text))
    return cjk + words, cjk, words


def char_count(text):
    """字符口径：去掉换行后的字符数"""
    return len(text.replace("\n", ""))


def is_punct_or_space(ch):
    """是否为标点或空白（用 Unicode 类别判定，不维护任何标点字表）"""
    if ch.isspace():
        return True
    return unicodedata.category(ch).startswith("P")


# ---------------------------------------------------------------------------
# 解析整理稿：自动识别形态与列
# ---------------------------------------------------------------------------
PAGE_LABEL_RE = re.compile(r"^\s*(?:P|第)\s*(\d+)\s*页?\s*$", re.I)
BARE_NUM_RE = re.compile(r"^\s*(\d+)\s*$")


def split_row(line):
    """拆分 Markdown 表格行，正确处理 \\| 转义"""
    s = line.strip()
    if not s.startswith("|"):
        return None
    s = s.replace("\\|", "\x00")
    cells = s[1:-1].split("|") if s.endswith("|") else s[1:].split("|")
    return [c.replace("\x00", "|").strip() for c in cells]


def is_separator_row(cells):
    # 允许 1 个以上短横线：本项目表格用 |---|---| 与 |-|-|-| 两种写法
    return bool(cells) and all(re.fullmatch(r":?-+:?", (c or "").strip()) for c in cells)


def page_label_of(cell):
    """
    从单元格里提取页码标识，提取不到返回 None。
    单元格可能是 "P1"、"第1页"、"1"，也可能带着图片或说明，如 "P1<br/>![](...)"。
    """
    if not cell:
        return None
    first = re.split(r"<br\s*/?>|\n", cell, flags=re.I)[0]
    first = re.sub(r"[*_`\[\]]", "", first).strip()
    m = PAGE_LABEL_RE.match(first) or BARE_NUM_RE.match(first)
    return m.group(1) if m else None


def find_tables(lines):
    """找出文件里所有 Markdown 表格，返回 [[行单元格, ...], ...]"""
    tables, cur = [], []
    for line in lines:
        cells = split_row(line)
        if cells is None:
            if cur:
                tables.append(cur)
                cur = []
            continue
        cur.append(cells)
    if cur:
        tables.append(cur)
    out = []
    for t in tables:
        rows = [r for r in t if not is_separator_row(r)]
        if len(rows) >= 2:
            out.append(rows)
    return out


def detect_columns(rows, original_text):
    """
    自动识别页码列与实录列，不依赖列序。

    页码列：表头之外的单元格大多能解析出页码标识（P1 / 第1页），
            或为严格递增的连续整数。
    实录列：整列文本与原始实录的相似度最高的那一列 —— 用一个自校验的
            标准来定位，而不是写死"最后一列"。
    """
    body = rows[1:] if len(rows) > 1 else rows
    ncols = max(len(r) for r in body)

    def col_texts(ci):
        return [r[ci] if ci < len(r) else "" for r in body]

    # 页码列：大多数单元格能解析出页码标识即可认定
    page_col = None
    for ci in range(ncols):
        texts = col_texts(ci)
        if texts and sum(1 for t in texts if page_label_of(t)) >= 0.8 * len(texts):
            page_col = ci
            break

    # 实录列：与原始实录最像的一列
    scores = []
    for ci in range(ncols):
        if ci == page_col:
            scores.append((-1.0, ci))
            continue
        text = "\n".join(col_texts(ci))
        if not text:
            scores.append((-1.0, ci))
            continue
        scores.append((difflib.SequenceMatcher(None, original_text, text).ratio(), ci))
    scores.sort(reverse=True)
    col_col = scores[0][1] if scores and scores[0][0] > 0 else None

    return page_col, col_col, scores


def normalize_cell(text):
    """把单元格还原成文本：<br/> 当换行，去掉每行首尾空白，保留有意的段间空行"""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    return "\n".join(s.strip() for s in text.split("\n")).strip()


def load_original(path):
    with open(path, encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip()]


def strip_markdown_scaffolding(lines):
    """纯文字整理模式下，去掉纯装饰的 Markdown 行，只留正文"""
    out = []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if s.startswith("```"):
            continue
        s = re.sub(r"^#{1,6}\s*", "", s)
        if s:
            out.append(s)
    return out


def strip_generated_blocks(src, start=CHANGELOG_START, end=CHANGELOG_END):
    """
    去掉整理稿里由本工具生成的内容（标记之间的修改清单）。
    不这样做的话，上一次回填的清单会被当成正文再次参与比对：
    既污染字数与差异统计，也会让重复运行的结果不断漂移（破坏幂等）。
    """
    while start in src and end in src:
        a = src.index(start)
        b = src.index(end, a) + len(end)
        src = src[:a] + src[b:]
    return src


def load_organized(path, original_text, col=None, page_col=None, translation_col=None):
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    lines = strip_generated_blocks(raw).split("\n")

    # 先按表格形态解析；用户显式指定的列号只覆盖识别结果，不影响是否走表格
    tables = find_tables(lines)
    if len(tables) > 1:
        raise ValueError("Multiple tables: compare each transcript separately, or combine its body into one table.")
    best, rows, scores = None, None, []
    for rows in tables:
        pc, cc, scores = detect_columns(rows, original_text)
        if cc is None:
            continue
        score_of = {ci: score for score, ci in scores}
        sc = score_of.get(cc, 0.0)
        if best is None or sc > best[0]:
            best = (sc, rows, pc, cc, scores)
    if best is not None:
        sc, rows, pc, cc, scores = best
        page_col = page_col if page_col is not None else pc
        col = col if col is not None else cc
        ncols = max(len(r) for r in rows)
        if col >= ncols:
            raise ValueError("Transcript column is out of range")
        if translation_col is not None and translation_col >= ncols:
            raise ValueError("Translation column is out of range")
        if translation_col is not None and translation_col in (col, page_col):
            raise ValueError("Translation, transcript and page columns must be different")
    if col is not None and rows is not None:
        body = rows[1:]
        pages = []
        page_notes = {}
        translations = {}
        for i, r in enumerate(body):
            text = normalize_cell(r[col] if col < len(r) else "")
            if page_col is not None and page_col < len(r):
                num = page_label_of(r[page_col])
                label = "P%s" % num if num else "第%d块" % (i + 1)
            else:
                label = "第%d块" % (i + 1)
            if label in page_notes:
                raise ValueError("Use unique unit labels; returning to the same slide needs a new unit ID")
            if translation_col is not None:
                translated = normalize_cell(r[translation_col] if translation_col < len(r) else "")
                if not translated:
                    raise ValueError("Missing translation for " + label)
                translations[label] = translated
            note_parts = []
            for ci, cell in enumerate(r):
                if ci in (col, page_col, translation_col):
                    continue
                # Preserve source times, titles and uncertainty notes; images are rendered separately.
                note = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", cell)
                note = normalize_cell(note)
                if note.strip():
                    note_parts.append(note)
            page_notes[label] = "\n".join(note_parts)
            pages.append((label, text))
        if pages:
            return {"mode": "table", "pages": pages, "col": col + 1,
                    "page_col": None if page_col is None else page_col + 1,
                    "scores": scores, "page_notes": page_notes,
                    "translations": translations}

    # 不是表格形态（或没找到可信的实录列）→ 按整篇比对
    body = strip_markdown_scaffolding(lines)
    return {"mode": "plain", "pages": [("全文", "\n".join(body))],
            "col": None, "page_col": None, "scores": [], "translations": {}}


# ---------------------------------------------------------------------------
# 对齐与校验
# ---------------------------------------------------------------------------
def build_ranges(units):
    parts, ranges, cur = [], [], 0
    for i, u in enumerate(units):
        if i:
            parts.append("\n")
            cur += 1
        ranges.append((cur, cur + len(u)))
        parts.append(u)
        cur += len(u)
    return "".join(parts), ranges


def analyze(orig_units, pages):
    orig_text, _ = build_ranges(orig_units)
    page_texts = [t for _, t in pages]
    org_text, page_ranges = build_ranges(page_texts)

    opcodes = difflib.SequenceMatcher(None, orig_text, org_text, autojunk=False).get_opcodes()

    if not orig_text.strip() or not org_text.strip():
        raise ValueError("Original and organized transcript must both contain text")
    n_pages = len(page_ranges)
    page_of_bpos = [0] * len(org_text)
    for idx, (bs, be) in enumerate(page_ranges):
        for j in range(bs, be):
            page_of_bpos[j] = idx
    for idx in range(n_pages - 1):
        page_of_bpos[page_ranges[idx][1]] = idx

    changed_orig, changed_org = set(), set()
    left_segs = [[] for _ in range(n_pages)]
    hunks = []

    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            # equal 是 1:1 字符对应，按块边界精确切分（归属由此自动推导）
            i, j = i1, j1
            while j < j2:
                p = page_of_bpos[j]
                _, be = page_ranges[p]
                if be <= j:
                    i += 1
                    j += 1
                    continue
                seg_end = min(j2, be)
                left_segs[p].append((i, i + (seg_end - j)))
                i += seg_end - j
                j = seg_end
            continue

        p = page_of_bpos[j1] if j2 > j1 else page_of_bpos[min(j1, len(org_text) - 1)]
        if i2 > i1:
            left_segs[p].append((i1, i2))
            changed_orig.update(range(i1, i2))
        if j2 > j1:
            changed_org.update(range(j1, j2))
        hunks.append({
            "page_idx": p,
            "page": pages[p][0],
            "orig": orig_text[i1:i2],
            "org": org_text[j1:j2],
            "i1": i1, "i2": i2, "j1": j1, "j2": j2,
            "kind": {"replace": "改", "delete": "删", "insert": "增"}[tag],
        })

    left_texts = []
    for segs in left_segs:
        merged = []
        for s, e in sorted(segs):
            if merged and s <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        piece, prev = [], None
        for s, e in merged:
            if prev is not None and s != prev:
                piece.append("\n")
            piece.append(orig_text[s:e])
            prev = e
        left_texts.append("".join(piece))

    return {
        "orig_text": orig_text, "org_text": org_text,
        "page_ranges": page_ranges, "left_texts": left_texts,
        "left_segs": left_segs,
        "changed_orig": changed_orig, "changed_org": changed_org,
        "hunks": hunks,
    }


def verify_integrity(res, pages, orig_text):
    """
    程序化校验：
      ① 原文每个字符都被归属，且仅归属一处  → 对齐记录内部一致，包含删除
      ② 块归属随原文档位单调递增            → 对齐记录单调，不能证明现场顺序
    """
    n = len(orig_text)
    owner = [None] * n
    conflicts = []
    for p, segs in enumerate(res["left_segs"]):
        for s, e in segs:
            for k in range(s, e):
                if owner[k] is not None and owner[k] != p:
                    conflicts.append(k)
                owner[k] = p
    unassigned = [k for k in range(n) if owner[k] is None and orig_text[k] != "\n"]

    spans = []
    for p in range(len(pages)):
        offs = [k for k in range(n) if owner[k] == p]
        spans.append((min(offs), max(offs)) if offs else None)
    nonmono = []
    for p in range(len(pages) - 1):
        a, b = spans[p], spans[p + 1]
        if a and b and a[1] > b[0]:
            nonmono.append((pages[p][0], pages[p + 1][0]))

    return {"conflicts": conflicts, "unassigned": unassigned,
            "nonmono": nonmono, "spans": spans,
            "ok": not conflicts and not unassigned and not nonmono}


# ---------------------------------------------------------------------------
# 分类与渲染
# ---------------------------------------------------------------------------
def classify(hunks):
    """只按字符性质机械分类，不维护任何词表"""
    punct, words = [], []
    for h in hunks:
        chars = h["orig"] + h["org"]
        (punct if chars and all(is_punct_or_space(c) for c in chars) else words).append(h)
    return punct, words


def render_highlight(text, is_changed, cls):
    out, buf, state = [], [], None
    for idx, ch in enumerate(text):
        cur = is_changed(idx)
        if cur != state and buf:
            body = html.escape("".join(buf))
            out.append("<%s>%s</%s>" % (cls, body, cls) if state else body)
            buf = []
        state = cur
        buf.append(ch)
    if buf:
        body = html.escape("".join(buf))
        out.append("<%s>%s</%s>" % (cls, body, cls) if state else body)
    return "".join(out).replace("\n", "<br/>")


def ctx(text, a, b, width=10):
    pre = text[max(0, a - width):a].replace("\n", "⏎")
    post = text[b:b + width].replace("\n", "⏎")
    mid = text[a:b].replace("\n", "⏎")
    return pre.replace("|", "\\|"), ("**%s**" % mid if mid else ""), post.replace("|", "\\|")


def hunk_rows(items, orig_text, org_text):
    out = []
    for h in items:
        pa, ma, sa = ctx(orig_text, h["i1"], h["i2"])
        pb, mb, sb = ctx(org_text, h["j1"], h["j2"])
        left = ma or "（空）"
        right = mb or "（空）"
        out.append("| %s | %s | `%s`%s`%s` | `%s`%s`%s` |"
                   % (h["page"], {"改": "替换", "删": "删除", "增": "新增"}[h["kind"]],
                      pa, left, sa, pb, right, sb))
    return "\n".join(out)


CSS = """
:root{--del:#c0392b;--delbg:#fdecea;--ins:#1a7f37;--insbg:#e6f4ea;--line:#e5e7eb;--muted:#6b7280}
*{box-sizing:border-box}
body{margin:0;padding:32px 24px;font:15px/1.9 -apple-system,"PingFang SC","Helvetica Neue",sans-serif;
     color:#1f2328;background:#fafafa}
h1{font-size:20px;margin:0 0 4px}
.sub{color:var(--muted);font-size:13px;margin-bottom:20px}
.summary{background:#fff;border:1px solid var(--line);border-radius:10px;padding:16px 20px;margin-bottom:24px;
         display:flex;gap:36px;flex-wrap:wrap;font-size:13px}
.summary div b{display:block;font-size:20px;font-weight:600;color:#111}
.summary .lbl{color:var(--muted)}
.card{background:#fff;border:1px solid var(--line);border-radius:10px;margin-bottom:20px;overflow:hidden}
.source-note{font-size:13px;color:#664d03;background:#fff8e1;padding:8px 10px;margin:8px 0;white-space:normal}
.card header{padding:10px 18px;border-bottom:1px solid var(--line);background:#f6f8fa;
             display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
.card h2{margin:0;font-size:14px;letter-spacing:.5px}
.meta{color:var(--muted);font-size:12px}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:0}
.cols.with-ppt{grid-template-columns:minmax(200px,0.9fr) 1.05fr 1.05fr}
.cols.with-ppt.with-translation{grid-template-columns:minmax(200px,0.85fr) 1fr 1.08fr 1.08fr}
/* 阅读视图（默认）：只留 PPT 与整理后实录；「对照改动」再展开原始实录与改动清单 */
body.view-read .technical,body.view-read .summary,body.view-read .verify,body.view-read .card header .meta{display:none}
body.view-read .col.orig,body.view-read .hunklist{display:none}
/* 阅读视图不标改动：补标点会让几乎每个句子都带高亮，读起来很花 */
body.view-read ins{background:transparent;color:inherit;font-weight:inherit}
body.view-read .cols.with-ppt{grid-template-columns:minmax(220px,0.85fr) 1.6fr}
body.view-read .cols.with-ppt.with-translation{grid-template-columns:minmax(220px,0.85fr) minmax(0,1.15fr) minmax(0,1.15fr)}
@media(max-width:1100px){.cols.with-ppt{grid-template-columns:1fr 1fr}
  .cols.with-ppt .col.ppt{grid-column:1/-1}
  .cols.with-ppt.with-translation{grid-template-columns:repeat(3,minmax(0,1fr))}
  body.view-read .cols.with-ppt.with-translation{grid-template-columns:1fr 1fr}}
@media(max-width:900px){.cols,.cols.with-ppt,.cols.with-ppt.with-translation,body.view-read .cols.with-ppt,body.view-read .cols.with-ppt.with-translation{grid-template-columns:1fr}
  .cols.with-ppt .col.ppt{grid-column:auto}}
.col{padding:14px 18px}
.col+.col{border-left:1px solid var(--line)}
.colhead{font-size:11px;letter-spacing:1px;color:var(--muted);margin-bottom:8px;text-transform:uppercase}
.col.ppt{background:#fbfbfc}
.col.ppt img{width:100%;height:auto;display:block;border:1px solid var(--line);border-radius:6px;background:#fff}
.viewswitch{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden;background:#fff;vertical-align:middle}
.viewswitch button{border:0;background:transparent;padding:7px 16px;font:inherit;font-size:13px;cursor:pointer;color:var(--muted)}
.viewswitch button[aria-pressed="true"]{background:#1f2328;color:#fff;font-weight:600}
.body{white-space:normal;word-break:break-word}
.seg+.seg{margin-top:10px;padding-top:10px;border-top:1px dotted #e5e7eb}
del{background:var(--delbg);color:var(--del);text-decoration:line-through;border-radius:2px;padding:0 1px}
ins{background:var(--insbg);color:var(--ins);text-decoration:none;font-weight:600;border-radius:2px;padding:0 1px}
.hunklist{margin:0;padding:8px 18px 14px;border-top:1px dashed var(--line);list-style:none;font-size:13px}
.hunklist li{padding:3px 0;display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.tag{font-size:11px;border-radius:3px;padding:1px 6px;flex:none}
.t-mod{background:#fff4e5;color:#9a6700}.t-del{background:var(--delbg);color:var(--del)}.t-ins{background:var(--insbg);color:var(--ins)}
.old{color:var(--del);background:var(--delbg);padding:0 2px;border-radius:2px}
.new{color:var(--ins);background:var(--insbg);padding:0 2px;border-radius:2px}
.arrow{color:var(--muted)}
.empty{color:var(--muted);font-style:italic}
.verify{font-size:13px;padding:10px 16px;border-radius:8px;margin:0 0 20px;line-height:1.7}
.verify.ok{background:#e6f4ea;border:1px solid #b7e0c3;color:#1a7f37}
.verify.bad{background:#fdecea;border:1px solid #f5c6c2;color:#c0392b}
.verify b{font-weight:600}
.verify span{color:#4b5563}
footer{color:var(--muted);font-size:12px;margin-top:28px;line-height:1.8}
code{background:#f0f1f3;padding:1px 5px;border-radius:3px;font-size:12px}
"""


def retention_note(o_wps, n_wps, mode):
    """保留率异常时的解释。纯文字模式新增标题/分段会推高保留率，不是错误。"""
    if not o_wps:
        return ""
    r = n_wps / o_wps * 100
    if r > 100:
        return ("整理后比原文长（%.1f%%）。纯文字模式常见：整理稿新增了标题、说明行或分段，"
                "这些会以「新增」出现在差异清单里；逐页对应模式出现这一提示则需检查是否混入了参考稿内容。" % r)
    return ""


IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def collect_page_images(pages, pages_dir, outdir):
    """把每页对应的 PPT 截图收进报告目录，返回 {页码标签: {"src":…, "seg":(起,止)}}。

    页码标签形如 "P1"，截图文件名形如 "P001.jpg" —— 按数字对齐，不做字符串匹配。
    图片拷进 <outdir>/pages/ 并按相对路径引用，报告目录整体可移动、可分享。
    """
    if not pages_dir or not os.path.isdir(pages_dir):
        return {}

    meta = {}
    meta_path = os.path.join(pages_dir, "_pages.json")
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, encoding="utf-8") as f:
                for row in json.load(f):
                    meta[str(row.get("page", ""))] = row
        except (OSError, ValueError):
            pass  # _pages.json 只是锦上添花，读不了就只显示图

    dest_dir = os.path.join(outdir, "pages")
    found = {}
    for label, _ in pages:
        num = page_label_of(label)
        if not num:
            continue
        n = int(num)
        src = None
        for stem in ("P%03d" % n, "P%02d" % n, "P%d" % n, "%d" % n):
            for ext in IMAGE_EXTS:
                cand = os.path.join(pages_dir, stem + ext)
                if os.path.isfile(cand):
                    src = cand
                    break
            if src:
                break
        if not src:
            continue
        os.makedirs(dest_dir, exist_ok=True)
        name = "P%03d%s" % (n, os.path.splitext(src)[1].lower())
        shutil.copyfile(src, os.path.join(dest_dir, name))
        row = meta.get("P%03d" % n) or meta.get("P%d" % n) or meta.get(str(n)) or {}
        found[label] = {"src": "pages/" + name,
                        "seg": (row.get("seg_start"), row.get("seg_end"))}
    return found


def build_html(orig_path, org_path, res, pages, ver, mode_label, script_name, page_images=None, page_notes=None, translations=None):
    o_wps, _, _ = wps_count(res["orig_text"])
    n_wps, _, _ = wps_count(res["org_text"])
    o_ch, n_ch = char_count(res["orig_text"]), char_count(res["org_text"])
    n_mod = sum(1 for h in res["hunks"] if h["kind"] == "改")
    n_del = sum(1 for h in res["hunks"] if h["kind"] == "删")
    n_ins = sum(1 for h in res["hunks"] if h["kind"] == "增")

    cards = []
    for idx, (label, _) in enumerate(pages):
        merged = []
        for s, e in sorted(res["left_segs"][idx]):
            if merged and s <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        lo = merged[0][0] if merged else 0
        hi = merged[-1][1] if merged else 0

        left_parts = [render_highlight(res["orig_text"][s:e],
                                       lambda k, base=s: (base + k) in res["changed_orig"], "del")
                      for s, e in merged]
        left_html = "".join('<div class="seg">%s</div>' % p for p in left_parts)

        bs, be = res["page_ranges"][idx]
        right_html = render_highlight(res["org_text"][bs:be],
                                      lambda k: (bs + k) in res["changed_org"], "ins")

        left_wps, _, _ = wps_count(res["left_texts"][idx])
        right_wps, _, _ = wps_count(res["org_text"][bs:be])
        ph = [h for h in res["hunks"] if h["page_idx"] == idx]
        ctx_html = ""
        if ph:
            items = "".join(
                '<li><span class="tag %s">%s</span><span class="old">%s</span>'
                '<span class="arrow">→</span><span class="new">%s</span></li>'
                % ({"改": "t-mod", "删": "t-del", "增": "t-ins"}[h["kind"]],
                   h["kind"],
                   html.escape(h["orig"]) or "<em>（空）</em>",
                   html.escape(h["org"]) or "<em>（空）</em>")
                for h in ph)
            ctx_html = '<ul class="hunklist">%s</ul>' % items

        img = (page_images or {}).get(label)
        if img:
            a, b = img.get("seg") or (None, None)
            when = "　%.1f–%.1fs" % (a, b) if a is not None and b is not None else ""
            # 不加 loading="lazy"：工作台就是要让人一次扫完所有页，懒加载会只渲染首屏那几张
            ppt_html = ('<div class="col ppt"><div class="colhead">PPT 页面%s</div>'
                        '<a href="%s" target="_blank"><img src="%s" alt="%s"/></a></div>'
                        % (when, html.escape(img["src"], quote=True),
                           html.escape(img["src"], quote=True), html.escape(label)))
        else:
            ppt_html = '<div class="col ppt"><b>缺少页面图片，需补齐或说明原因</b></div>' if page_images is not None else ""
        note_text = (page_notes or {}).get(label, "")
        if note_text:
            note_html = '<div class="source-note">%s</div>' % html.escape(note_text).replace("\n", "<br/>")
            ppt_html = ppt_html + note_html if not ppt_html else ppt_html.replace("</div>", note_html + "</div>", 1)
        translated = (translations or {}).get(label)
        translation_html = ('<div class="col translation" lang="zh-CN"><div class="colhead">中文译稿</div>'
                            '<div class="body">%s</div></div>'
                            % html.escape(translated).replace("\n", "<br/>") if translated else "")
        cols_cls = ("cols with-ppt" if ppt_html else "cols") + (" with-translation" if translated else "")

        cards.append("""
<section class="card">
  <header>
    <h2>%s</h2>
    <span class="meta">原文对应字符区间 %d–%d · 原文 %d 字 → 整理后 %d 字（近似口径） · <b>%d 处差异</b></span>
  </header>
  <div class="%s">
    %s
    <div class="col orig"><div class="colhead">原始实录（本块对应部分）</div>
      <div class="body">%s</div></div>
    <div class="col"><div class="colhead">%s</div>
      <div class="body">%s</div></div>
    %s
  </div>
  %s
</section>""" % (html.escape(label), lo, hi, left_wps, right_wps, len(ph),
                 cols_cls, ppt_html,
                 left_html or '<span class="empty">（本块无对应原文）</span>',
                 "英文演讲稿" if translated else "整理后实录",
                 right_html or '<span class="empty">（本块留空）</span>',
                 translation_html, ctx_html))

    ok = ver["ok"]
    detail = []
    if ver["conflicts"]:
        detail.append("有 %d 个字符被归属到多个块" % len(ver["conflicts"]))
    if ver["unassigned"]:
        detail.append("有 %d 个字符未被归属" % len(ver["unassigned"]))
    if ver["nonmono"]:
        detail.append("块归属非单调：%s" % ver["nonmono"])
    banner = ('<div class="verify %s">%s <b>内部对齐检查：%s</b>　'
              '<span>本次检查 %d 个原文字符的内部归属；'
              '被删内容也参与归属。此结果不证明无遗漏、顺序正确或没有编造；需回查原材料%s</span></div>'
              % ("ok" if ok else "bad", "✔" if ok else "✘", "PASS" if ok else "FAIL",
                 char_count(res["orig_text"]),
                 "" if ok else "：" + "；".join(detail)))

    has_ppt = page_images is not None
    has_translation = bool(translations)
    page_title = ("逐页阅读：PPT 与中英演讲稿" if has_translation else "逐页阅读：PPT 与演讲稿") if has_ppt else "实录比对报告"
    lead = ((("每页依次显示 PPT、英文演讲稿和中文译稿。" if has_translation else
              "每页一行：左边是这一页的 PPT，右边是整理后的演讲稿。") +
             "默认是阅读视图；切到「对照改动」可以看到原始实录和每一处改动。")
            if has_ppt else "左右两栏对照：左边原文、右边整理后，行内高亮标出每一处改动。")
    ppt_note = ("<br/>点 PPT 图可看原图。"
                if has_ppt else
                "<br/>加 <code>--pages &lt;截图目录&gt;</code> 可把每页 PPT 一并放进报告。")

    view_class = "view-read" if has_ppt else ""
    view_switch = ("""
<div class="viewbar" style="margin:0 0 18px">
  <span class="viewswitch" role="group" aria-label="视图切换">
    <button id="btn-read" type="button" aria-pressed="true">阅读（%s）</button>
    <button id="btn-diff" type="button" aria-pressed="false">对照改动</button>
  </span>
  <span class="meta" style="margin-left:12px">对照视图会展开「原始实录」列与逐条改动清单</span>
</div>""" % ("PPT + 英文 + 中文" if has_translation else "PPT + 整理后实录") if has_ppt else "")
    view_script = """
<script>
(function () {
  var read = document.getElementById('btn-read'), diff = document.getElementById('btn-diff');
  if (!read || !diff) return;
  function set(mode) {
    var isRead = mode === 'read';
    document.body.classList.toggle('view-read', isRead);
    read.setAttribute('aria-pressed', String(isRead));
    diff.setAttribute('aria-pressed', String(!isRead));
  }
  read.addEventListener('click', function () { set('read'); });
  diff.addEventListener('click', function () { set('diff'); });
})();
</script>""" if has_ppt else ""

    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>%s</title>
<style>%s</style></head><body class="%s">
<h1>%s</h1>
%s
<div class="sub technical">A = <code>%s</code>　B = <code>%s</code>　·　形态：%s　·
算法：<code>difflib.SequenceMatcher</code> 字符级对齐，块归属由对齐结果自动推导，无人工映射参数<br/>
%s</div>

%s

<div class="summary">
  <div><span class="lbl">原始字数（近似口径）</span><b>%d</b></div>
  <div><span class="lbl">整理后字数（近似口径）</span><b>%d</b></div>
  <div><span class="lbl">保留比例</span><b>%.1f%%</b></div>
  <div><span class="lbl">替换 / 删除 / 新增（字符级）</span><b>%d / %d / %d</b></div>
  <div><span class="lbl">差异块</span><b>%d</b></div>
</div>
<div class="sub technical" style="margin:-12px 0 20px">
  字数口径＝近似字数算法：汉字与中文标点各计 1，连续字母数字串各计 1 个词，半角空格与符号不计数。
  按字符口径（去换行字符数）则为 <b>%d</b> → <b>%d</b>；两者差值来自 ASCII 折叠与空格。
  「替换 / 删除 / 新增」是差异块的字符级统计，与字数是不同口径，勿混用。<br/>
  %s
</div>

%s

<footer class="technical">
  红底删除线 = 原文有、整理后去掉或改写的字；绿底加粗 = 整理后新增或改写的字。<br/>
  每块「原始实录」只显示与整理稿对齐后归入该块的原文区间，拼接顺序即原文顺序，未被移动到别块。%s<br/>
  由 <code>%s</code> 生成，重新运行即可复现。
</footer>%s
</body></html>""" % (
        page_title, CSS, view_class, page_title,
        view_switch,
        html.escape(orig_path), html.escape(org_path), html.escape(mode_label),
        lead,
        banner,
        o_wps, n_wps, (n_wps / o_wps * 100) if o_wps else 0.0,
        n_mod, n_del, n_ins, len(res["hunks"]),
        o_ch, n_ch,
        retention_note(o_wps, n_wps, mode_label),
        "\n".join(cards), ppt_note, html.escape(script_name), view_script)


def build_changelog(orig_name, org_name, res, pages, ver, mode_label,
                    include_header=True, script_name="diff_transcript.py"):
    punct, words = classify(res["hunks"])
    o_wps, _, _ = wps_count(res["orig_text"])
    n_wps, _, _ = wps_count(res["org_text"])
    o_ch, n_ch = char_count(res["orig_text"]), char_count(res["org_text"])

    header = """# 修改清单（程序生成）

> 本文件由 `%s` 根据原文与整理稿的差异生成。条目、上下文与分类由
> `difflib.SequenceMatcher` 字符级对齐结果推导，重新运行即可复现：

```bash
python3 %s "%s" "%s"
```

""" % (script_name, script_name, orig_name, org_name)

    body = """| 指标 | 数值 |
|---|---|
| 形态 | %s |
| 原始字数（**近似口径**） | %d |
| 整理后字数（**近似口径**） | %d |
| 保留比例 | %.1f%% |
| 原始 / 整理后（字符口径：去换行字符数） | %d / %d |
| 差异块总数 | **%d** |
| ├ 标点 / 空白调整 | %d |
| └ 字词调整 | %d |
| 内部对齐检查 | **%s** |

> **字数口径**：近似字数算法＝汉字与中文标点各计 1 ＋ 连续字母数字串各计 1 个词，
> 半角空格与符号不计数。字符口径为去换行后的字符数，ASCII 每字符算 1，
> 内部对齐检查用它（包括被删字符的对齐归属）。
> 本清单与字数均已剔除整理稿里由本工具生成的内容（标记之间的部分），重复运行结果稳定。

%s
> **分类口径（机械规则，无词表）**：改动涉及的字符**全部**属于 Unicode 标点类或空白类 →
> 「标点 / 空白调整」；否则 → 「字词调整」。字词调整里既有转写错别字修正，也有顺句改写，
> 本工具不做语义区分，请人工复核。

---

## 一、标点 / 空白调整（%d 处）

| 块 | 类型 | 原文（含上下文） | 整理后（含上下文） |
|---|---|---|---|
%s

## 二、字词调整（%d 处）

| 块 | 类型 | 原文（含上下文） | 整理后（含上下文） |
|---|---|---|---|
%s
""" % (mode_label, o_wps, n_wps, (n_wps / o_wps * 100) if o_wps else 0.0, o_ch, n_ch,
       len(res["hunks"]), len(punct), len(words), "PASS" if ver["ok"] else "FAIL",
       retention_note(o_wps, n_wps, mode_label),
       len(punct), hunk_rows(punct, res["orig_text"], res["org_text"]),
       len(words), hunk_rows(words, res["orig_text"], res["org_text"]))

    return header + body if include_header else body


def split_sentences(text):
    out = []
    for para in text.split("\n"):
        if not para.strip():
            continue
        for s in re.split(r"(?<=[。！？])", para):
            if s.strip():
                out.append(s.strip())
    return out


def inline_word_diff(a, b):
    oa, ob = [], []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            oa.append(a[i1:i2])
            ob.append(b[j1:j2])
        else:
            if i2 > i1:
                oa.append("[-%s-]" % a[i1:i2])
            if j2 > j1:
                ob.append("{+%s+}" % b[j1:j2])
    return "".join(oa), "".join(ob)


def build_diff(orig_path, org_path, orig_text, org_text):
    a, b = split_sentences(orig_text), split_sentences(org_text)
    lines = ["--- %s" % orig_path, "+++ %s" % org_path,
             "# 行内标记：[-原文-] 删除/改写处，{+整理后+} 新增/改写处", ""]
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            lines += ["  " + a[k] for k in range(i1, i2)]
        elif tag == "replace" and (i2 - i1) == (j2 - j1):
            for k in range(i2 - i1):
                oa, ob = inline_word_diff(a[i1 + k], b[j1 + k])
                if oa != a[i1 + k]:
                    lines += ["- " + oa, "+ " + ob]
                else:
                    lines.append("  " + a[i1 + k])
        else:
            lines += ["- " + a[k] for k in range(i1, i2)]
            lines += ["+ " + b[k] for k in range(j1, j2)]
    return "\n".join(lines) + "\n"


def inject_changelog(path, body, start=CHANGELOG_START, end=CHANGELOG_END):
    with open(path, encoding="utf-8") as f:
        src = f.read()
    if start not in src or end not in src:
        return False
    a = src.index(start) + len(start)
    b = src.index(end)
    with open(path, "w", encoding="utf-8") as f:
        f.write(src[:a] + "\n\n" + body.rstrip() + "\n\n" + src[b:])
    return True


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="实录比对：原始实录 vs 整理稿（程序生成 diff + 内部对齐检查）")
    ap.add_argument("original")
    ap.add_argument("organized")
    ap.add_argument("--out")
    ap.add_argument("--col", type=int)
    ap.add_argument("--page-col", type=int)
    ap.add_argument("--translation-col", type=int, help="逐页译稿列，从 1 开始；只展示，不参与原文改动比对")
    ap.add_argument("--pages", help="逐页截图目录（P001.jpg… 及可选 _pages.json）；给了就在报告里加 PPT 列")
    ap.add_argument("--no-inject", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    if any(v is not None and v < 1 for v in (args.col, args.page_col, args.translation_col)):
        ap.error("column numbers start at 1")

    script_name = os.path.basename(__file__)
    orig_units = load_original(args.original)
    orig_text = "\n".join(orig_units)

    parsed = load_organized(args.organized, orig_text,
                            col=(args.col - 1) if args.col else None,
                            page_col=(args.page_col - 1) if args.page_col else None,
                            translation_col=(args.translation_col - 1) if args.translation_col else None)
    if args.translation_col and parsed["mode"] != "table":
        ap.error("--translation-col requires a Markdown table with a transcript column")
    pages = parsed["pages"]

    res = analyze(orig_units, pages)
    ver = verify_integrity(res, pages, res["orig_text"])

    mode_label = "逐页对应（表格）" if parsed["mode"] == "table" else "纯文字整理"
    outdir = args.out or os.path.join(
        os.path.dirname(os.path.abspath(args.organized)),
        os.path.splitext(os.path.basename(args.organized))[0] + "_diff")
    os.makedirs(outdir, exist_ok=True)

    page_images = collect_page_images(pages, args.pages, outdir)
    missing_images = [label for label, _ in pages if label not in page_images] if args.pages else []
    if missing_images:
        print("缺图：" + ", ".join(missing_images), file=sys.stderr)
    with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
        f.write(build_html(args.original, args.organized, res, pages, ver,
                           mode_label, script_name, page_images if args.pages else None,
                           parsed.get("page_notes"), parsed.get("translations")))
    with open(os.path.join(outdir, "changelog.md"), "w", encoding="utf-8") as f:
        f.write(build_changelog(os.path.basename(args.original),
                                os.path.basename(args.organized),
                                res, pages, ver, mode_label,
                                include_header=True, script_name=script_name))
    with open(os.path.join(outdir, "changes.diff"), "w", encoding="utf-8") as f:
        f.write(build_diff(args.original, args.organized,
                           res["orig_text"], res["org_text"]))

    injected = False
    if not args.no_inject:
        injected = inject_changelog(args.organized, build_changelog(
            os.path.basename(args.original), os.path.basename(args.organized),
            res, pages, ver, mode_label, include_header=False,
            script_name=script_name))

    print("检查范围：仅内部对齐；不能证明内容完整、现场顺序正确或无编造。")
    if args.quiet:
        print("%s | %d 块 | %d 处差异"
              % ("PASS" if ver["ok"] else "FAIL", len(pages), len(res["hunks"])))
        return 0 if ver["ok"] and not missing_images else 1

    o_wps, _, _ = wps_count(res["orig_text"])
    n_wps, _, _ = wps_count(res["org_text"])
    o_ch, n_ch = char_count(res["orig_text"]), char_count(res["org_text"])
    print("=" * 68)
    print("形态：%s" % mode_label)
    if parsed["mode"] == "table":
        print("自动识别：实录列＝第 %s 列，页码列＝%s"
              % (parsed["col"], parsed["page_col"] or "未识别（改用序号）"))
        top = sorted(parsed["scores"], reverse=True)[:3]
        print("  列匹配度：" + "　".join("第%d列 %.2f" % (ci + 1, sc) for sc, ci in top))
    print("原始实录：%d 行" % len(orig_units))
    print("整理稿　：%d 块" % len(pages))
    print("-" * 68)
    print("字数（近似字数，仅用于观察）：%d → %d（保留 %.1f%%）"
          % (o_wps, n_wps, (n_wps / o_wps * 100) if o_wps else 0.0))
    print("字数（字符口径，去换行字符数）          ：%d → %d（保留 %.1f%%）"
          % (o_ch, n_ch, n_ch / o_ch * 100))
    note = retention_note(o_wps, n_wps, mode_label)
    if note:
        print("注意：%s" % note)
    print("-" * 68)
    print("内部对齐检查：%s" % ("PASS" if ver["ok"] else "FAIL"))
    print("  · 原文每个字符都被归属且仅归属一处：%s"
          % ("是" if not (ver["conflicts"] or ver["unassigned"]) else "否"))
    print("  · 块归属随原文位置单调递增（仅内部检查）：%s"
          % ("是" if not ver["nonmono"] else "否"))
    print("-" * 68)
    print("差异块 %d 处：" % len(res["hunks"]))
    for h in res["hunks"]:
        print("  [%s][%s] %r → %r" % (h["page"], h["kind"], h["orig"], h["org"]))
    print("-" * 68)
    if args.pages:
        print("PPT 列：%d/%d 页匹配到截图%s"
              % (len(page_images), len(pages),
                 "" if len(page_images) == len(pages) else "（缺图的页只有文字两列）"))
    for name in ("index.html", "changelog.md", "changes.diff"):
        print("已生成：%s" % os.path.join(outdir, name))
    if args.no_inject:
        inject_note = "已跳过回填（--no-inject）"
    elif injected:
        inject_note = "已回填修改清单到整理稿"
    else:
        inject_note = "整理稿里没有 CHANGELOG 标记，未回填"
    print(inject_note)
    return 0 if ver["ok"] and not missing_images else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(2)
