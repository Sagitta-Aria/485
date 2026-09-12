"""Compile-check actual slave identity macros without running or flashing firmware."""

from __future__ import annotations

import ast
from pathlib import Path
import re
import subprocess
import unittest

from c_toolchain import resolve_c_compiler
from cable_tester.ui import gui as gui


ROOT = Path(__file__).resolve().parents[2]
HOST = ROOT / "host"


class SlaveIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.compiler = resolve_c_compiler(ROOT).executable

    def _compile(self, source: str, side: int, index: int, *, preprocess: bool = False) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.compiler, "-std=c11", "-Werror", "-I", str(ROOT),
             "-I", str(HOST / "tests/firmware/topology_stubs"),
             f"-DBOARD_SLAVE_MASTER_INDEX={side}", f"-DBOARD_SLAVE_INDEX={index}",
             *(["-E", "-P"] if preprocess else ["-fsyntax-only"]), "-x", "c", "-"],
            input=source, capture_output=True, text=True, timeout=20,
        )

    def test_all_twenty_wifi_ids_are_unique_and_bus_addresses_repeat_per_side(self) -> None:
        names = set()
        for side in (1, 2):
            for index in range(1, 11):
                with self.subTest(side=side, index=index):
                    source = (
                        '#include "slave/main/wifi_server.h"\n'
                        f'_Static_assert(BOARD_RS485_NODE_ID == {0x10 + index}, "bus_address");\n'
                        'const char *test_wifi_id = WIFI_DEVICE_ID;\n'
                    )
                    compiled = self._compile(source, side, index)
                    self.assertEqual(compiled.returncode, 0, compiled.stderr)
                    expanded = self._compile(source, side, index, preprocess=True)
                    self.assertEqual(expanded.returncode, 0, expanded.stderr)
                    match = re.search(r"const char \*test_wifi_id = ([^;]+);", expanded.stdout)
                    self.assertIsNotNone(match)
                    name = ast.literal_eval(match[1])
                    self.assertEqual(name, f"m{side}-s{index}")
                    self.assertIn(name, gui.SCOPED_SLAVE_WIFI_IDS)
                    names.add(name)
        self.assertEqual(len(names), 20)

    def test_invalid_side_or_slave_index_fails_compilation(self) -> None:
        for side, index in ((0, 1), (3, 1), (1, 0), (2, 11)):
            with self.subTest(side=side, index=index):
                result = self._compile('#include "slave/main/board_config.h"\n', side, index)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("must be", result.stderr)


if __name__ == "__main__":
    unittest.main()
