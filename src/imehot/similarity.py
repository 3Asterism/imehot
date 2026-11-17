"""拼音相似度：用来识别「先选错、再改对」的词对。

## 为什么需要它

用户把「说思路」打成「所思路」「所示录」，backspace 删掉重打。
苹果自己的专利（US9703394）说得很清楚：用户拒绝一个候选时，键盘模块会
**降低**它的概率，但**不会删除**——只是递减似然值。

所以错字会留在原生词库里，而那个用来降权的权重我们读不到（见 ADR 0001）。
必须在 imehot 这一层把它认出来。

## 怎么认

参考 CN102122298A「中文相似性匹配方法」：相似度由编辑距离、键盘指法、
声母/韵母相似、模糊音、字形共同决定。我们只取**拼音输入法**需要的那部分：

1. **模糊音归一化**（`zh/z`、`ch/c`、`sh/s`、`n/l`、`in/ing`、`an/ang`、`en/eng`）
2. 归一化后再算编辑距离

实测效果（三个词归一化后完全相同）::

    所思路  suo si lu
    说思路  shuo si lu   → sh→s    归一化后 suo si lu
    所示录  suo shi lu   → sh→s    归一化后 suo si lu

注意：真实的同音词（`会`/`汇`、`在`/`再`）也会被算成高相似。
所以本模块**只产出「可疑」这个信号，从不做删除决定**。
"""

from __future__ import annotations

import re

from .native import parse_syllable_lengths

#: 默认相似度阈值。只用于「同一批次里 TokenID 相邻」的词对，误伤面很小。
DEFAULT_THRESHOLD = 0.86

_INITIAL_FUZZY = re.compile(r"^(zh|ch|sh)")
_FINAL_FUZZY = (
    (re.compile(r"ing$"), "in"),
    (re.compile(r"ang$"), "an"),
    (re.compile(r"eng$"), "en"),
)


def normalize_syllable(syllable: str) -> str:
    """把一个音节按模糊音规则归一化。"""
    s = syllable.lower()
    if not s:
        return s
    s = _INITIAL_FUZZY.sub(lambda m: m.group(1)[0], s)
    if s.startswith("n") and len(s) > 1:
        s = "l" + s[1:]
    for pattern, repl in _FINAL_FUZZY:
        s = pattern.sub(repl, s)
    return s


def _split(pinyin: str, syllables: str) -> list[str]:
    steps = parse_syllable_lengths(pinyin, syllables)
    if steps is None:
        return []
    parts: list[str] = []
    cursor = 0
    for step in steps:
        parts.append(pinyin[cursor : cursor + step])
        cursor += step
    return parts


def normalize(pinyin: str, syllables: str = "") -> str:
    """归一化整串拼音。

    有音节信息时逐音节处理（`n→l` 和韵母合并才可靠）；
    没有时只做全局安全的 `zh/ch/sh → z/c/s`。
    """
    p = (pinyin or "").lower()
    if not p:
        return ""
    parts = _split(p, syllables)
    if parts:
        return "".join(normalize_syllable(x) for x in parts)
    return _INITIAL_FUZZY.sub(lambda m: m.group(1)[0], p)


def edit_distance(a: str, b: str) -> int:
    """Levenshtein 距离（滚动数组，短串足够快）。"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def similarity(a_pinyin: str, a_syllables: str, b_pinyin: str, b_syllables: str) -> float:
    """两个词的读音相似度，0.0 ~ 1.0。"""
    na = normalize(a_pinyin, a_syllables)
    nb = normalize(b_pinyin, b_syllables)
    if not na or not nb:
        return 0.0
    longest = max(len(na), len(nb))
    return max(0.0, 1.0 - edit_distance(na, nb) / longest)


def looks_like_correction(
    a_pinyin: str,
    a_syllables: str,
    a_surface: str,
    b_pinyin: str,
    b_syllables: str,
    b_surface: str,
    threshold: float = DEFAULT_THRESHOLD,
) -> bool:
    """判断 a（先学到的）是否像是 b（后学到的）的一次纠正。"""
    if a_surface == b_surface:
        return False
    return similarity(a_pinyin, a_syllables, b_pinyin, b_syllables) >= threshold
