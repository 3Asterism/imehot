"""词条质检的单元测试。"""

from __future__ import annotations

import unittest

from imehot.promote import is_cjk, is_emoji, judge


class CharClassTest(unittest.TestCase):
    def test_emoji(self) -> None:
        for ch in "🎆🪛🍵🏠👴🐔✅⬆️🧵":
            self.assertTrue(is_emoji(ch), ch)

    def test_not_emoji(self) -> None:
        for ch in "回测abc测":
            self.assertFalse(is_emoji(ch), ch)

    def test_cjk(self) -> None:
        self.assertTrue(is_cjk("回"))
        self.assertFalse(is_cjk("a"))


class JudgeTest(unittest.TestCase):
    def test_chinese_word_passes(self) -> None:
        self.assertTrue(judge("回测", "huice").ok)
        self.assertTrue(judge("郝晓晞", "haoxiaoxi").ok)

    def test_single_chinese_char_rejected(self) -> None:
        self.assertFalse(judge("衢", "qu").ok)

    def test_emoji_rejected(self) -> None:
        self.assertFalse(judge("🎆", "yanhua").ok)
        self.assertFalse(judge("🪛", "luosidao").ok)

    def test_single_ascii_letter_rejected(self) -> None:
        self.assertFalse(judge("S", "S", allow_english=True).ok)

    def test_english_rejected_by_default(self) -> None:
        self.assertFalse(judge("community", "community").ok)

    def test_english_allowed_opt_in(self) -> None:
        self.assertTrue(judge("community", "community", allow_english=True).ok)

    def test_digits_rejected(self) -> None:
        self.assertFalse(judge("123", "123", allow_english=True).ok)

    def test_empty_rejected(self) -> None:
        self.assertFalse(judge("", "").ok)

    def test_min_cjk_respected(self) -> None:
        self.assertFalse(judge("中文", "zhongwen", min_cjk=3).ok)
        self.assertTrue(judge("中文词", "zhongwenci", min_cjk=3).ok)


class GarbageRuleTest(unittest.TestCase):
    """来自 ime-lexicon 的垃圾词规则。"""

    def test_too_long_is_sentence(self) -> None:
        sentence = "这是一整句话不是一个词因为我们一次上屏了十六个字以上"
        self.assertGreater(len(sentence), 16)
        self.assertFalse(judge(sentence, "zheshi").ok)

    def test_length_boundary(self) -> None:
        self.assertTrue(judge("一二三四五六七八九十一二三四五六", "x").ok)  # 16 字刚好
        self.assertFalse(judge("一二三四五六七八九十一二三四五六七", "x").ok)  # 17 字

    def test_bad_suffix_rejected(self) -> None:
        self.assertFalse(judge("这样的话", "zheyangdehua").ok)
        self.assertFalse(judge("真的吧", "zhendeba").ok)
        self.assertTrue(judge("公司", "gongsi").ok)

    def test_english_pinyin_equals_surface_is_fine(self) -> None:
        # 原生词库里英文词的读音就是词面本身，不能因此误杀
        self.assertTrue(judge("community", "community", allow_english=True).ok)


if __name__ == "__main__":
    unittest.main()
