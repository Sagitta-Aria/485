from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import auxiliary_loop_test as auxiliary
import cable_tester_gui as gui


class FakeTransport:
    """Return deterministic measurements based on auxiliary SWITCH state."""

    def __init__(self, assisted_value: float = 4.0) -> None:
        self.commands: list[str] = []
        self.assisted = False
        self.assisted_value = assisted_value

    def request(self, command: str) -> str:
        self.commands.append(command)
        if command.startswith("CONNECT "):
            self.assisted = False
            return f"OK {command}"
        if command.startswith("SWITCH "):
            self.assisted = True
            return f"OK {command}"
        if command == "MEASURE":
            resistance = self.assisted_value if self.assisted else 9.0
            return (
                f"OK MEASURE resistance={resistance:.3f} "
                f"raw={round(resistance * 100)} range=1"
            )
        if command == "RESET":
            self.assisted = False
            return "OK RESET"
        raise AssertionError(f"unexpected command: {command}")


class AuxiliaryLoopPureTests(unittest.TestCase):
    def test_candidate_parser_supports_ranges_and_deduplicates(self) -> None:
        self.assertEqual(auxiliary.parse_candidates("1-3,2,5"), [1, 2, 3, 5])

        with self.assertRaises(ValueError):
            auxiliary.parse_candidates("3-1")
        with self.assertRaises(ValueError):
            auxiliary.parse_candidates("24")

    def test_auxiliary_loop_uses_four_expected_kelvin_switches(self) -> None:
        self.assertEqual(
            auxiliary.auxiliary_loop_commands(7),
            (
                "SWITCH S1 7 Y2 ON",
                "SWITCH S1 7 Y3 ON",
                "SWITCH S2 7 Y1 ON",
                "SWITCH S2 7 Y0 ON",
            ),
        )

    def test_summary_uses_median_error_and_mad(self) -> None:
        summary = auxiliary.summarize_values([8.9, 9.0, 9.1], 0.0)

        self.assertAlmostEqual(summary["median_ohm"], 9.0)
        self.assertAlmostEqual(summary["absolute_median_error_ohm"], 9.0)
        self.assertAlmostEqual(summary["mad_ohm"], 0.1)


class AuxiliaryLoopFlowTests(unittest.TestCase):
    def test_assisted_condition_configures_samples_and_resets(self) -> None:
        transport = FakeTransport()
        result = auxiliary.run_condition(
            transport,
            auxiliary.TargetPair(),
            phase="screening",
            candidate_x=3,
            assisted=True,
            sample_count=3,
            settle_seconds=0.0,
            sample_interval_seconds=0.0,
            sleep=lambda seconds: None,
        )

        self.assertEqual(len(result.readings), 3)
        self.assertEqual(
            transport.commands,
            [
                "CONNECT S1 0 S2 0",
                "SWITCH S1 3 Y2 ON",
                "SWITCH S1 3 Y3 ON",
                "SWITCH S2 3 Y1 ON",
                "SWITCH S2 3 Y0 ON",
                "MEASURE",
                "MEASURE",
                "MEASURE",
                "RESET",
            ],
        )

    def test_experiment_selects_lowest_verified_error(self) -> None:
        class CandidateTransport(FakeTransport):
            def request(self, command: str) -> str:
                if command.startswith("SWITCH S1 "):
                    self.assisted_value = {
                        1: 5.0,
                        2: 2.0,
                        3: 3.0,
                    }[int(command.split()[2])]
                return super().request(command)

        outcome = auxiliary.run_experiment(
            CandidateTransport(),
            auxiliary.TargetPair(),
            [1, 2, 3],
            target_ohm=0.0,
            screen_samples=2,
            verify_samples=3,
            finalist_count=2,
            settle_seconds=0.0,
            sample_interval_seconds=0.0,
            seed=7,
            progress=lambda message: None,
            sleep=lambda seconds: None,
        )

        best = outcome["best"]
        self.assertEqual(best["candidate_x"], 2)
        self.assertAlmostEqual(best["paired_improvement_ohm"], 7.0)
        self.assertTrue(best["improvement_confirmed"])

    def test_report_writes_json_and_csv(self) -> None:
        outcome = auxiliary.run_experiment(
            FakeTransport(),
            auxiliary.TargetPair(),
            [1],
            target_ohm=0.0,
            screen_samples=1,
            verify_samples=1,
            finalist_count=1,
            settle_seconds=0.0,
            sample_interval_seconds=0.0,
            seed=1,
            progress=lambda message: None,
            sleep=lambda seconds: None,
        )
        report = {
            "target_id": "ESP1",
            "target_resistance_ohm": 0.0,
            **outcome,
        }
        with tempfile.TemporaryDirectory() as directory:
            json_path, csv_path = auxiliary.write_report_files(
                report, Path(directory)
            )

            self.assertTrue(json_path.exists())
            self.assertTrue(csv_path.exists())
            self.assertIn("candidate_x", csv_path.read_text(encoding="utf-8-sig"))


class AuxiliaryLoopControllerTests(unittest.TestCase):
    def test_gui_controller_completes_and_returns_best_loop(self) -> None:
        events: list[tuple[str, object]] = []
        commands: list[str] = []
        assisted = False
        candidate_x = 0
        controller: auxiliary.AuxiliaryLoopTestController

        def send_request(target_id: str, request_id: str, command: str) -> str:
            nonlocal assisted, candidate_x
            commands.append(command)
            if command.startswith("CONNECT "):
                assisted = False
                payload = f"OK {command}"
            elif command.startswith("SWITCH "):
                assisted = True
                candidate_x = int(command.split()[2])
                payload = f"OK {command}"
            elif command == "MEASURE":
                resistance = (
                    {1: 5.0, 2: 2.0}[candidate_x] if assisted else 9.0
                )
                payload = (
                    f"OK MEASURE resistance={resistance:.3f} "
                    f"raw={round(resistance * 100)} range=1"
                )
            elif command == "RESET":
                assisted = False
                payload = "OK RESET"
            else:
                self.fail(f"unexpected command: {command}")
            self.assertTrue(
                controller.feed_result(target_id, request_id, payload)
            )
            return f"OK FORWARDED {target_id} {request_id}"

        with tempfile.TemporaryDirectory() as directory:
            controller = auxiliary.AuxiliaryLoopTestController(
                send_request,
                lambda event_type, message: events.append((event_type, message)),
                report_root=Path(directory),
                candidates=(1, 2),
                screen_samples=1,
                verify_samples=1,
                finalist_count=1,
                settle_seconds=0.0,
                sample_interval_seconds=0.0,
                response_timeout_seconds=0.1,
            )
            self.assertTrue(controller.start("ESP1"))

            deadline = time.monotonic() + 2.0
            while controller.running and time.monotonic() < deadline:
                time.sleep(0.01)

            self.assertFalse(controller.running)
            completed = [
                message
                for event_type, message in events
                if event_type == "aux_test_complete"
            ]
            errors = [
                message
                for event_type, message in events
                if event_type == "aux_test_error"
            ]
            self.assertEqual(errors, [])
            self.assertEqual(len(completed), 1)
            self.assertEqual(completed[0]["candidate_x"], 2)
            self.assertEqual(commands[-1], "RESET")

    @mock.patch("cable_tester_gui.messagebox.askokcancel", return_value=True)
    def test_gui_button_starts_test_for_selected_target(
        self, _confirmation: mock.Mock
    ) -> None:
        app = gui.CableTesterApp.__new__(gui.CableTesterApp)
        app.batch_calibration = mock.Mock(running=False)
        app.auxiliary_loop_test = mock.Mock()
        app.auxiliary_loop_test.start.return_value = True
        app.sequential_loop_test = mock.Mock(running=False)
        app.pairwise_resistance_test = mock.Mock(running=False)
        app.router = mock.Mock(running=True)
        app.target_id = mock.Mock()
        app.target_id.get.return_value = "ESP2"
        app.root = mock.Mock()
        app.batch_calibration_button = mock.Mock()
        app.auxiliary_test_button = mock.Mock()
        app.sequential_test_button = mock.Mock()
        app.pairwise_test_button = mock.Mock()
        app.auxiliary_test_status = mock.Mock()
        app._append_log = mock.Mock()

        app._run_auxiliary_loop_test()

        app.auxiliary_loop_test.start.assert_called_once_with("ESP2")
        app.batch_calibration_button.state.assert_called_once_with(["disabled"])
        app.auxiliary_test_button.state.assert_called_once_with(["disabled"])
        app.auxiliary_test_status.set.assert_called_once_with(
            "辅助回路测试：正在准备"
        )


if __name__ == "__main__":
    unittest.main()
