"""拼音相似度的单元测试。用用户报告的真实错字当用例。"""

from __future__ import annotations

import unittest
from typing import ClassVar

from imehot.similarity import (
    DEFAULT_THRESHOLD,
    edit_distance,
    looks_like_correction,
    normalize,
    normalize_syllable,
    similarity,
)


class NormalizeTest(unittest.TestCase):
    def test_initial_fuzzy(self) -> None:
        # zhang → (zh→z) zang → (ang→an) zan：两级模糊音叠加
        self.assertEqual(normalize_syllable("shuo"), "suo")
        self.assertEqual(normalize_syllable("zhang"), "zan")
        self.assertEqual(normalize_syllable("chi"), "ci")

    def test_n_to_l(self) -> None:
        self.assertEqual(normalize_syllable("nan"), "lan")
        self.assertEqual(normalize_syllable("ni"), "li")

    def test_final_fuzzy(self) -> None:
        self.assertEqual(normalize_syllable("xing"), "xin")
        self.assertEqual(normalize_syllable("shang"), "san")
        self.assertEqual(normalize_syllable("cheng"), "cen")

    def test_empty(self) -> None:
        self.assertEqual(normalize_syllable(""), "")

    def test_normalize_uses_syllables(self) -> None:
        # 有音节边界时，n→l 只在声母位置生效，不会破坏韵尾
        self.assertEqual(
            normalize("nanan", "23"),
            normalize_syllable("na") + normalize_syllable("nan"),
        )

    def test_normalize_without_syllables_is_conservative(self) -> None:
        # 没有音节信息时不碰 n/韵母，只做 zh/ch/sh
        self.assertEqual(normalize("shuo", ""), "suo")
        self.assertEqual(normalize("nan", ""), "nan")

    def test_normalize_empty(self) -> None:
        self.assertEqual(normalize("", ""), "")


class EditDistanceTest(unittest.TestCase):
    def test_identical(self) -> None:
        self.assertEqual(edit_distance("abc", "abc"), 0)

    def test_empty(self) -> None:
        self.assertEqual(edit_distance("", "abc"), 3)
        self.assertEqual(edit_distance("abc", ""), 3)

    def test_one_edit(self) -> None:
        self.assertEqual(edit_distance("abc", "abd"), 1)
        self.assertEqual(edit_distance("abc", "ab"), 1)

    def test_classic(self) -> None:
        self.assertEqual(edit_distance("kitten", "sitting"), 3)


class RealTypoTest(unittest.TestCase):
    """用户真实案例：说思路 被打成 所思路 / 所示录。"""

    CASES: ClassVar[tuple[tuple[str, str, str], ...]] = (
        # (拼音, 音节长度串, 词面) —— 长度串必须与拼音长度自洽
        ("suosilu", "322", "所思路"),
        ("shuosilu", "422", "说思路"),
        ("suoshilu", "332", "所示录"),
    )

    def test_all_normalize_to_same(self) -> None:
        normalized = {normalize(py, syl) for py, syl, _ in self.CASES}
        self.assertEqual(normalized, {"suosilu"})

    def test_typos_pair_with_correction(self) -> None:
        correct_py, correct_syl, correct_surface = self.CASES[1]
        for py, syl, surface in (self.CASES[0], self.CASES[2]):
            with self.subTest(surface=surface):
                self.assertTrue(
                    looks_like_correction(
                        py, syl, surface, correct_py, correct_syl, correct_surface
                    )
                )

    def test_similarity_is_one(self) -> None:
        py, syl, _ = self.CASES[0]
        cpy, csyl, _ = self.CASES[1]
        self.assertAlmostEqual(similarity(py, syl, cpy, csyl), 1.0)


class NegativeTest(unittest.TestCase):
    def test_different_words_do_not_pair(self) -> None:
        self.assertFalse(looks_like_correction("huice", "32", "回测", "huiche", "32", "回车"))

    def test_identical_surface_never_pairs(self) -> None:
        self.assertFalse(looks_like_correction("huice", "32", "回测", "huice", "32", "回测"))

    def test_empty_pinyin(self) -> None:
        self.assertEqual(similarity("", "", "huice", "32"), 0.0)

    def test_threshold_is_respected(self) -> None:
        # 只差一个字母 → 相似度 < 1，用极高阈值时应判为不相似
        sim = similarity("huice", "32", "huicee", "33")
        self.assertLess(sim, 1.0)
        self.assertFalse(
            looks_like_correction("huice", "32", "回测", "huicee", "33", "回测测", threshold=0.99)
        )
        self.assertGreaterEqual(sim, 0.0)


class ThresholdSanityTest(unittest.TestCase):
    def test_default_threshold_in_range(self) -> None:
        self.assertTrue(0.5 < DEFAULT_THRESHOLD <= 1.0)


if __name__ == "__main__":
    unittest.main()
