from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from cable_tester.analysis import four_wire_loop_test as loop


class FourWireLoopPureTests(unittest.TestCase):
    def test_plan_contains_24_two_wire_groups(self) -> None:
        groups = loop.build_group_plan()

        self.assertEqual(len(groups), 24)
        self.assertEqual(groups[0].ports, ("S1_X0", "S1_X1"))
        self.assertEqual(groups[11].ports, ("S1_X22", "S1_X23"))
        self.assertEqual(groups[12].ports, ("S2_X0", "S2_X1"))
        self.assertEqual(groups[-1].ports, ("S2_X22", "S2_X23"))

    def test_directed_plan_has_24_times_23_directions(self) -> None:
        plan = loop.build_directed_plan()

        self.assertEqual(len(plan), 24 * 23)
        self.assertNotEqual(plan[0].source, plan[0].destination)
        self.assertEqual(plan[0].source, plan[23].destination)
        self.assertEqual(plan[0].destination, plan[23].source)
        self.assertEqual(
            plan[0].switch_commands,
            (
                "SWITCH S1 1 Y2 ON",
                "SWITCH S1 3 Y1 ON",
                "SWITCH S1 2 Y0 ON",
                "SWITCH S1 0 Y3 ON",
            ),
        )

    def test_first_success_is_single_measurement(self) -> None:
        spec = loop.build_directed_plan()[:1][0]
        result = loop.build_result(
            spec,
            spec.switch_commands,
            "OK MEASURE resistance=1.250 raw=1250 range=0",
        )

        self.assertEqual(result.resistance_ohm, 1.25)
        self.assertEqual(result.status, "成功")
        self.assertEqual(result.measurement_count, 1)
        self.assertFalse(result.averaged_recheck)
        self.assertEqual(result.initial_classification, "normal")

    def test_first_error_averages_two_rechecks_and_marks_error_class(self) -> None:
        spec = loop.build_directed_plan()[:1][0]
        result = loop.build_result(
            spec,
            spec.switch_commands,
            "ERR MEASURE TIMEOUT received=0",
            (
                "OK MEASURE resistance=1.000 raw=1000 range=0",
                "OK MEASURE resistance=3.000 raw=3000 range=0",
            ),
        )

        self.assertEqual(result.resistance_ohm, 2.0)
        self.assertEqual(result.status, "初测错误，两次复测平均")
        self.assertEqual(result.measurement_count, 3)
        self.assertTrue(result.averaged_recheck)
        self.assertEqual(result.initial_classification, "error")

    def test_recheck_error_keeps_final_value_as_err(self) -> None:
        spec = loop.build_directed_plan()[:1][0]
        result = loop.build_result(
            spec,
            spec.switch_commands,
            "ERR MEASURE TIMEOUT received=0",
            (
                "OK MEASURE resistance=1.000 raw=1000 range=0",
                "ERR MEASURE OVERRANGE status=1",
            ),
        )

        self.assertIsNone(result.resistance_ohm)
        self.assertEqual(result.status, "复测错误")
        self.assertEqual(result.measurement_count, 3)


class FourWireLoopControllerTests(unittest.TestCase):
    def test_controller_runs_each_direction_and_exports_results(self) -> None:
        groups = loop.build_group_plan()[:2]
        specs = loop.build_directed_plan(groups)
        commands: list[str] = []
        events: list[tuple[str, object]] = []
        sessions: list[loop.FourWireSession] = []
        responses = iter(
            [
                "OK MEASURE resistance=1.000 raw=1000 range=0",
                "ERR MEASURE TIMEOUT received=0",
                "OK MEASURE resistance=2.000 raw=2000 range=0",
                "OK MEASURE resistance=4.000 raw=4000 range=0",
            ]
        )
        controller: loop.FourWireLoopTestController

        def send_request(target_id: str, request_id: str, command: str) -> str:
            commands.append(command)
            if command == "RESET":
                payload = "OK RESET"
            elif command.startswith("SWITCH "):
                payload = f"OK {command}"
            elif command == "MEASURE":
                payload = next(responses)
            else:
                self.fail(f"unexpected command: {command}")
            self.assertTrue(controller.feed_result(target_id, request_id, payload))
            return f"OK FORWARDED {target_id} {request_id}"

        def write_report(
            session: loop.FourWireSession, report_root: Path
        ) -> Path:
            sessions.append(session)
            return report_root / "report.xlsx"

        with tempfile.TemporaryDirectory() as directory:
            controller = loop.FourWireLoopTestController(
                send_request,
                lambda event_type, message: events.append((event_type, message)),
                groups=groups,
                specs=specs,
                report_root=Path(directory),
                settle_seconds=0.0,
                response_timeout_seconds=0.1,
                report_writer=write_report,
            )
            self.assertTrue(controller.start("ESP1"))
            deadline = time.monotonic() + 1.0
            while controller.running and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertFalse(controller.running)
        self.assertEqual(len(sessions), 1)
        session = sessions[0]
        self.assertEqual(session.total_directions, 2)
        self.assertEqual(len(session.results), 2)
        self.assertEqual(session.results[0].resistance_ohm, 1.0)
        self.assertEqual(session.results[1].resistance_ohm, 3.0)
        self.assertEqual(session.results[1].measurement_count, 3)
        self.assertEqual(commands.count("MEASURE"), 4)
        self.assertEqual([event for event, _ in events if event == "four_wire_complete"], ["four_wire_complete"])


if __name__ == "__main__":
    unittest.main()
