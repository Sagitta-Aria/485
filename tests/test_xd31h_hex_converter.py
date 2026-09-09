from __future__ import annotations

import unittest

import xd31h_hex_converter as converter


class Xd31hHexConverterTests(unittest.TestCase):
    """验证临时转换脚本与固件 XD31H 解析规则保持一致。"""

    def test_decodes_range_zero_response_and_crc(self) -> None:
        result = converter.convert_text("01 03 04 00 00 05 C5 39 30")

        self.assertIn("原始值：0x05C5 = 1477", result)
        self.assertIn("电阻值：147.7 Ω", result)
        self.assertIn("计算=0x3039，正确", result)

    def test_decodes_range_one_and_two(self) -> None:
        range_one = converter.convert_text("01 03 04 00 01 15 00 A5 63")
        range_two = converter.convert_text("01 03 04 00 02 03 0A DB 04")

        self.assertIn("电阻值：53.76 Ω", range_one)
        self.assertIn("电阻值：0.778 Ω", range_two)

    def test_accepts_rx_prefix_and_contiguous_hex(self) -> None:
        spaced = converter.parse_hex_bytes("RX：01 03 04 00 00 04 D8 F8 A9")
        contiguous = converter.parse_hex_bytes("010304000004D8F8A9")

        self.assertEqual(spaced, contiguous)

    def test_strips_serial_tool_crlf_from_request(self) -> None:
        result = converter.convert_text("TX：010300000002C40B0D0A")

        self.assertIn("已移除末尾的 0D 0A", result)
        self.assertIn("寄存器数量：2", result)
        self.assertIn("计算=0x0BC4，正确", result)


if __name__ == "__main__":
    unittest.main()
