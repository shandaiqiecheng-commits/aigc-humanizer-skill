#!/usr/bin/env python3
"""LaTeX 改写稿结构校验：确认只动了散文，没动代码。

用法：
    python check_tex.py rewritten.tex [original.tex]

检查项：
  1. \\begin{X} / \\end{X} 配对与嵌套
  2. \\ref{} 都有对应 \\label{}
  3. \\cite{} 的键都在 thebibliography 里
  4. 花括号平衡
  5. $ 与 \\[ \\] 配对
  6. 数字清单（含两位以上小数的，人工核对用）
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path


def strip_comments(text: str) -> str:
    return re.sub(r"(?<!\\)%.*", "", text)


def check_env(text: str) -> list[str]:
    errs = []
    stack = []
    for m in re.finditer(r"\\(begin|end)\{([^}]+)\}", text):
        kind, name = m.group(1), m.group(2).strip()
        if kind == "begin":
            stack.append((name, m.start()))
        else:
            if not stack:
                errs.append(f"多余的 \\end{{{name}}}")
            elif stack[-1][0] != name:
                errs.append(
                    f"环境不匹配：\\begin{{{stack[-1][0]}}} 被 \\end{{{name}}} 关闭"
                )
                stack.pop()
            else:
                stack.pop()
    for name, _ in stack:
        errs.append(f"\\begin{{{name}}} 未关闭")
    return errs


def check_refs(text: str) -> list[str]:
    labels = set(re.findall(r"\\label\{([^}]+)\}", text))
    refs = set(re.findall(r"\\(?:ref|eqref|autoref)\{([^}]+)\}", text))
    return [f"\\ref{{{r}}} 没有对应的 \\label" for r in sorted(refs - labels)]


def check_cites(text: str) -> list[str]:
    bib = set(re.findall(r"\\bibitem\{([^}]+)\}", text))
    cites = set()
    for m in re.finditer(r"\\cite\{([^}]+)\}", text):
        cites.update(k.strip() for k in m.group(1).split(","))
    return [f"\\cite{{{c}}} 在参考文献里找不到" for c in sorted(cites - bib)]


def check_braces(text: str) -> list[str]:
    depth = 0
    line = 1
    for i, ch in enumerate(text):
        if ch == "\n":
            line += 1
        elif ch == "\\":
            continue
        elif ch == "{" and (i == 0 or text[i - 1] != "\\"):
            depth += 1
        elif ch == "}" and (i == 0 or text[i - 1] != "\\"):
            depth -= 1
            if depth < 0:
                return [f"第 {line} 行出现多余的 }}"]
    return [f"花括号不平衡，剩余 {depth} 个未闭合"] if depth else []


def check_math(text: str) -> list[str]:
    errs = []
    # 去掉 $$...$$ 与 $...$ 后统计剩余 $
    body = re.sub(r"\\\$", "", text)
    if body.count("$") % 2:
        errs.append(f"$ 数量为奇数（{body.count('$')}），行内公式可能没闭合")
    if body.count("\\[") != body.count("\\]"):
        errs.append("\\[ 与 \\] 数量不一致")
    return errs


STRUCTURAL_CMDS = (
    "label", "ref", "eqref", "autoref", "cite", "includegraphics",
    "lstinputlisting", "contentsline", "addcontentsline", "input", "include",
)


def content_numbers(text: str) -> list[tuple[int, str]]:
    """正文数字（两位以上小数），剔除结构命令参数里的。"""
    for cmd in STRUCTURAL_CMDS:
        text = re.sub(r"\\" + cmd + r"\*?(\[[^\]]*\])?\{[^}]*\}", " ", text)
    text = re.sub(r"\\begin\{thebibliography\}.*?\\end\{thebibliography\}", " ", text, flags=re.S)
    text = re.sub(r"\\begin\{lstlisting\}.*?\\end\{lstlisting\}", " ", text, flags=re.S)
    out = []
    for lineno, line in enumerate(text.split("\n"), 1):
        for m in re.finditer(r"(?<![\w.])-?\d+\.\d{2,}", line):
            out.append((lineno, m.group(0)))
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    path = Path(sys.argv[1])
    raw = path.read_text(encoding="utf-8")
    text = strip_comments(raw)

    all_errs: list[str] = []
    for name, fn in (
        ("环境配对", check_env),
        ("引用闭环", check_refs),
        ("文献引用", check_cites),
        ("花括号", check_braces),
        ("公式定界", check_math),
    ):
        errs = fn(text)
        if errs:
            all_errs += [f"[{name}] {e}" for e in errs]
        else:
            print(f"  OK  {name}")

    nums = content_numbers(text)
    print(f"\n  正文两位小数数字 {len(nums)} 个：")
    buf = []
    for lineno, n in nums:
        buf.append(f"{n}@{lineno}")
    for i in range(0, len(buf), 12):
        print("    " + "  ".join(buf[i:i + 12]))

    if len(sys.argv) > 2:
        orig = content_numbers(strip_comments(Path(sys.argv[2]).read_text(encoding="utf-8")))
        a, b = Counter(n for _, n in orig), Counter(n for _, n in nums)
        missing, added = a - b, b - a
        print("\n  == 与原稿对比 ==")
        if missing:
            print("    丢失的数字：", dict(missing))
            all_errs.append(f"[数字] 丢失 {sum(missing.values())} 个")
        if added:
            print("    新增的数字：", dict(added))
            all_errs.append(f"[数字] 新增 {sum(added.values())} 个")
        if not missing and not added:
            print("    数字完全一致")

    if all_errs:
        print("\n  == 问题 ==")
        for e in all_errs:
            print("    " + e)
        return 1

    print("\n  结论：结构通过")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
