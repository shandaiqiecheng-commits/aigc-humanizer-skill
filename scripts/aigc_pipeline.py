#!/usr/bin/env python3
"""AIGC Humanizer 流水线工具（移植自 BypassAIGC）。

原项目把「分段 → 逐段调 LLM 改写 → 滚动上下文 → 长度校验」做成了一个 Web 服务；
移植成 skill 后，改写由 agent 自己完成，本脚本只负责确定性的那部分：

    split   按段落切分，标出标题/短段落（这些不改）
    scan    统计 AI 套话密度与句长均匀度，用于改写前后对比
    check   把改写稿和原文逐段对齐，校验长度、修改率、数据是否丢失、套话残留

用法：
    python aigc_pipeline.py split paper.md --out segs.json
    python aigc_pipeline.py scan paper.md
    python aigc_pipeline.py check paper.md paper.humanized.md --mode academic

退出码：0 通过；1 有硬性违规（数据丢失 / 段落数不一致 / 长度超限）。
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from difflib import SequenceMatcher
from pathlib import Path

# ---------------------------------------------------------------- 基础计数

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
EN_RE = re.compile(r"[A-Za-z]")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")


def count_len(text: str) -> int:
    """复刻原项目 count_text_length：有汉字就数汉字，否则数英文字母。"""
    cjk = len(CJK_RE.findall(text))
    if cjk:
        return cjk
    return len(EN_RE.findall(text))


def norm(text: str) -> str:
    """比对用的归一化：去掉所有空白。"""
    return re.sub(r"\s+", "", text)


# ---------------------------------------------------------------- 分段

FM_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
MD_HEAD_RE = re.compile(r"^#{1,6}\s")
NUM_HEAD_RE = re.compile(
    r"^(第[一二三四五六七八九十百]+[章节篇]|[0-9]+(\.[0-9]+)*\s*[^\d]|"
    r"[一二三四五六七八九十]+、|（[一二三四五六七八九十]+）)"
)
HEADING_RE = MD_HEAD_RE  # 兼容旧引用
SENT_SPLIT_RE = re.compile(r"(?<=[。！？；!?;])\s*")


def strip_front(text: str) -> str:
    """去掉 YAML frontmatter，避免 `---` 被当成破折号、元数据被算进句长。"""
    m = FM_RE.match(text)
    return text[m.end():] if m else text


def split_blocks(text: str) -> list[str]:
    """按空行切块；没有空行时按单换行切。"""
    if re.search(r"\n[ \t]*\n", text):
        raw = re.split(r"\n[ \t]*\n", text)
    else:
        raw = text.split("\n")
    return [b.strip() for b in raw if b.strip()]


def split_long(block: str, max_chars: int) -> list[str]:
    """单段超长时按句子边界再切，避免一次改写跨度过大。"""
    if count_len(block) <= max_chars:
        return [block]
    sents = [s for s in SENT_SPLIT_RE.split(block) if s]
    out: list[str] = []
    cur = ""
    for s in sents:
        if not cur:
            cur = s
        elif count_len(cur + s) <= max_chars:
            cur += s
        else:
            out.append(cur)
            cur = s
    if cur:
        out.append(cur)
    return out


def is_heading(block: str) -> bool:
    """判断是不是标题。

    Markdown 标题一律算。编号式标题（第 3 章 / 2.1 / 一、）只在短的时候才算——
    `1. 理论意义：深化对…的理解` 是列表项，不是标题，该改。
    """
    if MD_HEAD_RE.match(block):
        return True
    if "\n" in block.strip():
        return False
    return bool(NUM_HEAD_RE.match(block)) and count_len(block) < 30


def is_keyword_list(block: str) -> bool:
    """关键词行：单行、没有句末标点、被 ; / 、 / ， / 全角空格切成 3 段以上短词。"""
    if "\n" in block.strip():
        return False
    if re.search(r"[。！？.!?]", block):
        return False
    parts = [p.strip() for p in re.split(r"[;；、,，]", block) if p.strip()]
    if len(parts) < 3:
        parts = [p.strip() for p in block.split("\u3000") if p.strip()]
    if len(parts) < 3:
        return False
    return all(count_len(p) <= 30 for p in parts)


REF_RE = re.compile(r"^\s*\[\d+\]")


def is_reference(block: str) -> bool:
    """参考文献条目。这种绝对不能改写：作者、年份、期刊名、卷期页码都不能动。"""
    return bool(REF_RE.match(block))


def classify(block: str, skip_threshold: int) -> tuple[bool, bool, str]:
    """返回 (is_title, skip, reason)。"""
    if is_heading(block):
        return True, True, "heading"
    if is_reference(block):
        return True, True, "reference"
    if is_keyword_list(block):
        return True, True, "keyword_list"
    if count_len(block) < skip_threshold:
        return True, True, "too_short"
    # 结尾没有标点且很短，多半是小标题
    if len(block) < 40 and not re.search(r"[。！？；.!?;：:]$", block):
        return True, True, "title_like"
    return False, False, ""


# ---------------------------------------------------------------- 文档读取

def is_docx(path) -> bool:
    return Path(path).suffix.lower() == ".docx"


def read_docx_paras(path):
    """返回 [(段号, 文本, 样式名)]。表格不在其中——表里的数据本来也不该改。"""
    try:
        from docx import Document
    except ImportError:
        raise SystemExit(
            "处理 .docx 需要 python-docx：pip install python-docx\n"
            "或者先把论文另存为 .txt / .md。"
        )
    doc = Document(str(path))
    out = []
    for i, p in enumerate(doc.paragraphs):
        style = p.style.name if p.style is not None else ""
        out.append((i, p.text, style))
    return doc, out


def docx_is_heading(style: str, text: str) -> bool:
    """Word 里的标题样式比正则可靠得多，优先用样式判断。"""
    s = (style or "").lower()
    if "heading" in s or "标题" in s or s == "title":
        return True
    return is_heading(text)


def get_blocks(path) -> list[str]:
    """统一取块序列：txt/md 按空行切，docx 取非空段落。"""
    if is_docx(path):
        _, paras = read_docx_paras(path)
        return [t.strip() for _, t, _ in paras if t.strip()]
    return split_blocks(Path(path).read_text(encoding="utf-8-sig"))


def cmd_split(args: argparse.Namespace) -> int:
    path = Path(args.file)
    segments: list[dict] = []
    idx = 0

    if is_docx(path):
        _, paras = read_docx_paras(path)
        for para_index, raw, style in paras:
            text = raw.strip()
            if not text:
                continue
            if docx_is_heading(style, text):
                segments.append({
                    "index": idx, "para_index": para_index, "text": text,
                    "chars": count_len(text), "is_title": True,
                    "skip": True, "reason": "heading",
                })
                idx += 1
                continue
            for piece in split_long(text, args.max_chars):
                is_title, skip, reason = classify(piece, args.skip_threshold)
                segments.append({
                    "index": idx, "para_index": para_index, "text": piece,
                    "chars": count_len(piece), "is_title": is_title,
                    "skip": skip, "reason": reason,
                })
                idx += 1
    else:
        text = path.read_text(encoding="utf-8-sig")
        front = ""
        m = FM_RE.match(text)
        if m:
            front = m.group(0)
            text = text[m.end():]

        if front:
            segments.append({
                "index": 0, "text": front.rstrip("\n"), "chars": count_len(front),
                "is_title": True, "skip": True, "reason": "frontmatter",
            })
            idx = 1

        for block in split_blocks(text):
            for piece in split_long(block, args.max_chars):
                is_title, skip, reason = classify(piece, args.skip_threshold)
                segments.append({
                    "index": idx, "text": piece, "chars": count_len(piece),
                    "is_title": is_title, "skip": skip, "reason": reason,
                })
                idx += 1

    todo = [s for s in segments if not s["skip"]]
    payload = {
        "source": str(path),
        "kind": "docx" if is_docx(path) else "text",
        "total": len(segments),
        "to_rewrite": len(todo),
        "skipped": len(segments) - len(todo),
        "total_chars": sum(s["chars"] for s in todo),
        "segments": segments,
    }

    if args.out:
        Path(args.out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"已写出 {args.out}")
        print(
            f"共 {payload['total']} 段，需改写 {payload['to_rewrite']} 段"
            f"（{payload['total_chars']} 字），跳过 {payload['skipped']} 段"
        )
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------- AI 套话库

# (类别, 严重度, 正则)。high = 命中即改；mid = 看上下文，学术写作里偶尔合法。
TELLS: list[tuple[str, str, str]] = [
    # 拔高
    ("拔高", "high", r"具有(重要|深远|重大|划时代)意义"),
    ("拔高", "high", r"开启.{0,4}新(篇章|时代)"),
    ("拔高", "high", r"推动.{0,6}(变革|革新)"),
    ("拔高", "high", r"产生.{0,4}深远(的)?影响"),
    ("拔高", "mid", r"关键(时刻|节点|一环)"),
    ("拔高", "mid", r"奠定(了)?.{0,4}基础"),
    ("拔高", "high", r"(具有|提供)(重要|巨大|广阔)的?(应用|参考|指导)?(价值|前景|意义)"),
    # 热度空话
    ("热度", "high", r"广受关注|引发(广泛)?(热议|关注)|业内(普遍)?好评|备受(瞩目|关注)"),
    # 假分析（有动词没内容）
    ("假分析", "high", r"通过.{1,20}(实现|达到|完成)"),
    ("假分析", "high", r"依托.{1,20}(推动|实现|打造)"),
    ("假分析", "high", r"围绕.{1,20}(展开|进行|构建)"),
    ("假分析", "mid", r"以.{1,15}为(核心|抓手|导向|依托)"),
    # 宣传词
    ("宣传", "high", r"(行业|全球|国内)?领先(的|水平|地位)"),
    ("宣传", "high", r"卓越|极致|革命性|颠覆性|全方位|一体化解决"),
    ("宣传", "high", r"赋能|护航|助力.{0,6}(腾飞|升级)"),
    # 假权威
    ("假权威", "high", r"有人认为|业内(人士)?(普遍)?认为|专家指出|相关人士(表示|指出)|研究表明(?![，,]\s*\[)"),
    ("假权威", "mid", r"被(广泛)?认为|公认(为)?"),
    # 套话收尾
    ("套话", "high", r"机遇与挑战并存|未来可期|前景(值得|十分)期待|值得(我们)?期待"),
    ("套话", "high", r"(具有|有着)(广阔|光明)的?(发展|应用)前景"),
    # 黑话
    ("黑话", "high", r"赋能|生态(化|闭环)?|闭环|抓手|底层逻辑|价值沉淀|长期主义"),
    ("黑话", "high", r"深度融合|全链路|颗粒度|场景化|范式(转移|跃迁)?|跃迁"),
    # 系词伪装
    ("系词", "high", r"(可|可以)(被)?视为|呈现出|(充分)?体现了|彰显了|标志着|折射出"),
    # 否定排比 / 空并列
    ("排比", "high", r"不是.{1,20}而是|不只是.{1,20}更是|不仅是.{1,20}还是"),
    ("排比", "mid", r"更(快|稳|智能|高效|安全|便捷)"),
    ("排比", "mid", r"从.{1,12}到.{1,12}"),
    # 无主语 / 悬浮
    ("无主语", "mid", r"无需配置|数据告诉我们|市场会(奖励|惩罚)|众所周知"),
    # 填充词
    ("填充", "high", r"值得注意的是|不可否认|毋庸置疑|总体而言|综上所述|从这个角[度看]来看"),
    ("填充", "high", r"总而言之|换句话说|简而言之|一言以蔽之"),
    # 伪洞察
    ("伪洞察", "high", r"真正的问题(是|在于)|归根结底|本质上(是|讲)|核心在于"),
    # 开场导览
    ("导览", "high", r"让我们(一起)?(深入)?(探讨|看看|了解)|本文将(带你|从.{1,10})?(了解|探讨|分析)"),
    ("导览", "high", r"接下来(我们)?将(介绍|讨论|分析)|首先让?我们"),
    # 伪口语钩子
    ("伪口语", "high", r"说实话[？?]|老实讲|重点来了|你敢信|不得不说"),
    # 英文 AI 词
    ("英文", "high", r"\b(delve|leverage|robust|nuanced|pivotal|tapestry|underscore|showcase|realm|multifaceted)\b"),
    ("英文", "high", r"\b(serves? as|stands? as)\b"),
    ("英文", "high", r"It'?s not (just )?.{1,30}, it'?s"),
    ("英文", "mid", r"\b(landscape|paradigm|seamless(ly)?|holistic|cutting-edge)\b"),
    # 开场模板（中文 AI 最典型的起手式）
    ("开场", "high", r"随着.{0,14}的(快速|迅猛|不断|持续)?发展"),
    ("开场", "high", r"在.{0,10}(飞速|快速)发展的(今天|背景下|时代)"),
    ("开场", "high", r"近年(来|间)[，,]?.{0,10}(日益|逐渐|愈发)"),
    # 成绩拔高
    ("成绩", "high", r"取得了?(显著|突破性|令人瞩目)的?(突破|进展|成效|成果)"),
    ("成绩", "high", r"(大幅|显著|明显)(提升|提高|改善)了.{0,8}(性能|效率|效果|体验)"),
    # 结构化套话
    ("结构", "high", r"主要体现在以下几个(方面|层面)"),
    ("结构", "high", r"(首先|其次|再次)[，,].{0,30}(其次|再次|最后)[，,]"),
    ("结构", "high", r"从.{0,8}层面(来看|讲)"),
    # 万能落点
    ("落点", "high", r"可(以)?(广泛)?应用于"),
    ("落点", "high", r"为.{0,14}提供了?(新|有力|有效)?的?(思路|方案|支撑|参考|借鉴)"),
    ("落点", "mid", r"在.{0,12}方面(具有|有着|展现出).{0,12}优势"),
    # 标点
    ("标点", "high", r"——|(?<!-)--(?!-)"),
    ("标点", "mid", r"[，,]\s*(即|也就是说)"),
]

COMPILED = [(c, s, re.compile(p, re.IGNORECASE)) for c, s, p in TELLS]

SENT_RE = re.compile(r"[^。！？；!?;\n]+[。！？；!?;]?")


def find_tells(text: str) -> list[dict]:
    hits = []
    for cat, sev, rx in COMPILED:
        for m in rx.finditer(text):
            hits.append({"cat": cat, "sev": sev, "hit": m.group(0)})
    return hits


def _cv(values: list[int]) -> tuple[float, int, float]:
    if len(values) < 5:
        return 0.0, len(values), 0.0
    mean = statistics.fmean(values)
    if mean == 0:
        return 0.0, len(values), 0.0
    return statistics.pstdev(values) / mean, len(values), mean


def cv_stats(text: str) -> dict:
    """句长变异系数，中英文分开算（中文按汉字数，英文按词数）。

    混排时不能合成一个指标：同一个句子，汉字数和英文词数不是一个尺度。
    AI 文本通常 < 0.35，人类写作 0.45~0.7。这是参考值不是硬指标——
    技术文档句长本来就整齐，硬凑节奏反而假。
    """
    cjk_lens: list[int] = []
    en_lens: list[int] = []
    for block in split_blocks(strip_front(text)):
        if is_heading(block):
            continue
        for s in SENT_RE.findall(block):
            if CJK_RE.search(s):
                n = len(CJK_RE.findall(s))
                if n >= 8:
                    cjk_lens.append(n)
            else:
                n = len(WORD_RE.findall(s))
                if n >= 6:
                    en_lens.append(n)
    out = {}
    for key, vals in (("cjk", cjk_lens), ("latin", en_lens)):
        cv, n, mean = _cv(vals)
        out[key] = {"cv": round(cv, 2), "n": n, "mean": round(mean, 1)}
    return out


def scan_stats(text: str) -> dict:
    hits = find_tells(text)
    cv = cv_stats(text)
    by_cat: dict[str, int] = {}
    for h in hits:
        by_cat[h["cat"]] = by_cat.get(h["cat"], 0) + 1
    return {
        "chars": count_len(text),
        "sentence_cv": cv,
        "dash": len(re.findall(r"——", text)),
        "tells_total": len(hits),
        "tells_high": sum(1 for h in hits if h["sev"] == "high"),
        "tells_by_cat": dict(sorted(by_cat.items(), key=lambda kv: -kv[1])),
        "tells_detail": [h["hit"] for h in hits],
    }


def _cv_line(label: str, cv: dict) -> str | None:
    parts = []
    for key, name in (("cjk", "中文"), ("latin", "英文")):
        d = cv[key]
        if d["n"] >= 5:
            parts.append(f"{name} {d['cv']}（{d['n']} 句，均长 {d['mean']}）")
    if not parts:
        return None
    return f"{label} " + " / ".join(parts)


def cmd_scan(args: argparse.Namespace) -> int:
    text = strip_front(Path(args.file).read_text(encoding="utf-8-sig"))
    st = scan_stats(text)
    if args.json:
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return 0
    print(f"字数        {st['chars']}")
    cvl = _cv_line("句长CV     ", st["sentence_cv"])
    print(cvl or "句长CV      样本不足")
    print(f"破折号      {st['dash']} 处")
    print(f"AI 套话     {st['tells_total']} 处（高危 {st['tells_high']}）")
    if st["tells_by_cat"]:
        print("  分类      " + "  ".join(f"{k}:{v}" for k, v in st["tells_by_cat"].items()))
    if st["tells_detail"]:
        uniq = list(dict.fromkeys(st["tells_detail"]))[:25]
        print("  命中      " + " / ".join(uniq))
    return 0


# ---------------------------------------------------------------- 保护片段

NUM_UNIT = (
    r"(?:mg|kg|g|mL|ml|L|mm|cm|km|m|μm|um|nm|h|min|s|ms|℃|°C|mol|mmol|"
    r"Hz|kHz|MHz|GHz|V|mV|A|mA|W|kW|Pa|kPa|MPa|dB|GB|MB|KB|TB|rpm|%|％)"
)
SPAN_PATTERNS = [
    ("引用", re.compile(r"\[\s*\d+(?:\s*[,\-–~]\s*\d+)*\s*\]")),
    ("百分比", re.compile(r"\d+(?:\.\d+)?\s*[%％]")),
    ("数值", re.compile(r"\d+(?:\.\d+)?\s*" + NUM_UNIT)),
    ("图表", re.compile(r"(?:图|表|式|Fig\.?|Table|Eq\.?)\s*\(?\d+(?:[.\-]\d+)*\)?")),
    ("公式", re.compile(r"\$[^$\n]{1,80}\$")),
    ("代码", re.compile(r"`[^`\n]{1,60}`")),
    ("长数字", re.compile(r"\b\d{4,}\b")),
]


def protected_spans(text: str) -> list[tuple[str, str]]:
    """提取必须原样保留的片段。同一个片段被多条规则命中时只记一次。"""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for kind, rx in SPAN_PATTERNS:
        for m in rx.finditer(text):
            span = m.group(0)
            key = norm(span)
            if key in seen:
                continue
            seen.add(key)
            out.append((kind, span))
    return out


# ---------------------------------------------------------------- 校验

def sparse_change(row: dict, min_change: float) -> bool:
    """改动过少的判定。数据密集的段落本来就改不动——术语和数字必须原样保留，
    硬去凑修改率只能靠加废话，所以这里不算问题。"""
    floor = 40 if row.get("unit") == "字" else 20
    return (
        row["change"] < min_change
        and row["o"] >= floor
        and row.get("spans", 0) < 3
    )


def cmd_check(args: argparse.Namespace) -> int:
    b_src = get_blocks(args.original)
    b_dst = get_blocks(args.rewritten)
    src = "\n\n".join(b_src)
    dst = "\n\n".join(b_dst)

    if len(b_src) != len(b_dst):
        print(f"[FAIL] 段落数不一致：原文 {len(b_src)} 段，改写稿 {len(b_dst)} 段")
        print("       逐段改写必须保持段落划分不变，否则读者会看出结构被重排。")
        if args.json:
            print(json.dumps({"ok": False, "reason": "block_count_mismatch",
                              "src": len(b_src), "dst": len(b_dst)}, ensure_ascii=False))
        return 1

    rows = []
    fails: list[str] = []
    warns: list[str] = []
    tot_o = tot_r = 0
    wen_o = wen_r = 0

    for i, (o, r) in enumerate(zip(b_src, b_dst), start=1):
        is_cjk = bool(CJK_RE.search(o))
        if is_cjk:
            co, cr = count_len(o), count_len(r)
            unit = "字"
            tot_o += co
            tot_r += cr
        else:
            co = len(WORD_RE.findall(o))
            cr = len(WORD_RE.findall(r))
            unit = "词"
            wen_o += co
            wen_r += cr

        is_front = o.startswith("---") and o.count("---") >= 2
        if is_heading(o) or is_reference(o) or is_front or count_len(o) < args.skip_threshold:
            if norm(o) != norm(r):
                fails.append(f"第 {i} 段是标题/短段落/参考文献，不应改动：{o[:30]!r} → {r[:30]!r}")
            rows.append({"i": i, "kind": "heading", "o": co, "r": cr, "unit": unit})
            continue

        delta = cr - co
        pct = (delta / co * 100) if co else 0.0
        ratio = 1 - SequenceMatcher(None, norm(o), norm(r)).ratio()

        # 中文按绝对字数（学术 ±30 字），英文按词数百分比（±10%），情感模式统一按 ±10%
        if args.mode == "emotion":
            over = abs(pct) > args.max_pct
            limit_desc = f"±{args.max_pct:g}%"
        elif is_cjk:
            over = abs(delta) > args.max_delta
            limit_desc = f"±{args.max_delta} 字"
        else:
            over = abs(pct) > 10.0
            limit_desc = "±10% 词数"

        missing = []
        rn = norm(r)
        spans = protected_spans(o)
        for kind, span in spans:
            if norm(span) not in rn:
                missing.append(f"{kind}「{span}」")

        residual = find_tells(r)
        high = [h["hit"] for h in residual if h["sev"] == "high"]

        row = {
            "i": i,
            "o": co,
            "r": cr,
            "unit": unit,
            "delta": delta,
            "pct": round(pct, 1),
            "change": round(ratio, 3),
            "spans": len(spans),
            "missing": missing,
            "residual_high": high,
            "residual_total": len(residual),
        }
        rows.append(row)

        if missing:
            fails.append(f"第 {i} 段丢失数据/引用：{'、'.join(missing[:4])}")
        if over:
            warns.append(f"第 {i} 段长度 {co}{unit}→{cr}{unit}（{delta:+d}，超出 {limit_desc}）")
        if high:
            warns.append(f"第 {i} 段残留套话：{'、'.join(dict.fromkeys(high))[:60]}")
        if sparse_change(row, args.min_change):
            warns.append(f"第 {i} 段几乎没改（修改率 {ratio:.0%}）")

    total_change = 1 - SequenceMatcher(None, norm(strip_front(src)), norm(strip_front(dst))).ratio()
    st_src, st_dst = scan_stats(strip_front(src)), scan_stats(strip_front(dst))

    # 句长越改越均匀是典型翻车：把长句都砍短了，读起来更像说明书
    for key, name in (("cjk", "中文"), ("latin", "英文")):
        b, a = st_src["sentence_cv"][key], st_dst["sentence_cv"][key]
        if b["n"] >= 5 and a["n"] >= 5 and a["cv"] < 0.40 and a["cv"] < b["cv"] * 0.9:
            warns.append(
                f"{name}句长 CV 从 {b['cv']} 降到 {a['cv']}：句子被改得更均匀了，"
                f"回去把部分句子合并或拆开，拉开长短差"
            )

    if not args.json:
        print("== 总览 ==")
        print(f"段落数      {len(b_src)} → {len(b_dst)}   OK")
        if tot_o:
            print(f"中文字数    {tot_o} → {tot_r}   ({tot_r - tot_o:+d}, {(tot_r - tot_o) / tot_o * 100:+.1f}%)")
        if wen_o:
            print(f"英文词数    {wen_o} → {wen_r}   ({wen_r - wen_o:+d}, {(wen_r - wen_o) / wen_o * 100:+.1f}%)")
        print(f"整体修改率  {total_change:.1%}")
        print(f"AI 套话     {st_src['tells_total']} → {st_dst['tells_total']} 处"
              f"（高危 {st_src['tells_high']} → {st_dst['tells_high']}）")
        cv_b = _cv_line("改前 ", st_src["sentence_cv"]) or "改前  样本不足"
        cv_a = _cv_line("改后 ", st_dst["sentence_cv"]) or "改后  样本不足"
        print("句长 CV     " + cv_b)
        print("            " + cv_a + "   (0.45+ 更接近人写)")
        print(f"破折号      {st_src['dash']} → {st_dst['dash']} 处")
        print()
        print("== 逐段 ==")
        print(f"{'#':>3} {'原':>5} {'改':>5} {'±':>5} {'改率':>6}  备注")
        for row in rows:
            if row.get("kind") == "heading":
                print(f"{row['i']:>3} {row['o']:>5} {row['r']:>5} {'-':>5} {'-':>6}  标题/文献(未改)")
                continue
            note = row["unit"] if row["unit"] == "词" else ""
            if row["missing"]:
                note = "数据丢失!"
            elif row["residual_high"]:
                note = "残留:" + "、".join(dict.fromkeys(row["residual_high"]))[:28]
            elif sparse_change(row, args.min_change):
                note = "改动过少"
            print(f"{row['i']:>3} {row['o']:>5} {row['r']:>5} {row['delta']:>+5d} "
                  f"{row['change']:>5.0%}  {note}")

    if fails:
        print("\n== 必须修复 ==")
        for f in fails:
            print("  [FAIL] " + f)
    if warns:
        print("\n== 建议修复 ==")
        for w in warns[:20]:
            print("  [WARN] " + w)
        if len(warns) > 20:
            print(f"  ... 另有 {len(warns) - 20} 条")

    ok = not fails
    print("\n" + ("结论：通过" if ok else "结论：未通过，先修 FAIL 项再交付"))

    if args.json:
        print(json.dumps(
            {"ok": ok, "change": round(total_change, 3), "rows": rows,
             "fails": fails, "warns": warns,
             "scan_before": st_src["tells_total"], "scan_after": st_dst["tells_total"]},
            ensure_ascii=False, indent=2))
    return 0 if ok else 1


# ---------------------------------------------------------------- 写回 docx

def set_para_text(para, text: str) -> None:
    """把整段文本塞进第一个 run，保留它的字体，删掉其余 run。

    这样段落样式、缩进、行距、字号都能留住，但**段内混排会塌**：
    句中的加粗、斜体、上标统一变成第一个 run 的格式。
    """
    if not para.runs:
        para.add_run(text)
        return
    para.runs[0].text = text
    for r in para.runs[1:]:
        r._element.getparent().remove(r._element)


def cmd_writeback(args: argparse.Namespace) -> int:
    try:
        from docx import Document
    except ImportError:
        raise SystemExit("处理 .docx 需要 python-docx：pip install python-docx")

    doc = Document(str(args.docx))
    data = json.loads(Path(args.segs).read_text(encoding="utf-8"))
    segs = data.get("segments", [])

    # 按段号分组，长段被切开的多块按顺序拼回去
    grouped: dict[int, list[str]] = {}
    order: list[int] = []
    for s in segs:
        pi = s.get("para_index")
        if pi is None:
            continue
        if pi not in grouped:
            grouped[pi] = []
            order.append(pi)
        grouped[pi].append(s["text"])

    if not order:
        print("这个 JSON 里没有 para_index，可能是 txt 分段结果，不需要写回。")
        return 1

    changed = 0
    for pi in order:
        new_text = "".join(grouped[pi])
        para = doc.paragraphs[pi]
        if para.text.strip() == new_text.strip():
            continue
        set_para_text(para, new_text)
        changed += 1

    doc.save(str(args.out))
    print(f"已写出 {args.out}")
    print(f"改动 {changed} 段，未改动 {len(order) - changed} 段")
    if changed:
        print("提示：段内加粗/斜体等混排格式已统一为段首格式，交付前请抽查。")
    return 0


# ---------------------------------------------------------------- CLI

def main() -> int:
    p = argparse.ArgumentParser(description="AIGC Humanizer 流水线工具")
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("split", help="切分段落")
    ps.add_argument("file")
    ps.add_argument("--max-chars", type=int, default=500)
    ps.add_argument("--skip-threshold", type=int, default=15)
    ps.add_argument("--out")
    ps.set_defaults(func=cmd_split)

    pc = sub.add_parser("scan", help="扫描 AI 套话密度")
    pc.add_argument("file")
    pc.add_argument("--json", action="store_true")
    pc.set_defaults(func=cmd_scan)

    pk = sub.add_parser("check", help="校验改写稿")
    pk.add_argument("original")
    pk.add_argument("rewritten")
    pk.add_argument("--mode", choices=["academic", "emotion"], default="academic")
    pk.add_argument("--max-delta", type=int, default=30, help="学术模式字数容差")
    pk.add_argument("--max-pct", type=float, default=10.0, help="情感模式字数容差(百分比)")
    pk.add_argument("--min-change", type=float, default=0.25, help="单段最低修改率")
    pk.add_argument("--skip-threshold", type=int, default=15)
    pk.add_argument("--json", action="store_true")
    pk.set_defaults(func=cmd_check)

    pw = sub.add_parser("writeback", help="把改写结果写回 docx（保留格式）")
    pw.add_argument("docx", help="原始 .docx")
    pw.add_argument("segs", help="改写后的 segs.json")
    pw.add_argument("-o", "--out", required=True, help="输出 .docx")
    pw.set_defaults(func=cmd_writeback)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
