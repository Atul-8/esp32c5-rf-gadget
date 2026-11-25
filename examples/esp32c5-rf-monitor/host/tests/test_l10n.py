# -*- coding: utf-8 -*-
"""英文界面「零中文」的正式断言：下行 payload 全过一遍，逐条查 CJK。

为什么要有这条测试：英文界面里的文案有两个来源——
  1. 网页里的静态词条（``web/js/i18n.js`` 的 ``en`` 词典）；
  2. 模块现算出来的判词/表格/增益状态（``modes/`` 里是中文）。
第 2 类没法靠前端翻，只能在 ``host/l10n.py`` 沿着 Python→JS 的边界换成英文。
少写一条模板，中文就会漏进英文界面，而这种漏是肉眼在界面上一眼看不全的，
所以这里把 10 个场景的**所有**下行出口都扫一遍。

跑法：``cd host && python tests/test_l10n.py -v``
"""
from __future__ import annotations

import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import paths  # noqa: E402 - 本地上位机模块

paths.ensure_on_path()

import l10n  # noqa: E402
from session import HostConfig, HostSession, to_json  # noqa: E402

#: 10 个内置假场景（tests/mock_source.py 的全部场景）
SCENARIOS = (
    "quiet",
    "wifi_busy",
    "ble_crowded",
    "unknown_cw_clean",
    "unknown_cw",
    "video_58g",
    "microwave",
    "mixed",
    "wifi_suppress_ble",
    "hotel_scan",
)

CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")

#: 每个场景灌多少帧（0.5 s/帧）。凑够 12 s 就有判据样本，再多只是白等
STEPS = 12

#: 下行的所有出口（新增一个入口就要加到这里，否则它会绕过本测试）
OUTLETS = ("tables", "describe", "stats", "sweep_state", "gain_state")


def _walk(node: object, path: str, out: list[tuple[str, str]]) -> None:
    """递归收集所有含 CJK 的字符串（带 JSON 路径，报错时能直接定位）。"""
    if isinstance(node, str):
        if CJK.search(node):
            out.append((path, node))
    elif isinstance(node, dict):
        for key, val in node.items():
            _walk(val, f"{path}.{key}", out)
    elif isinstance(node, (list, tuple)):
        for val in node:
            _walk(val, f"{path}[]", out)


def _snapshot_payload(session: HostSession, lang: str) -> list[tuple[str, str]]:
    """跑一段假时间（够凑出判据样本），再把各下行出口全扫一遍。"""
    found: list[tuple[str, str]] = []
    session.open(
        HostConfig.from_json(
            to_json({"port": "MOCK", "mock": True, "scenario": "quiet", "lang": lang})
        )
    )
    try:
        for _ in range(STEPS):
            session.step(0.5, on_snapshot=lambda snap: None)
        for name in OUTLETS:
            _walk(getattr(session, name)(), name, found)
        text = session.render_text()
        if not text.strip():
            found.append(("render_text", "<<空>>"))
        for line in text.split("\n"):
            if CJK.search(line):
                found.append(("render_text", line.strip()[:160]))
    finally:
        session.close()
    return found


class EnglishHasNoChineseTest(unittest.TestCase):
    """英文模式：10 个场景 × 5 个出口 + render_text，一条中文都不许剩。"""

    def test_en_payload_has_no_cjk(self) -> None:
        offenders: list[str] = []
        for scen in SCENARIOS:
            session = HostSession(bins_out=256)
            try:
                session.open(
                    HostConfig.from_json(
                        to_json({"port": "MOCK", "mock": True, "scenario": scen, "lang": "en"})
                    )
                )
                for _ in range(STEPS):
                    session.step(0.5, on_snapshot=lambda snap: None)
                found: list[tuple[str, str]] = []
                for name in OUTLETS:
                    _walk(getattr(session, name)(), name, found)
                for line in session.render_text().split("\n"):
                    if CJK.search(line):
                        found.append(("render_text", line.strip()[:160]))
            finally:
                session.close()
            for path, text in found:
                offenders.append(f"{scen} {path}: {text}")
        self.assertEqual(
            offenders[:20],
            [],
            f"英文模式还有 {len(offenders)} 条中文下行文案（补 host/l10n.py 的 TABLE）",
        )

    def test_en_render_text_looks_english(self) -> None:
        """ASCII 页改成「从已本地化的表格拼英文」，得真拼出东西来。"""
        session = HostSession(bins_out=256)
        try:
            session.open(
                HostConfig.from_json(
                    to_json({"port": "MOCK", "mock": True, "scenario": "mixed", "lang": "en"})
                )
            )
            for _ in range(STEPS):
                session.step(0.5, on_snapshot=lambda snap: None)
            text = session.render_text()
        finally:
            session.close()
        self.assertGreater(len(text), 200)
        self.assertIn("Periodic spectrum waterfall", text)
        self.assertIsNone(CJK.search(text), "render_text(en) 里还有中文")


class ChineseStillChineseTest(unittest.TestCase):
    """中文模式不能被误翻成英文（防止「为了英文把中文也改了」）。"""

    def test_zh_payload_keeps_chinese(self) -> None:
        """中文模式必须仍有相当量的中文 —— 防的是「为了英文把中文也改了」。

        门槛是**实测值**定的，不是拍的：``OUTLETS`` 五个出口 + ``render_text``
        在 ``--mock --scenario quiet`` 下实测 16 条（tables 10 / describe 1 /
        render_text 5，stats·sweep_state·gain_state 这三个出口是纯数值）。
        取 12 留四分之一余量。**改动 ``OUTLETS`` 后必须重新实测这个数**
        （原先门槛是 20，是因为当时还有一个 ``audit`` 出口带着 4 条边界文案）。
        """
        found = _snapshot_payload(HostSession(bins_out=256), "zh")
        self.assertGreater(len(found), 12, "中文模式下行几乎没有中文了，可能是误配 l10n")

    def test_zh_render_text_keeps_chinese(self) -> None:
        session = HostSession(bins_out=256)
        try:
            session.open(
                HostConfig.from_json(
                    to_json({"port": "MOCK", "mock": True, "scenario": "mixed", "lang": "zh"})
                )
            )
            for _ in range(STEPS):
                session.step(0.5, on_snapshot=lambda snap: None)
            text = session.render_text()
        finally:
            session.close()
        self.assertIsNotNone(CJK.search(text), "render_text(zh) 全变英文了")


class TranslateUnitTest(unittest.TestCase):
    """``l10n`` 的三个纯函数：能翻、翻不到就原样返回、绝不抛异常。"""

    def test_skeleton_collapses_numbers(self) -> None:
        self.assertEqual(l10n.skeleton("带宽 12.5 MHz、占空 58%"), l10n.skeleton("带宽 3 MHz、占空 9%"))
        self.assertIn("{}", l10n.skeleton("带宽 12.5 MHz"))

    def test_known_string_translated(self) -> None:
        text = l10n.localize("重度")
        self.assertFalse(CJK.search(str(text)), f"「重度」没翻：{text}")

    def test_unknown_string_passes_through(self) -> None:
        """翻不到就原样返回——界面不能因为一条文案挂掉，也不能吞内容。"""
        weird = "这条文案一定不在词典里 42"
        self.assertEqual(l10n.localize(weird), weird)

    def test_placeholder_mismatch_passes_through(self) -> None:
        """模板占位数量与原文数字段不一致 → 原样返回（宁可留中文，也不要数字错位）。"""
        # 造一条「骨架对得上、但模板占位少一个」的词条：模板 1 个 {} 对上原文 2 个数字段
        key = "样例 {} {} 测试"
        l10n.TABLE[key] = "sample {} test"
        try:
            probe = "样例 1 2 测试"
            self.assertEqual(l10n.skeleton(probe), key)
            self.assertEqual(l10n.translate(probe), probe)
        finally:
            l10n.TABLE.pop(key, None)

    def test_non_string_untouched(self) -> None:
        self.assertEqual(l10n.localize(3.5), 3.5)
        self.assertEqual(l10n.localize(None), None)
        self.assertEqual(l10n.localize(True), True)

    def test_negative_number_keeps_its_sign(self) -> None:
        """负数必须整段当一个数字段，且回填时把负号带回英文。

        踩过：骨架正则只看 ``\d+``，「开关幅度 -2.0 码」的骨架会变成 ``开关幅度 -{} 码``，
        与词典里的 ``开关幅度 {} 码`` 差一个减号 → 模板不命中 → 英文界面漏一整段中文。
        这种漏只在**真板安静频段**（开关幅度为负）才出现，假场景是造不出来的。
        """
        # 骨架层面：带不带负号必须落在同一个键上（差一个减号就整段漏翻）
        self.assertEqual(l10n.skeleton("开关幅度 -2.0 码"), l10n.skeleton("开关幅度 2.0 码"))
        # 回填层面：负数整段喂进 {}，负号跟着回来，绝不能被抹成绝对值
        key = l10n.skeleton("样例 -2.0 码")
        l10n.TABLE[key] = "sample {} codes"
        try:
            self.assertEqual(l10n.translate("样例 -2.0 码"), "sample -2.0 codes")
        finally:
            l10n.TABLE.pop(key, None)

    def test_dict_keys_not_translated(self) -> None:
        """只翻值、不翻 key：前端是按 key 取数的，翻了就取不到。"""
        out = l10n.localize({"重度": "重度"})
        self.assertIn("重度", out)
        self.assertFalse(CJK.search(str(out["重度"])))


class ListSentenceTest(unittest.TestCase):
    """列表长度会变的句子（「有明显抬升的信道：ch1, ch2, ...」）。

    踩过：真板上一次列出 6 个信道，而词典里只按 mock 见过的 1/4/5/9/10/11/12 个备了条目，
    差那 2 个长度就整句漏中文，且**假场景永远造不出这个长度**。现在改成正则换外壳，
    长度随便多少都能翻，这组用例把 1~12 全铺一遍。
    """

    def test_clear_rise_any_length(self) -> None:
        for n in range(1, 13):
            channels = ", ".join(f"ch{i}" for i in range(1, n + 1))
            text = f"有明显抬升（轻度以上）的信道：{channels}。"
            out = l10n.translate(text)
            self.assertFalse(l10n.has_cjk(out), f"{n} 个信道没翻干净：{out}")
            self.assertEqual(out, f"Channels with a clear rise (mild or worse): {channels}.")

    def test_clear_rise_none(self) -> None:
        out = l10n.translate("有明显抬升（轻度以上）的信道：无。")
        self.assertEqual(out, "Channels with a clear rise (mild or worse): none.")

    def test_avoid_and_busy_any_length(self) -> None:
        for n in range(1, 13):
            channels = ", ".join(f"ch{i}" for i in range(1, n + 1))
            avoid = l10n.translate(f"5G 建议避开（占用偏高）：{channels}。")
            self.assertFalse(l10n.has_cjk(avoid), f"避开句 {n} 个没翻干净：{avoid}")
            # 单信道时词典里是 "avoid this"（单数），列表正则给的是 "avoid these"
            self.assertIn(
                avoid,
                (f"5G: avoid this (occupancy too high): {channels}.",
                 f"5G: avoid these (occupancy too high): {channels}."),
            )
            busy = l10n.translate(f"2.4G 在用的 WiFi 信道：{channels}")
            self.assertFalse(l10n.has_cjk(busy), f"在用句 {n} 个没翻干净：{busy}")
            self.assertEqual(busy, f"WiFi channels in use on 2.4G: {channels}")

    def test_list_rules_do_not_eat_normal_sentences(self) -> None:
        """列表正则只能命中那几句，别把普通句子的数字也当成列表。"""
        self.assertIsNone(l10n._translate_list("带宽 20 MHz"))
        self.assertIsNone(l10n._translate_list("有明显抬升（轻度以上）的信道"))  # 没有冒号/句号


class ErrorTextTest(unittest.TestCase):
    """异常文案必须零中文：它会经 ``[RF error]`` / ``Last error`` / 日志进界面。

    踩过：真板自检中 ``SerialLinkError: 连接已标记失效，请重新打开`` 从 ``span.err``
    和底部日志里露出来——错误文案走的是另一条路（不是表格 payload），只有真板出错
    才会出现，mock 全绿也抓不到。
    """

    CASES = [
        "SerialLinkError: 连接已标记失效，请重新打开",
        "SerialLinkError: 频谱帧解析失败：帧长度不符：262176 != 544",
        "SerialLinkError: 帧同步丢失：b'abcd'（需重新连接）",
        "SerialLinkError: 串口读取超时（无数据）",
        "SerialLinkError: FSM STOP 未收到 FSMEND（在途帧没排空）",
        "SerialLinkError: 固件未通告频谱能力（CAPS 无 SPECCAPS），无法扫描",
        "SnapshotError: bins_db 长度异常：300",
        "RuntimeError: 这条文案一定不在片段表里 42",
    ]

    def test_translate_error_never_keeps_cjk(self) -> None:
        for text in self.CASES:
            out = l10n.translate_error(text)
            self.assertFalse(l10n.has_cjk(out), f"异常文案还留着中文：{out}")
            self.assertTrue(out.strip(), f"异常文案被剔空了：{text}")

    def test_translate_error_keeps_numbers(self) -> None:
        out = l10n.translate_error("SerialLinkError: 频谱帧解析失败：帧长度不符：262176 != 544")
        self.assertIn("262176", out)
        self.assertIn("544", out)

    def test_translate_error_keeps_plain_ascii(self) -> None:
        plain = "SerialLinkError: timeout"
        self.assertEqual(l10n.translate_error(plain), plain)

    def test_error_text_zh_keeps_original(self) -> None:
        """中文模式不许动原文——终端与中文界面看的是同一句话。"""
        from session import error_text

        text = "SerialLinkError: 连接已标记失效，请重新打开"
        self.assertEqual(error_text("zh", Exception("连接已标记失效，请重新打开")), "Exception: 连接已标记失效，请重新打开")
        self.assertEqual(error_text("zh", text), text)

    def test_error_text_en_uses_translator(self) -> None:
        from session import error_text

        out = error_text("en", Exception("连接已标记失效，请重新打开"))
        self.assertFalse(l10n.has_cjk(out), out)
        self.assertIn("reopen the port", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)