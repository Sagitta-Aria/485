"""Run the firmware's actual portable C lease and response-correlation helpers."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from c_toolchain import build_environment, resolve_c_compiler


ROOT = Path(__file__).resolve().parents[1]


class RS485TopologyProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        compiler = resolve_c_compiler(ROOT)
        native_clang = "clang" in Path(compiler.executable).name.lower()
        if native_clang and os.name == "nt":
            targets = subprocess.run(
                [*compiler.command, "--print-targets"], check=True, capture_output=True,
                text=True, timeout=10,
            ).stdout
            if "x86-64" not in targets:
                raise RuntimeError(
                    f"{compiler.executable} has no native Windows x86-64 backend, so it cannot "
                    "build the portable C test. Set CABLE_HOST_CC to a working compiler."
                )
        cls._temp = tempfile.TemporaryDirectory(prefix="rs485_protocol_", dir=ROOT / "tests")
        output = Path(cls._temp.name) / ("protocol.dll" if os.name == "nt" else "protocol.so")
        command = [*compiler.command, "-std=c11", "-Wall", "-Wextra", "-Werror", "-O1", "-ffreestanding"]
        if compiler.is_zig:
            command += ["-target", "x86_64-windows-gnu", "-shared"]
        elif os.name == "nt" and native_clang:
            command += ["--target=x86_64-pc-windows-msvc", "-fuse-ld=lld", "-nostdlib", "-shared", "-Wl,/noentry"]
        elif os.name == "nt":
            command += ["-shared"]
        else:
            command += ["-shared", "-fPIC"]
        command += [str(ROOT / "tests/firmware/rs485_topology_protocol_test.c"), "-o", str(output)]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=180,
                           env=build_environment(compiler, ROOT))
            cls._library = ctypes.CDLL(str(output))
        except subprocess.CalledProcessError as error:
            cls._temp.cleanup()
            raise RuntimeError(f"Portable C test build failed:\n{error.stdout}\n{error.stderr}") from error
        except Exception:
            cls._temp.cleanup()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        if os.name == "nt":
            import _ctypes

            _ctypes.FreeLibrary(cls._library._handle)
        del cls._library
        cls._temp.cleanup()

    def _check(self, name: str) -> None:
        function = getattr(self._library, name)
        function.restype = ctypes.c_int
        function.argtypes = []
        self.assertEqual(function(), 0, f"C regression failed at source line returned by {name}")

    def test_idempotency_ownership_and_clear_tombstone(self) -> None:
        self._check("rs485_test_lease")

    def test_watchdog_tick_wrap_and_retry_renewal(self) -> None:
        self._check("rs485_test_clock_wrap")

    def test_wire_byte_order_and_expansion_limit(self) -> None:
        self._check("rs485_test_payload")

    def test_stale_session_step_mask_nonce_reply_rejected(self) -> None:
        self._check("rs485_test_stale_ack")


if __name__ == "__main__":
    unittest.main()
