"""临时 XD31H 十六进制转换工具，不访问串口或修改测试工程状态。"""

from __future__ import annotations

import argparse
import re


def modbus_crc16(data: bytes) -> int:
    """计算 Modbus RTU CRC16，返回主机整数形式的 16 位校验值。"""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def parse_hex_bytes(text: str) -> bytes:
    """解析空格或连续格式的十六进制字节，并兼容 RX/TX 文本前缀。"""
    match = re.search(r"\b(?:RX|TX)\s*[:：]\s*(.*)", text, re.IGNORECASE)
    if match is not None:
        text = match.group(1)

    compact = re.sub(r"(?i)0x", "", text.strip())
    compact = re.sub(r"[\s,，:：-]+", "", compact)
    if not compact:
        raise ValueError("没有找到十六进制数据")
    if len(compact) % 2 != 0:
        raise ValueError("十六进制字符数量必须是偶数")
    if re.fullmatch(r"[0-9a-fA-F]+", compact) is None:
        raise ValueError("输入包含非十六进制字符")
    return bytes.fromhex(compact)


def _format_crc(frame: bytes) -> str:
    """格式化末尾低字节在前的 Modbus CRC 校验结果。"""
    if len(frame) < 3:
        return "CRC：数据不足，无法校验"
    received = frame[-2] | (frame[-1] << 8)
    calculated = modbus_crc16(frame[:-2])
    state = "正确" if received == calculated else "错误"
    return (
        f"CRC：接收=0x{received:04X}，计算=0x{calculated:04X}，{state}"
    )


def _format_xd31h_response(frame: bytes) -> list[str]:
    """按工程中的状态、量程和原始值布局格式化 XD31H 九字节响应。"""
    status = frame[3]
    range_value = frame[4]
    raw_value = (frame[5] << 8) | frame[6]
    lines = [
        "类型：XD31H 测量响应",
        f"从机地址：{frame[0]}",
        f"功能码：0x{frame[1]:02X}",
        f"状态：{status}" + ("（正常）" if status == 0 else "（测量无效）"),
        f"量程：{range_value}",
        f"原始值：0x{raw_value:04X} = {raw_value}",
    ]

    if status == 0 and range_value in (0, 1, 2):
        divisor = (10, 100, 1000)[range_value]
        digits = range_value + 1
        lines.append(f"电阻值：{raw_value / divisor:.{digits}f} Ω")
    elif status == 1:
        lines.append("电阻值：超量程（OL）")
    else:
        lines.append("电阻值：无法换算")

    lines.append(_format_crc(frame))
    return lines


def _format_modbus_request(frame: bytes) -> list[str]:
    """格式化工程当前使用的 03 功能码八字节读取请求。"""
    start_address = (frame[2] << 8) | frame[3]
    register_count = (frame[4] << 8) | frame[5]
    return [
        "类型：Modbus 03 读取请求",
        f"从机地址：{frame[0]}",
        f"起始地址：0x{start_address:04X}",
        f"寄存器数量：{register_count}",
        _format_crc(frame),
    ]


def format_conversion(data: bytes) -> str:
    """输出通用进制转换信息，并在格式匹配时追加 XD31H/Modbus 解析。"""
    lines: list[str] = []
    if len(data) >= 10 and data[-2:] == b"\r\n":
        data = data[:-2]
        lines.append("提示：已移除末尾的 0D 0A 回车换行")

    lines.extend(
        [
            "十六进制：" + " ".join(f"{byte:02X}" for byte in data),
            "逐字节十进制：" + " ".join(str(byte) for byte in data),
            f"合并大端整数：{int.from_bytes(data, byteorder='big')}",
        ]
    )

    if (
        len(data) == 9
        and data[1] == 0x03
        and data[2] == 0x04
    ):
        lines.extend(_format_xd31h_response(data))
    elif len(data) == 8 and data[1] == 0x03:
        lines.extend(_format_modbus_request(data))
    else:
        lines.append("类型：普通十六进制数据")
    return "\n".join(lines)


def convert_text(text: str) -> str:
    """把一行用户输入转换为可直接显示的中文结果。"""
    return format_conversion(parse_hex_bytes(text))


def main() -> None:
    """处理单次命令行输入，或进入可连续粘贴帧的交互模式。"""
    parser = argparse.ArgumentParser(description="XD31H 十六进制帧转换工具")
    parser.add_argument("hex_data", nargs="*", help="十六进制字节或完整帧")
    arguments = parser.parse_args()

    if arguments.hex_data:
        try:
            print(convert_text(" ".join(arguments.hex_data)))
        except ValueError as error:
            parser.error(str(error))
        return

    print("粘贴十六进制帧；输入 q 或 exit 退出。")
    while True:
        try:
            text = input("HEX> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if text.lower() in {"q", "quit", "exit"}:
            break
        if not text:
            continue
        try:
            print(convert_text(text))
        except ValueError as error:
            print(f"输入错误：{error}")
        print()


if __name__ == "__main__":
    main()
