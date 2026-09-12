"""Compile the real flash journal and exercise it against fault-injected NOR."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import zlib

from c_toolchain import build_environment, resolve_c_compiler

ROOT = Path(__file__).resolve().parents[2]
HOST = ROOT / "host"


class TopologyJournalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        compiler = resolve_c_compiler(ROOT)
        cls._temporary = tempfile.TemporaryDirectory(prefix="journal_c_", dir=HOST / "tests")
        cls._library = None
        output = Path(cls._temporary.name) / ("journal.dll" if os.name == "nt" else "journal.so")
        command = [*compiler.command, "-std=c11", "-Wall", "-Wextra", "-Werror", "-O1", "-shared"]
        if os.name != "nt":
            command += ["-fPIC"]
        command += ["-I", str(HOST / "tests/firmware/journal_stubs"), "-I", str(HOST / "tests/firmware/topology_stubs")]
        command += [str(HOST / "tests/firmware/topology_journal_test.c"), "-o", str(output)]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=120,
                           env=build_environment(compiler, ROOT))
            cls._library = ctypes.CDLL(str(output))
        except subprocess.CalledProcessError as error:
            cls._temporary.cleanup()
            raise RuntimeError(f"Journal C harness build failed:\n{error.stdout}\n{error.stderr}") from error

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
        self.assertEqual(function(), 0, f"C assertion line in {name}")

    def test_ack_replay_owner_and_identity_crc(self) -> None:
        self.assertEqual(zlib.crc32(b"1234 1 17 TOPO_DATA 1234 17 0 2 25380"), 0x3C2A7B92)
        self._check("journal_test_ack_replay_and_crc")

    def test_every_byte_of_interrupted_append(self) -> None:
        self._check("journal_test_append_power_cuts")

    def test_every_byte_of_interrupted_ack_commit(self) -> None:
        self._check("journal_test_ack_power_cuts")

    def test_physical_watermarks_full_cache_and_ring_wrap(self) -> None:
        self._check("journal_test_watermark_and_ring")

    def test_unacked_corruption_and_interrupted_sector_erase(self) -> None:
        self._check("journal_test_corruption_and_erase_recovery")

    def test_interrupted_metadata_bank_erase(self) -> None:
        self._check("journal_test_metadata_bank_power_cuts")

    def test_metadata_bank_switch_keeps_unacked_records(self) -> None:
        self._check("journal_test_metadata_switch_keeps_unacked_records")

    def test_successful_driver_return_with_missing_program(self) -> None:
        self._check("journal_test_silent_program_failure")


if __name__ == "__main__":
    unittest.main()
