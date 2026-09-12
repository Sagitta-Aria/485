from __future__ import annotations

import time
import unittest
from unittest import mock

import cable_tester_gui as gui
import dual_node_measurement as dual


class DualNodePureTests(unittest.TestCase):
    def test_fixed_mapping_closes_voltage_then_current_leads(self) -> None:
        spec = dual.build_dual_node_spec("S1", 0, 1, "S2", 2, 3)

        self.assertEqual(
            spec.switch_commands,
            (
                "SWITCH S1 1 Y2 ON",
                "SWITCH S2 3 Y1 ON",
                "SWITCH S2 2 Y0 ON",
                "SWITCH S1 0 Y3 ON",
            ),
        )

    def test_rejects_duplicate_or_out_of_range_ports(self) -> None:
        with self.assertRaises(ValueError):
            dual.build_dual_node_spec("S1", 0, 0, "S2", 2, 3)
        with self.assertRaises(ValueError):
            dual.build_dual_node_spec("S1", 0, 1, "S1", 1, 3)
        with self.assertRaises(ValueError):
            dual.build_dual_node_spec("S1", 0, 24, "S2", 2, 3)


class DualNodeControllerTests(unittest.TestCase):
    def _wait_until_stopped(self, controller: dual.DualNodeMeasurementController) -> None:
        deadline = time.monotonic() + 1.0
        while controller.running and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(controller.running)

    def test_controller_runs_four_switches_and_keeps_matrix_closed(self) -> None:
        commands: list[str] = []
        events: list[tuple[str, object]] = []
        controller: dual.DualNodeMeasurementController

        def send_request(target_id: str, request_id: str, command: str) -> str:
            commands.append(command)
            payload = f"OK {command}"
            self.assertTrue(controller.feed_result(target_id, request_id, payload))
            return f"OK FORWARDED {target_id} {request_id}"

        controller = dual.DualNodeMeasurementController(
            send_request,
            lambda event_type, message: events.append((event_type, message)),
            settle_seconds=0.0,
            response_timeout_seconds=0.1,
        )
        self.assertTrue(controller.start("ESP1", "S1", 0, 1, "S2", 2, 3))
        self._wait_until_stopped(controller)

        self.assertEqual(
            commands,
            [
                "SWITCH S1 1 Y2 ON",
                "SWITCH S2 3 Y1 ON",
                "SWITCH S2 2 Y0 ON",
                "SWITCH S1 0 Y3 ON",
            ],
        )
        complete = [message for event, message in events if event == "dual_node_complete"]
        self.assertEqual(len(complete), 1)
        self.assertTrue(complete[0]["kept_closed"])

    def test_switch_error_keeps_partial_matrix_and_publishes_error(self) -> None:
        commands: list[str] = []
        events: list[tuple[str, object]] = []
        controller: dual.DualNodeMeasurementController

        def send_request(target_id: str, request_id: str, command: str) -> str:
            commands.append(command)
            payload = (
                "ERR SWITCH INTERNAL"
                if command == "SWITCH S1 4 Y3 ON"
                else f"OK {command}"
            )
            self.assertTrue(controller.feed_result(target_id, request_id, payload))
            return f"OK FORWARDED {target_id} {request_id}"

        controller = dual.DualNodeMeasurementController(
            send_request,
            lambda event_type, message: events.append((event_type, message)),
            settle_seconds=0.0,
            response_timeout_seconds=0.1,
        )
        self.assertTrue(controller.start("ESP1", "S1", 4, 5, "S2", 6, 7))
        self._wait_until_stopped(controller)

        self.assertEqual(commands[-1], "SWITCH S1 4 Y3 ON")
        self.assertTrue(any(event == "dual_node_error" for event, _ in events))

    def test_gui_button_passes_positive_and_negative_pairs(self) -> None:
        app = gui.CableTesterApp.__new__(gui.CableTesterApp)
        app.batch_calibration = mock.Mock(running=False)
        app.auxiliary_loop_test = mock.Mock(running=False)
        app.sequential_loop_test = mock.Mock(running=False)
        app.pairwise_resistance_test = mock.Mock(running=False)
        app.four_wire_loop_test = mock.Mock(running=False)
        app.dual_node_measurement = mock.Mock(running=False)
        app.dual_node_measurement.start.return_value = True
        app.target_id = mock.Mock()
        app.target_id.get.return_value = "ESP1"
        app.dual_positive_bank = mock.Mock()
        app.dual_positive_bank.get.return_value = "S1"
        app.dual_positive_x1 = mock.Mock()
        app.dual_positive_x1.get.return_value = "0"
        app.dual_positive_x2 = mock.Mock()
        app.dual_positive_x2.get.return_value = "1"
        app.dual_negative_bank = mock.Mock()
        app.dual_negative_bank.get.return_value = "S2"
        app.dual_negative_x1 = mock.Mock()
        app.dual_negative_x1.get.return_value = "2"
        app.dual_negative_x2 = mock.Mock()
        app.dual_negative_x2.get.return_value = "3"
        app.router = mock.Mock(running=True)
        app.batch_calibration_button = mock.Mock()
        app.auxiliary_test_button = mock.Mock()
        app.sequential_test_button = mock.Mock()
        app.pairwise_test_button = mock.Mock()
        app.dual_node_measurement_button = mock.Mock()
        app.dual_node_status = mock.Mock()
        app._append_log = mock.Mock()

        app._run_dual_node_measurement()

        app.dual_node_measurement.start.assert_called_once_with(
            "ESP1", "S1", 0, 1, "S2", 2, 3
        )
        app.dual_node_measurement_button.configure.assert_called_once_with(
            text="停止双节点闭合"
        )


if __name__ == "__main__":
    unittest.main()
