"""原生数据解析的单元测试。"""

from __future__ import annotations

import unittest

from imehot.native import cf_to_unix, split_pinyin, unix_to_cf


class SplitPinyinTest(unittest.TestCase):
    def test_even(self) -> None:
        # 真实值：郝晓晞 / haoxiaoxi / "342" → hao(3) xiao(4) xi(2)
        self.assertEqual(split_pinyin("haoxiaoxi", "342"), "hao xiao xi")

    def test_uneven(self) -> None:
        self.assertEqual(split_pinyin("zouyu", "32"), "zou yu")

    def test_three_syllables(self) -> None:
        self.assertEqual(split_pinyin("luosidao", "323"), "luo si dao")

    def test_single(self) -> None:
        self.assertEqual(split_pinyin("qu", "2"), "qu")

    def test_lengths_sum_mismatch_falls_back(self) -> None:
        self.assertEqual(split_pinyin("abc", "13"), "abc")

    def test_empty_lengths_falls_back(self) -> None:
        self.assertEqual(split_pinyin("abc", ""), "abc")

    def test_zero_length_falls_back(self) -> None:
        self.assertEqual(split_pinyin("abc", "03"), "abc")

    def test_non_digit_falls_back(self) -> None:
        self.assertEqual(split_pinyin("abc", "xx"), "abc")

    def test_empty_pinyin(self) -> None:
        self.assertEqual(split_pinyin("", "33"), "")


class CfTimeTest(unittest.TestCase):
    def test_roundtrip(self) -> None:
        unix = 1_800_000_000.0
        self.assertAlmostEqual(cf_to_unix(unix_to_cf(unix)), unix)

    def test_zero_is_none(self) -> None:
        self.assertIsNone(cf_to_unix(0))
        self.assertIsNone(cf_to_unix(0.0))
        self.assertIsNone(cf_to_unix(None))

    def test_negative_is_none(self) -> None:
        self.assertIsNone(cf_to_unix(-5))


if __name__ == "__main__":
    unittest.main()
