"""Compile the real RS485 framing layer and drive it with scripted wire traffic.

This is the only coverage for master/main/rs485_bus.c: the frame encoder, the
Modbus CRC16, the SOF resynchronizer and the inter-frame silence check. The
harness links the production source against a clean-room stub set in
tests/firmware/rs485_bus_stubs, whose clock advances one 10 ms tick per blocking
read so a byte stream can be placed inside or outside the inter-frame gap.
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from c_toolchain import build_environment, resolve_c_compiler


ROOT = Path(__file__).resolve().parents[2]
HOST = ROOT / "host"

# The production file is shared verbatim between the two firmwares; this test
# covers the master copy and the equality check below keeps them from drifting.
MASTER_SOURCE = ROOT / "master/main/rs485_bus.c"
SLAVE_SOURCE = ROOT / "slave/main/rs485_bus.c"


class RS485BusFramingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        compiler = resolve_c_compiler(ROOT)
        cls._temporary = tempfile.TemporaryDirectory(prefix="rs485_bus_", dir=HOST / "tests")
        output = Path(cls._temporary.name) / ("rs485_bus.dll" if os.name == "nt" else "rs485_bus.so")
        command = [*compiler.command, "-std=c11", "-Wall", "-Wextra", "-Werror", "-O1", "-shared"]
        if os.name != "nt":
            command += ["-fPIC"]
        command += ["-I", str(HOST / "tests/firmware/rs485_bus_stubs"),
                    str(HOST / "tests/firmware/rs485_bus_test.c"), "-o", str(output)]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=120,
                           env=build_environment(compiler, ROOT))
            cls._library = ctypes.CDLL(str(output))
        except subprocess.CalledProcessError as error:
            cls._temporary.cleanup()
            raise RuntimeError(f"RS485 bus C harness build failed:\n{error.stdout}\n{error.stderr}") from error

    @classmethod
    def tearDownClass(cls) -> None:
        if os.name == "nt" and cls._library:
            import _ctypes

            _ctypes.FreeLibrary(cls._library._handle)
        cls._library = None
        cls._temporary.cleanup()

    def _check(self, name: str) -> None:
        function = getattr(self._library, name)
        function.argtypes = []
        function.restype = ctypes.c_int
        self.assertEqual(function(), 0, f"C assertion failed at the returned source line in {name}")

    def test_both_firmwares_ship_the_same_framing_layer(self) -> None:
        self.assertEqual(MASTER_SOURCE.read_bytes(), SLAVE_SOURCE.read_bytes(),
                         "master and slave rs485_bus.c must stay identical")

    def test_frames_separated_by_an_idle_line_are_delivered(self) -> None:
        self._check("rs485_bus_test_gap_separated_frames")

    def test_noise_is_skipped_and_oversized_length_is_rejected(self) -> None:
        self._check("rs485_bus_test_noise_then_frame")

    def test_frame_embedded_in_a_noise_burst_is_rejected(self) -> None:
        self._check("rs485_bus_test_gap_rejects_embedded_frame")

    def test_silence_check_never_exceeds_the_requested_timeout(self) -> None:
        self._check("rs485_bus_test_gap_respects_deadline")


if __name__ == "__main__":
    unittest.main()
