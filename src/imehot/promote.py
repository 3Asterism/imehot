"""词条质检：决定一个原生学到的词是否值得进入「热词库」。

原生词库是「用户选中过什么」的原始记录，里面混着大量噪音：
emoji、单个字母、误触产生的怪字。直接全量导入只会让热词库变成垃圾场。

这里的规则是可解释的、可逐条关闭的。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

# 常见 emoji / 符号区块
_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),  # 各类 emoji
    (0x2600, 0x27BF),  # 杂项符号 & Dingbats
    (0x2B00, 0x2BFF),  # 杂项符号与箭头
    (0xFE00, 0xFE0F),  # 变体选择符
    (0x1F1E6, 0x1F1FF),  # 区域指示符（国旗）
)
_INVISIBLE = {0x200D, 0x200B, 0x200C, 0x20E3, 0xFEFF}


def is_emoji(ch: str) -> bool:
    o = ord(ch)
    return o in _INVISIBLE or any(lo <= o <= hi for lo, hi in _EMOJI_RANGES)


def is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (
        0x4E00 <= o <= 0x9FFF  # CJK 统一表意文字
        or 0x3400 <= o <= 0x4DBF  # 扩展 A
        or 0xF900 <= o <= 0xFAFF  # 兼容表意文字
        or 0x3000 <= o <= 0x303F  # CJK 标点
    )


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str


#: 以这些虚词收尾的多半是整句/半句话，不是词（来自 ime-lexicon 的 profile 规则）。
BAD_SUFFIXES = ("的了", "的吧", "的是", "了呢", "了吗", "的话")

#: 超过这个长度基本是整句上屏，不是"词"。
DEFAULT_MAX_LEN = 16


def judge(
    surface: str,
    pinyin: str,
    *,
    min_cjk: int = 2,
    allow_english: bool = False,
    min_ascii_len: int = 3,
    max_len: int = DEFAULT_MAX_LEN,
) -> Verdict:
    """判定一个词面是否适合进入热词库。"""
    s = surface.strip()
    if not s:
        return Verdict(False, "空词面")

    if len(s) > max_len:
        return Verdict(False, f"长度 {len(s)} > {max_len}，是整句不是词")

    if any(is_emoji(c) for c in s):
        return Verdict(False, "含 emoji/符号")

    if any(unicodedata.category(c) in ("Cc", "Cf", "Cs") for c in s):
        return Verdict(False, "含控制字符")

    for suffix in BAD_SUFFIXES:
        if s.endswith(suffix):
            return Verdict(False, f"以虚词「{suffix}」收尾，是半句话")

    cjk = sum(1 for c in s if is_cjk(c))
    if cjk:
        if cjk < min_cjk:
            return Verdict(False, f"中文字符仅 {cjk} 个（要求 ≥{min_cjk}）")
        return Verdict(True, f"{cjk} 个中文字")

    # 纯非中文
    if s.isdigit():
        return Verdict(False, "纯数字")
    if not allow_english:
        return Verdict(False, "非中文（未启用 --include-english）")
    if not s.replace("'", "").replace("-", "").isalpha():
        return Verdict(False, "含非字母字符")
    if len(s) < min_ascii_len:
        return Verdict(False, f"英文长度 {len(s)} < {min_ascii_len}")
    # 注意：原生词库里英文词的 PinyinReading 就是词面本身（用户直接敲的英文），
    # 所以不能用"词面==拼音"来判噪音，否则会把所有英文词误杀。
    return Verdict(True, "英文词，原样上屏")
