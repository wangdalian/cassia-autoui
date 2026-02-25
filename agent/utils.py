"""Agent 模块公共工具函数与主题定义"""

import emoji
from rich.theme import Theme

# ============================================================
# Cassia Markdown 主题 — 参考 iTerm2 暗色终端色系
# 设计原则: 低饱和、高可读、清晰层级、适配主流暗色终端
# ============================================================

CASSIA_THEME = Theme({
    "markdown.h1": "bold #82AAFF",
    "markdown.h2": "bold #82AAFF",
    "markdown.h3": "bold #89DDFF",
    "markdown.h4": "#89DDFF",
    "markdown.strong": "bold",
    "markdown.emph": "italic",
    "markdown.link": "#78DCE8 underline",
    "markdown.code": "#A9DC76",
    "markdown.item.bullet": "#7F8490",
    "markdown.item.number": "#7F8490",
    "markdown.hr": "#5C6370",
})


# ============================================================
# Emoji 间距修正
# ============================================================

_EMOJI_GAP = "  "


def fix_emoji_spacing(text: str) -> str:
    """确保 emoji 与紧邻文字之间有足够间距。

    部分终端对复合 emoji（keycap 1️⃣ 等）的宽度计算有偏差，
    1 个空格可能被视觉"吞掉"，统一补 2 个空格以兼容各终端。
    仅在 emoji **后方**补空格，不动前方，避免破坏 Markdown 语法标记。
    """
    matches = emoji.emoji_list(text)
    if not matches:
        return text
    result: list[str] = []
    last_end = 0
    for m in matches:
        start, end = m["match_start"], m["match_end"]
        result.append(text[last_end:start])
        result.append(text[start:end])
        existing = 0
        pos = end
        while pos < len(text) and text[pos] == " ":
            existing += 1
            pos += 1
        gap = len(_EMOJI_GAP)
        if pos < len(text) and existing < gap:
            result.append(" " * (gap - existing))
        last_end = end
    result.append(text[last_end:])
    return "".join(result)
