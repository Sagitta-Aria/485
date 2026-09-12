"""Post-scan point verification must preserve evidence and use a fresh session."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from cable_tester.analysis.topology_scan import TopologyScanController
from test_topology_scan import FakeMasters
from test_topology_transfer import CachedMasters


class TopologyRecheckTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.events = []
        self.cancel_recheck = False
        self.fake = FakeMasters()
        self.controller = TopologyScanController(self.fake.send, self.publish,
            report_root=Path(self.directory.name), response_timeout_seconds=0.05,
            stream_timeout_seconds=0.1)
        self.fake.controller = self.controller

    def publish(self, kind, payload):
        self.events.append((kind, payload))
        if self.cancel_recheck and kind == "topology_progress" and payload.get("phase") == "recheck":
            self.controller.cancel()

    def run_scan(self, **parameters):
        self.events.clear()
        self.assertTrue(self.controller.start(**parameters))
        worker = self.controller._thread
        if worker is not None:
            worker.join(8)
        if self.controller.running:
            self.controller.cancel()
            worker.join(2)
        self.assertFalse(self.controller.running)
        terminal = [(kind, data) for kind, data in self.events
                    if kind in {"topology_complete", "topology_stopped", "topology_error"}]
        self.assertEqual(len(terminal), 1)
        kind, result = terminal[0]
        return kind, result, json.loads(Path(result["json"]).read_text(encoding="utf-8"))

    def baseline(self, **parameters):
        return self.run_scan(left_master="LEFT", right_master="RIGHT", left_modules=1,
                             right_modules=1, settle_seconds=0, **parameters)

    def test_normal_scan_has_no_pending_recheck(self):
        _, result, _ = self.baseline()
        self.assertEqual(result["pending_recheck_count"], 0)
        self.assertIsNone(result["recheck_parameters"])

    def test_recheck_uses_only_points_and_preserves_original_report_and_good_rows(self):
        self.fake.point_fault = (0, 0)
        _, original, original_report = self.baseline()
        self.assertEqual(original["pending_recheck_count"], 24)
        original_bytes = Path(original["json"]).read_bytes()
        self.fake.point_fault = None
        self.fake.commands.clear()
        kind, result, report = self.run_scan(**original["recheck_parameters"])
        self.assertEqual(kind, "topology_complete")
        self.assertEqual(result["measurements"], 24)
        self.assertEqual(result["rows"][0]["status"], "UNIQUE")
        self.assertEqual(result["rows"][0]["targets"], ["slave1-G1"])
        self.assertEqual(result["rows"][0]["candidates"], [])
        self.assertEqual(result["rows"][1:], original["rows"][1:])
        self.assertEqual(result["pending_recheck_count"], 0)
        self.assertNotEqual(report["session_id"], original_report["session_id"])
        self.assertEqual(report["recheck"]["parent_session_id"], original_report["session_id"])
        self.assertEqual(report["recheck"]["parent_report"], original["json"])
        self.assertEqual(report["recheck"]["completed_pairs"], 24)
        self.assertEqual(report["code_samples"], original_report["code_samples"])
        self.assertEqual(len(report["recheck_samples"]), 24)
        self.assertEqual(Path(original["json"]).read_bytes(), original_bytes)
        commands = [command.split() for _, command in self.fake.commands]
        self.assertFalse(any(command[0].startswith("TOPO_RUN") for command in commands))
        points = [command for command in commands if command[0] == "TOPO_POINT"]
        self.assertEqual([(int(command[4]), int(command[5])) for command in points], [(0, port) for port in range(24)])
        with Path(result["csv"]).open(encoding="utf-8-sig", newline="") as stream:
            row = next(csv.DictReader(stream))
        self.assertEqual(row["slave1-G1"], "1")
        self.assertEqual(row["slave1-G24"], "0")

    def test_full_row_finds_targets_excluded_by_code_and_retains_conflict(self):
        self.fake.point_fault = (0, 0)
        _, original, _ = self.baseline()
        self.fake.point_fault = None
        self.fake.graph[0] = {0, 5, 12, 23}
        _, result, report = self.run_scan(**original["recheck_parameters"])
        row = result["rows"][0]
        self.assertEqual(row["targets"], ["slave1-G1", "slave1-G6", "slave1-G13", "slave1-G24"])
        self.assertEqual(row["status"], "INCONSISTENT")
        self.assertTrue(row["point_recheck_complete"])
        self.assertEqual(row["candidates"], [])
        self.assertEqual(result["pending_recheck_count"], 0)
        self.assertEqual(report["point_rechecked_sources"], [0])

    def test_still_unknown_point_can_be_rechecked_without_repeating_good_points(self):
        self.fake.point_fault = (0, 0)
        _, original, _ = self.baseline()
        _, uncertain, _ = self.run_scan(**original["recheck_parameters"])
        self.assertEqual(uncertain["pending_recheck_count"], 1)
        self.assertEqual(uncertain["rows"][0]["candidates"], ["slave1-G1"])
        self.fake.point_fault = None
        self.fake.commands.clear()
        _, result, report = self.run_scan(**uncertain["recheck_parameters"])
        self.assertEqual(result["measurements"], 1)
        self.assertEqual(result["rows"][0]["status"], "UNIQUE")
        self.assertEqual(report["recheck"]["planned_pairs"], 1)
        self.assertEqual(sum(command.startswith("TOPO_POINT ") for _, command in self.fake.commands), 1)

    def test_cancel_keeps_unmeasured_points_unknown_and_resumes_only_remaining_pairs(self):
        self.fake.point_fault = (0, 0)
        _, original, _ = self.baseline()
        self.fake.point_fault = None
        self.cancel_recheck = True
        kind, partial, report = self.run_scan(**original["recheck_parameters"])
        self.assertEqual(kind, "topology_stopped")
        self.assertEqual(partial["measurements"], 1)
        self.assertEqual(partial["pending_recheck_count"], 23)
        self.assertEqual(partial["rows"][0]["status"], "UNKNOWN")
        self.assertEqual(partial["rows"][0]["targets"], ["slave1-G1"])
        self.assertEqual(partial["rows"][1:], original["rows"][1:])
        self.assertEqual(report["recheck"]["completed_pairs"], 1)
        self.cancel_recheck = False
        _, result, _ = self.run_scan(**partial["recheck_parameters"])
        self.assertEqual(result["measurements"], 23)
        self.assertEqual(result["pending_recheck_count"], 0)

    def test_stale_or_changed_configuration_is_rejected_before_hardware(self):
        self.fake.point_fault = (0, 0)
        _, original, _ = self.baseline()
        changed = dict(original["recheck_parameters"], right_modules=2)
        before = len(self.fake.commands)
        with self.assertRaises(ValueError):
            self.controller.start(**changed)
        self.assertEqual(len(self.fake.commands), before)
        self.baseline()
        before = len(self.fake.commands)
        with self.assertRaises(ValueError):
            self.controller.start(**original["recheck_parameters"])
        self.assertEqual(len(self.fake.commands), before)

    def test_recheck_requires_a_saved_scan(self):
        with self.assertRaises(ValueError):
            self.controller.start("LEFT", "RIGHT", 1, 1, recheck_session_id=123)
        self.assertEqual(self.fake.commands, [])

    def test_asymmetric_recheck_visits_all_ports_on_second_module(self):
        self.fake.point_fault = (0, 24)
        self.fake.graph[0] = {24}
        _, original, _ = self.run_scan(left_master="LEFT", right_master="RIGHT",
            left_modules=1, right_modules=2, settle_seconds=0)
        self.assertEqual(original["pending_recheck_count"], 48)
        self.fake.point_fault = None
        _, result, _ = self.run_scan(**original["recheck_parameters"])
        self.assertEqual(result["measurements"], 48)
        self.assertEqual(result["rows"][0]["targets"], ["slave2-G1"])

    def test_same_right_port_is_rechecked_separately_for_each_uncertain_source(self):
        self.fake.point_readings = {(0, 0): "ERR MEASURE TIMEOUT", (1, 1): "ERR MEASURE TIMEOUT"}
        _, original, _ = self.baseline()
        self.assertEqual(original["pending_recheck_count"], 48)
        self.fake.point_readings.clear()
        self.fake.commands.clear()
        _, result, _ = self.run_scan(**original["recheck_parameters"])
        self.assertEqual(result["measurements"], 48)
        points = [command.split() for _, command in self.fake.commands if command.startswith("TOPO_POINT ")]
        self.assertEqual({(int(command[4]), int(command[5])) for command in points},
                         {(source, target) for source in (0, 1) for target in range(24)})
        self.assertEqual(result["rows"][0]["targets"], ["slave1-G1"])
        self.assertEqual(result["rows"][1]["targets"], ["slave1-G2"])

    def test_cached_recheck_preserves_commit_before_ack_across_loss(self):
        self.fake = CachedMasters()
        self.fake.graph = {source: {source} for source in range(24)}
        self.fake.binary_reading = lambda source, first, end, raw: "ERR MEASURE TIMEOUT" if (source, first) == (0, 0) else raw
        self.fake.controller = self.controller
        self.controller._send_request = self.fake.send
        _, original, _ = self.baseline()
        self.fake.binary_reading = None
        self.fake.disconnect = self.fake.lost_job = self.fake.lost_ack = True
        self.fake.damage = "gap"
        self.fake.fetches = 0
        _, result, report = self.run_scan(**original["recheck_parameters"])
        self.assertIsNone(result["error"])
        self.assertEqual(result["measurements"], 24)
        self.assertEqual(result["rows"][0]["status"], "UNIQUE")
        self.assertEqual(len(self.fake.jobs), 24)
        self.assertEqual(self.fake.acked, 48)
        self.assertEqual(report["durable_sequence"], 48)
        self.assertNotEqual(original["durable_session"], result["durable_session"])


if __name__ == "__main__":
    unittest.main()
