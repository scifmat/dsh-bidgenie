"""统一字数口径（P2-10）：审查 / 合并 / 导出 / 概览共用同一套计数。

口径定义（与 `bg_review_script.py` 原有口径一致）：

- 正文有效的“字数” = CJK 统一表意字符逐个计数 + ASCII 连续词（含数字、
  英文、``_`` ``.`` ``-``，例如 ``2026-11-21`` / ``PLC_01`` 记 1 个词）；
- 默认剥离 HTML 注释（``<!-- ... -->``，用于 BG:FIGURE / 题注标记）与
  Markdown 标题行（``#`` 开头），只统计正文内容；
- 标点、空白不计数。

其它统计口径（例如 summary 的 200–400 长度校验）是“字符长度”而不是
“字数”，继续使用 ``len(str)``，不要混用本模块。
"""

from __future__ import annotations

import re

#: CJK 统一表意字符（汉字）。
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
#: ASCII 连续词：字母/数字开头，后续允许字母数字与 ``._-``。
_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
#: HTML 注释（含跨行），BG 标记一般写在其中。
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
#: Markdown ATX 标题行。
_HEADING_LINE_RE = re.compile(r"^#{1,6}\s+.*$", re.MULTILINE)


def count_words(text: str, *, strip_comments: bool = True, strip_headings: bool = True) -> int:
    """返回 text 的“字数”（CJK 字符 + ASCII 词）。

    :param strip_comments: 是否剥离 HTML 注释（默认剥离）。
    :param strip_headings: 是否剥离 Markdown 标题行（默认剥离；统计文档全文字数时
        可传 False 把标题也算进去，但同一份产物必须固定口径）。
    """
    if not text:
        return 0
    clean = str(text)
    if strip_comments:
        clean = _HTML_COMMENT_RE.sub("", clean)
    if strip_headings:
        clean = _HEADING_LINE_RE.sub("", clean)
    return len(_CJK_RE.findall(clean)) + len(_ASCII_WORD_RE.findall(clean))


def count_body_words(text: str) -> int:
    """正文字数（剥离注释与标题行），审查/导出自检/概览统一用这个。"""
    return count_words(text, strip_comments=True, strip_headings=True)
