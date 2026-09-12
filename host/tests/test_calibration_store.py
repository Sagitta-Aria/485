from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cable_tester.ui import gui as gui
from cable_tester.devices.calibration_store import (
    CalibrationProfileError,
    CalibrationStore,
    expected_port_labels,
)


def profile_values(base: float) -> dict[str, float]:
    """Create one complete deterministic 48-port profile for tests."""
    return {
        label: base
        for label in expected_port_labels()
    }


class CalibrationStoreTests(unittest.TestCase):
    """Verify persistence, ID isolation, and independent endpoint subtraction."""

    def test_profiles_are_persisted_in_separate_device_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = CalibrationStore(root)
            esp1_values = profile_values(0.0)
            esp2_values = profile_values(0.0)
            esp1_values["S1_X10"] = 10.0
            esp1_values["S2_X4"] = 7.0
            esp2_values["S1_X10"] = 1.0
            esp2_values["S2_X4"] = 2.0

            esp1_path = store.activate("ESP1", esp1_values)
            esp2_path = store.activate("ESP2", esp2_values)

            self.assertEqual(esp1_path, root / "ESP1" / "active.json")
            self.assertEqual(esp2_path, root / "ESP2" / "active.json")
            reloaded = CalibrationStore(root)
            esp1 = reloaded.correct("ESP1", "S1_X10", "S2_X4", 35.0)
            esp2 = reloaded.correct("ESP2", "S1_X10", "S2_X4", 35.0)
            assert esp1 is not None and esp2 is not None
            self.assertEqual(esp1.positive_offset_ohm, 10.0)
            self.assertEqual(esp1.negative_offset_ohm, 7.0)
            self.assertEqual(esp1.corrected_ohm, 18.0)
            self.assertEqual(esp2.corrected_ohm, 32.0)

    def test_gui_measurement_display_uses_selected_device_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = CalibrationStore(Path(temporary_directory))
            values = profile_values(0.0)
            values["S1_X10"] = 10.0
            values["S2_X4"] = 7.0
            store.activate("ESP1", values)

            app = gui.CableTesterApp.__new__(gui.CableTesterApp)
            app.calibration_store = store
            app._active_routes = {"ESP1": ("S1_X10", "S2_X4")}
            app._pending_commands = {("ESP1", "GUI-1"): "MEASURE"}
            app.batch_calibration = mock.Mock()
            app.batch_calibration.feed_result.return_value = False
            app.auxiliary_loop_test = mock.Mock()
            app.auxiliary_loop_test.feed_result.return_value = False
            app.sequential_loop_test = mock.Mock()
            app.sequential_loop_test.feed_result.return_value = False
            app.pairwise_resistance_test = mock.Mock()
            app.pairwise_resistance_test.feed_result.return_value = False
            app._append_log = mock.Mock()

            app._handle_result_frame(
                "ESP1",
                "GUI-1",
                "OK MEASURE resistance=35.000 raw=350 range=0",
            )

            final_message = app._append_log.call_args_list[-1].args[1]
            self.assertIn("resistance=18.000", final_message)
            self.assertIn("positive=S1_X10:10.000", final_message)
            self.assertIn("negative=S2_X4:7.000", final_message)
            self.assertIn("raw_resistance=35.000", final_message)

    def test_candidates_are_id_isolated_and_selection_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            profile_root = root / "profiles"
            report_root = root / "reports"
            values = profile_values(4.0)
            esp1_report = report_root / "2026-08" / "esp1.json"
            esp2_report = report_root / "2026-08" / "esp2.json"
            esp1_report.parent.mkdir(parents=True)
            for path, target_id, report_id in (
                (esp1_report, "ESP1", "20260811-001"),
                (esp2_report, "ESP2", "20260811-002"),
            ):
                path.write_text(
                    json.dumps(
                        {
                            "report_id": report_id,
                            "target_id": target_id,
                            "finished_at": "2026-08-11T14:03:00+08:00",
                            "complete": True,
                            "warnings": ["人工确认项"],
                            "estimates_ohm": values,
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )

            store = CalibrationStore(profile_root, report_root)
            candidates = store.list_candidates("ESP1")
            self.assertEqual([item.report_id for item in candidates], ["20260811-001"])
            self.assertTrue(candidates[0].usable)
            store.activate_candidate("ESP1", candidates[0])

            selected = store.load_selection("ESP1")
            assert selected is not None
            self.assertTrue(selected.enabled)
            self.assertEqual(selected.source_report_id, "20260811-001")
            self.assertEqual(selected.quality_warning_count, 1)
            self.assertIsNotNone(store.correct("ESP1", "S1_X0", "S2_X0", 20.0))

            store.set_enabled("ESP1", False)
            reloaded = CalibrationStore(profile_root, report_root)
            disabled = reloaded.load_selection("ESP1")
            assert disabled is not None
            self.assertFalse(disabled.enabled)
            self.assertIsNone(reloaded.correct("ESP1", "S1_X0", "S2_X0", 20.0))

            reloaded.set_enabled("ESP1", True)
            correction = reloaded.correct("ESP1", "S1_X0", "S2_X0", 20.0)
            assert correction is not None
            self.assertEqual(correction.corrected_ohm, 12.0)

    def test_negative_candidate_can_only_enter_through_manual_candidate_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            report_root = root / "reports"
            report_root.mkdir()
            values = profile_values(1.0)
            values["S1_X0"] = -0.5
            (report_root / "invalid.json").write_text(
                json.dumps(
                    {
                        "report_id": "20260811-003",
                        "target_id": "ESP1",
                        "finished_at": "2026-08-11T15:00:00+08:00",
                        "complete": True,
                        "warnings": ["S1_X0 解得负阻值"],
                        "estimates_ohm": values,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            store = CalibrationStore(root / "profiles", report_root)
            candidate = store.list_candidates("ESP1")[0]

            self.assertTrue(candidate.usable)
            self.assertEqual(candidate.negative_count, 1)
            with self.assertRaises(CalibrationProfileError):
                store.activate("ESP1", values)
            store.activate_candidate("ESP1", candidate)
            correction = store.correct("ESP1", "S1_X0", "S2_X0", 10.0)
            assert correction is not None
            self.assertEqual(correction.corrected_ohm, 9.5)

    def test_log_levels_distinguish_normal_warning_and_error(self) -> None:
        self.assertEqual(gui.classify_log_level("接收", "OK MEASURE"), "success")
        self.assertEqual(
            gui.classify_log_level("发送", "GUI -> ESP1 GUI-1 PING"),
            "normal",
        )
        self.assertTrue(
            gui.is_normal_transport_log("接收", "RESULT ESP1 GUI GUI-1 OK PONG")
        )
        self.assertFalse(
            gui.is_normal_transport_log(
                "接收", "RESULT master GUI GUI-2 OK BUS_SENT broadcast RESET"
            )
        )
        self.assertEqual(
            gui.classify_log_level(
                "接收", "RESULT master GUI GUI-2 OK BUS_SENT broadcast RESET"
            ),
            "success",
        )
        self.assertFalse(
            gui.is_normal_transport_log(
                "接收", "RESULT ESP1 GUI GUI-2 OK MEASURE resistance=1.000"
            )
        )
        self.assertEqual(gui.classify_log_level("警告", "未加载校准"), "warning")
        self.assertEqual(
            gui.classify_log_level("接收", "ERR MEASURE OVERRANGE"),
            "error",
        )


if __name__ == "__main__":
    unittest.main()
