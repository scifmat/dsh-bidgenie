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


#: R-13（2026-11-26 定向续跑）：题注开头「数字+量词」在 `▼表`/`▲图` 之后会被读成编号，
#: 例如 `▼表3 条路段设施供货清单与进场计划表` 读作「表 3 条路段…」。这里给
#: 大纲校验/写作任务书/审查共用同一口径（「1包」「2026年」「1.5mm」等业务数字不算）。
_CAPTION_NUMBER_PREFIX_RE = re.compile(r"^\s*(\d{1,3})\s*(条|个|项|段|处|类|种|套|台|批|份|户|家)")
_CAPTION_MEASURE_REWRITE = {
    "条": "各条",
    "个": "各个",
    "项": "各项",
    "段": "各段",
    "处": "各处",
    "类": "各类",
    "种": "各种",
    "套": "各套",
    "台": "各台",
    "批": "各批",
    "份": "各份",
    "户": "各户",
    "家": "各家",
}


def caption_number_prefix(caption: str) -> str:
    """返回题注开头的「数字+量词」（如「3 条」）；没有则空串。"""
    match = _CAPTION_NUMBER_PREFIX_RE.match(str(caption or ""))
    return match.group(0).strip() if match else ""


def rewrite_caption_number_prefix(caption: str) -> str:
    """把「3 条路段…」改写为「各条路段…」；无前缀时原样返回（R-13 建议文案用）。"""
    text = str(caption or "")
    match = _CAPTION_NUMBER_PREFIX_RE.match(text)
    if not match:
        return text
    replacement = _CAPTION_MEASURE_REWRITE.get(match.group(2), "各")
    return replacement + text[match.end() :]
