from __future__ import annotations

import time
import unittest
from unittest import mock

import cable_tester_gui as gui
import sequential_loop_test as sequence


class SequentialLoopPureTests(unittest.TestCase):
    def test_y0_sequence_runs_from_s1_x0_through_s2_x23(self) -> None:
        points = sequence.build_y0_sequence()

        self.assertEqual(len(points), 48)
        self.assertEqual(points[0].label, "S1_X0")
        self.assertEqual(points[1].label, "S2_X0")
        self.assertEqual(points[2].label, "S1_X1")
        self.assertEqual(points[-2].label, "S1_X23")
        self.assertEqual(points[-1].label, "S2_X23")
        self.assertEqual(points[0].route_label, "Y0 -> S1_X0")
        self.assertEqual(points[1].route_label, "Y0 -> S2_X0")
        self.assertEqual(points[0].command(True), "SWITCH S1 0 Y0 ON")
        self.assertEqual(points[-1].command(False), "SWITCH S2 23 Y0 OFF")


class SequentialLoopControllerTests(unittest.TestCase):
    def test_controller_opens_failed_point_and_resets(self) -> None:
        events: list[tuple[str, object]] = []
        commands: list[str] = []
        controller: sequence.SequentialLoopTestController

        def send_request(target_id: str, request_id: str, command: str) -> str:
            commands.append(command)
            if command == "SWITCH S1 0 Y0 ON":
                payload = "ERR SWITCH ESP_FAIL"
            elif command == "RESET":
                payload = "OK RESET"
            else:
                payload = f"OK {command}"
            self.assertTrue(controller.feed_result(target_id, request_id, payload))
            return f"OK FORWARDED {target_id} {request_id}"

        controller = sequence.SequentialLoopTestController(
            send_request,
            lambda event_type, message: events.append((event_type, message)),
            step_seconds=0.0,
            response_timeout_seconds=0.1,
        )
        self.assertTrue(controller.start("ESP1"))

        deadline = time.monotonic() + 1.0
        while controller.running and time.monotonic() < deadline:
            time.sleep(0.01)

        self.assertFalse(controller.running)
        self.assertEqual(
            commands,
            [
                "RESET",
                "SWITCH S1 0 Y0 ON",
                "SWITCH S1 0 Y0 OFF",
                "RESET",
            ],
        )
        errors = [message for event, message in events if event == "sequence_error"]
        self.assertEqual(len(errors), 1)

    def test_controller_completes_one_cycle_then_resets(self) -> None:
        events: list[tuple[str, object]] = []
        commands: list[str] = []
        controller: sequence.SequentialLoopTestController

        def send_request(target_id: str, request_id: str, command: str) -> str:
            commands.append(command)
            payload = "OK RESET" if command == "RESET" else f"OK {command}"
            self.assertTrue(controller.feed_result(target_id, request_id, payload))
            if command == "SWITCH S2 23 Y0 OFF":
                controller.cancel()
            return f"OK FORWARDED {target_id} {request_id}"

        controller = sequence.SequentialLoopTestController(
            send_request,
            lambda event_type, message: events.append((event_type, message)),
            step_seconds=0.0,
            response_timeout_seconds=0.1,
        )
        self.assertTrue(controller.start("ESP1"))

        deadline = time.monotonic() + 2.0
        while controller.running and time.monotonic() < deadline:
            time.sleep(0.01)

        self.assertFalse(controller.running)
        self.assertEqual(commands[0], "RESET")
        self.assertEqual(commands[1], "SWITCH S1 0 Y0 ON")
        self.assertEqual(commands[2], "SWITCH S1 0 Y0 OFF")
        self.assertEqual(commands[3], "SWITCH S2 0 Y0 ON")
        self.assertEqual(commands[4], "SWITCH S2 0 Y0 OFF")
        for command_index in range(1, len(commands) - 2, 2):
            self.assertTrue(commands[command_index].endswith(" Y0 ON"))
            self.assertTrue(commands[command_index + 1].endswith(" Y0 OFF"))
            self.assertEqual(
                commands[command_index].removesuffix(" ON"),
                commands[command_index + 1].removesuffix(" OFF"),
            )
        self.assertEqual(commands[-3], "SWITCH S2 23 Y0 ON")
        self.assertEqual(commands[-2], "SWITCH S2 23 Y0 OFF")
        self.assertEqual(commands[-1], "RESET")
        stopped = [message for event, message in events if event == "sequence_stopped"]
        self.assertEqual(len(stopped), 1)
        self.assertEqual(stopped[0]["completed_steps"], 48)
        self.assertEqual(stopped[0]["completed_cycles"], 1)

    @mock.patch("cable_tester_gui.messagebox.askokcancel", return_value=True)
    def test_gui_button_starts_selected_target(self, _confirmation: mock.Mock) -> None:
        app = gui.CableTesterApp.__new__(gui.CableTesterApp)
        app.batch_calibration = mock.Mock(running=False)
        app.auxiliary_loop_test = mock.Mock(running=False)
        app.sequential_loop_test = mock.Mock(running=False)
        app.sequential_loop_test.start.return_value = True
        app.pairwise_resistance_test = mock.Mock(running=False)
        app.router = mock.Mock(running=True)
        app.target_id = mock.Mock()
        app.target_id.get.return_value = "ESP2"
        app.root = mock.Mock()
        app.batch_calibration_button = mock.Mock()
        app.auxiliary_test_button = mock.Mock()
        app.sequential_test_button = mock.Mock()
        app.pairwise_test_button = mock.Mock()
        app.sequential_test_status = mock.Mock()
        app._append_log = mock.Mock()

        app._toggle_sequential_loop_test()

        app.sequential_loop_test.start.assert_called_once_with("ESP2")
        app.batch_calibration_button.state.assert_called_once_with(["disabled"])
        app.auxiliary_test_button.state.assert_called_once_with(["disabled"])
        app.sequential_test_button.configure.assert_called_once_with(
            text="停止顺序测试"
        )
        app.sequential_test_status.set.assert_called_once_with(
            "Y0顺序测试：正在准备"
        )


if __name__ == "__main__":
    unittest.main()
