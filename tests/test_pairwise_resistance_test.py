from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import cable_tester_gui as gui
import pairwise_resistance_test as pairwise


class PairwiseResistancePureTests(unittest.TestCase):
    def test_plan_contains_both_directions_for_all_1128_pairs(self) -> None:
        ports = pairwise.build_port_order()
        plan = pairwise.build_pair_plan(ports)

        self.assertEqual(len(ports), 48)
        self.assertEqual(len(plan), 2256)
        self.assertEqual(plan[0].label, "S1_X0 - S1_X1")
        self.assertEqual(plan[1].label, "S1_X1 - S1_X0")
        self.assertEqual(plan[92].label, "S1_X0 - S2_X23")
        self.assertEqual(plan[93].label, "S2_X23 - S1_X0")
        self.assertEqual(plan[94].label, "S1_X1 - S1_X2")
        self.assertEqual(plan[-2].label, "S2_X22 - S2_X23")
        self.assertEqual(plan[-1].label, "S2_X23 - S2_X22")
        keys = {(spec.first.label, spec.second.label) for spec in plan}
        self.assertEqual(len(keys), len(plan))
        unordered_keys = {
            frozenset((spec.first.label, spec.second.label)) for spec in plan
        }
        self.assertEqual(len(unordered_keys), 1128)
        for index in range(0, len(plan), 2):
            self.assertEqual(plan[index].first, plan[index + 1].second)
            self.assertEqual(plan[index].second, plan[index + 1].first)

    def test_default_settle_time_is_exactly_one_second(self) -> None:
        self.assertEqual(pairwise.DEFAULT_SETTLE_SECONDS, 1.0)

    def test_measurement_decoder_preserves_raw_response(self) -> None:
        spec = pairwise.PairSpec(
            pairwise.MatrixPort("S1", 0), pairwise.MatrixPort("S2", 23)
        )
        payload = "OK MEASURE resistance=12.345 raw=12345 range=2"

        result = pairwise.decode_measurement(spec, f"OK {spec.command}", payload)

        self.assertEqual(result.resistance_ohm, 12.345)
        self.assertEqual(result.raw_value, 12345)
        self.assertEqual(result.range_code, 2)
        self.assertEqual(result.measure_response, payload)

    def test_rechecked_measurement_averages_three_followups(self) -> None:
        spec = pairwise.PairSpec(
            pairwise.MatrixPort("S1", 0), pairwise.MatrixPort("S1", 1)
        )
        first = "OK MEASURE resistance=25.000 raw=25000 range=1"
        followups = [
            "OK MEASURE resistance=10.000 raw=10000 range=1",
            "OK MEASURE resistance=20.000 raw=20000 range=1",
            "OK MEASURE resistance=30.000 raw=30000 range=1",
        ]

        result = pairwise.decode_rechecked_measurement(
            spec, f"OK {spec.command}", first, followups
        )

        self.assertEqual(result.resistance_ohm, 20.0)
        self.assertEqual(result.measurement_count, 4)
        self.assertTrue(result.averaged_recheck)
        self.assertEqual(result.initial_classification, "over20")
        self.assertEqual(result.measure_response, first)
        self.assertEqual(result.additional_measure_responses, tuple(followups))

    def test_error_first_measurement_also_rechecks_and_marks_error(self) -> None:
        spec = pairwise.PairSpec(
            pairwise.MatrixPort("S1", 0), pairwise.MatrixPort("S1", 1)
        )
        followups = [
            "OK MEASURE resistance=1.000 raw=1000 range=1",
            "OK MEASURE resistance=2.000 raw=2000 range=1",
            "OK MEASURE resistance=3.000 raw=3000 range=1",
        ]

        result = pairwise.decode_rechecked_measurement(
            spec, f"OK {spec.command}", "ERR MEASURE TIMEOUT received=0", followups
        )

        self.assertEqual(result.resistance_ohm, 2.0)
        self.assertEqual(result.status, "初测错误，3次复测平均")
        self.assertEqual(result.raw_value, None)
        self.assertEqual(result.measurement_count, 4)
        self.assertEqual(result.initial_classification, "error")


class PairwiseResistanceControllerTests(unittest.TestCase):
    def test_controller_waits_measures_resets_then_writes_report(self) -> None:
        events: list[tuple[str, object]] = []
        commands: list[str] = []
        writer_observations: list[tuple[pairwise.PairwiseSession, list[str]]] = []
        controller: pairwise.PairwiseResistanceController
        plan = pairwise.build_pair_plan()[:2]
        measurement_index = 0

        def send_request(target_id: str, request_id: str, command: str) -> str:
            nonlocal measurement_index
            commands.append(command)
            if command.startswith("CONNECT "):
                payload = f"OK {command}"
            elif command == "MEASURE":
                measurement_index += 1
                payload = (
                    f"OK MEASURE resistance={measurement_index:.3f} "
                    f"raw={measurement_index * 100} range=1"
                )
            elif command == "RESET":
                payload = "OK RESET"
            else:
                self.fail(f"unexpected command: {command}")
            self.assertTrue(controller.feed_result(target_id, request_id, payload))
            return f"OK FORWARDED {target_id} {request_id}"

        def write_report(
            session: pairwise.PairwiseSession, report_root: Path
        ) -> Path:
            writer_observations.append((session, list(commands)))
            return report_root / "report.xlsx"

        with tempfile.TemporaryDirectory() as directory:
            controller = pairwise.PairwiseResistanceController(
                send_request,
                lambda event_type, message: events.append((event_type, message)),
                report_root=Path(directory),
                settle_seconds=0.0,
                response_timeout_seconds=0.1,
                pairs=plan,
                report_writer=write_report,
            )
            self.assertTrue(controller.start("ESP1"))
            deadline = time.monotonic() + 1.0
            while controller.running and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertFalse(controller.running)
        self.assertEqual(
            commands,
            [plan[0].command, "MEASURE", plan[1].command, "MEASURE", "RESET"],
        )
        self.assertEqual(len(writer_observations), 1)
        session, commands_at_write = writer_observations[0]
        self.assertEqual(commands_at_write[-1], "RESET")
        self.assertEqual([result.resistance_ohm for result in session.results], [1.0, 2.0])
        completed = [message for event, message in events if event == "pairwise_complete"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["successful"], 2)

    def test_cancel_during_settle_resets_and_exports_partial_report(self) -> None:
        events: list[tuple[str, object]] = []
        commands: list[str] = []
        sessions: list[pairwise.PairwiseSession] = []
        connect_completed = threading.Event()
        controller: pairwise.PairwiseResistanceController
        plan = pairwise.build_pair_plan()[:1]

        def send_request(target_id: str, request_id: str, command: str) -> str:
            commands.append(command)
            payload = f"OK {command}"
            self.assertTrue(controller.feed_result(target_id, request_id, payload))
            if command.startswith("CONNECT "):
                connect_completed.set()
            return f"OK FORWARDED {target_id} {request_id}"

        def write_report(
            session: pairwise.PairwiseSession, report_root: Path
        ) -> Path:
            sessions.append(session)
            return report_root / "partial.xlsx"

        with tempfile.TemporaryDirectory() as directory:
            controller = pairwise.PairwiseResistanceController(
                send_request,
                lambda event_type, message: events.append((event_type, message)),
                report_root=Path(directory),
                settle_seconds=1.0,
                response_timeout_seconds=0.1,
                pairs=plan,
                report_writer=write_report,
            )
            self.assertTrue(controller.start("ESP1"))
            self.assertTrue(connect_completed.wait(timeout=0.5))
            controller.cancel()
            deadline = time.monotonic() + 1.0
            while controller.running and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertFalse(controller.running)
        self.assertEqual(commands, [plan[0].command, "RESET"])
        self.assertEqual(len(sessions), 1)
        self.assertTrue(sessions[0].cancelled)
        self.assertEqual(sessions[0].results, [])
        stopped = [message for event, message in events if event == "pairwise_stopped"]
        self.assertEqual(len(stopped), 1)
        self.assertEqual(stopped[0]["completed"], 0)

    def test_high_first_measurement_rechecks_without_matrix_reset(self) -> None:
        commands: list[str] = []
        sessions: list[pairwise.PairwiseSession] = []
        controller: pairwise.PairwiseResistanceController
        plan = pairwise.build_pair_plan()[:1]
        measurement_responses = iter(
            [
                "OK MEASURE resistance=25.000 raw=25000 range=1",
                "OK MEASURE resistance=10.000 raw=10000 range=1",
                "OK MEASURE resistance=20.000 raw=20000 range=1",
                "OK MEASURE resistance=30.000 raw=30000 range=1",
            ]
        )

        def send_request(target_id: str, request_id: str, command: str) -> str:
            commands.append(command)
            if command.startswith("CONNECT "):
                payload = f"OK {command}"
            elif command == "MEASURE":
                payload = next(measurement_responses)
            elif command == "RESET":
                payload = "OK RESET"
            else:
                self.fail(f"unexpected command: {command}")
            self.assertTrue(controller.feed_result(target_id, request_id, payload))
            return f"OK FORWARDED {target_id} {request_id}"

        def write_report(
            session: pairwise.PairwiseSession, report_root: Path
        ) -> Path:
            sessions.append(session)
            return report_root / "report.xlsx"

        with tempfile.TemporaryDirectory() as directory:
            controller = pairwise.PairwiseResistanceController(
                send_request,
                lambda _event_type, _message: None,
                report_root=Path(directory),
                settle_seconds=0.0,
                response_timeout_seconds=0.1,
                pairs=plan,
                report_writer=write_report,
            )
            self.assertTrue(controller.start("ESP1"))
            deadline = time.monotonic() + 1.0
            while controller.running and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertEqual(
            commands,
            [plan[0].command, "MEASURE", "MEASURE", "MEASURE", "MEASURE", "RESET"],
        )
        self.assertEqual(len(sessions), 1)
        result = sessions[0].results[0]
        self.assertEqual(result.resistance_ohm, 20.0)
        self.assertEqual(result.measurement_count, 4)
        self.assertEqual(len(result.additional_measure_responses), 3)
        self.assertEqual(result.initial_classification, "over20")

    @mock.patch("cable_tester_gui.messagebox.askokcancel", return_value=True)
    def test_gui_button_starts_selected_target(self, _confirmation: mock.Mock) -> None:
        app = gui.CableTesterApp.__new__(gui.CableTesterApp)
        app.batch_calibration = mock.Mock(running=False)
        app.auxiliary_loop_test = mock.Mock(running=False)
        app.sequential_loop_test = mock.Mock(running=False)
        app.pairwise_resistance_test = mock.Mock(running=False)
        app.pairwise_resistance_test.start.return_value = True
        app.router = mock.Mock(running=True)
        app.target_id = mock.Mock()
        app.target_id.get.return_value = "ESP2"
        app.root = mock.Mock()
        app.batch_calibration_button = mock.Mock()
        app.auxiliary_test_button = mock.Mock()
        app.sequential_test_button = mock.Mock()
        app.pairwise_test_button = mock.Mock()
        app.pairwise_test_status = mock.Mock()
        app._append_log = mock.Mock()

        app._toggle_pairwise_resistance_test()

        app.pairwise_resistance_test.start.assert_called_once_with("ESP2")
        app.batch_calibration_button.state.assert_called_once_with(["disabled"])
        app.auxiliary_test_button.state.assert_called_once_with(["disabled"])
        app.sequential_test_button.state.assert_called_once_with(["disabled"])
        app.pairwise_test_button.configure.assert_called_once_with(
            text="停止阻值测试"
        )
        app.pairwise_test_status.set.assert_called_once_with(
            "全引脚阻值：正在准备"
        )


if __name__ == "__main__":
    unittest.main()
