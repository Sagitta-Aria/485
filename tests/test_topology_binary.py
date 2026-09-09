from __future__ import annotations

import itertools
import csv
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from topology_binary import BinaryRowSearch
import topology_scan as topology
from test_topology_transfer import CachedMasters


class BinarySearchTests(unittest.TestCase):
    def search(self, count, targets, fault=None):
        calls = []
        def probe(first, end):
            calls.append((first, end))
            value = int(any(first <= port < end for port in targets))
            return fault(first, end, len(calls), value) if fault else value
        search = BinaryRowSearch(count, probe)
        search.run()
        return search, calls

    def test_all_four_way_connections_on_24_ports(self):
        for targets in itertools.combinations(range(24), 4):
            search, _ = self.search(24, targets)
            self.assertEqual(search.targets, targets)
            self.assertEqual(search.status, "SHORT")

    def test_unknown_branch_count_and_module_boundaries(self):
        for count in (24, 48, 168, 240):
            for targets in ((), (0,), (count - 1,), (0, 11, 12, 23), tuple(range(count))):
                search, _ = self.search(count, targets)
                self.assertEqual(search.targets, targets)
                self.assertTrue(search.complete)

    def test_missed_root_group_is_caught_by_complement(self):
        search, calls = self.search(24, (0, 5, 12, 23), lambda first, end, n, value: 0 if n == 1 else value)
        self.assertEqual(search.targets, (0, 5, 12, 23))
        self.assertEqual(search.status, "INCONSISTENT")
        self.assertEqual(calls[:2], [(0, 24), (0, 24)])

    def test_missed_half_does_not_disappear_after_other_half_found(self):
        search, _ = self.search(24, (0, 5, 12, 23), lambda first, end, n, value: 0 if n == 2 else value)
        self.assertEqual(search.targets, (0, 5, 12, 23))
        self.assertEqual(search.status, "INCONSISTENT")

    def test_positive_group_without_positive_points_never_invents_connection(self):
        search, _ = self.search(24, (), lambda first, end, n, value: 1 if end-first > 1 else 0)
        self.assertEqual(search.targets, ())
        self.assertEqual(search.status, "INCONSISTENT")

    def test_unstable_leaf_never_becomes_confirmed_or_open(self):
        search, _ = self.search(24, (0, 1, 2, 3), lambda first, end, n, value: None if first == 2 and end == 3 else value)
        self.assertEqual(search.status, "UNKNOWN")
        self.assertNotIn(2, search.targets)
        self.assertIsNone(search.decisions[2])

    def test_interrupted_probe_leaves_partial_row_unknown(self):
        def probe(first, end):
            if first == 0 and end == 1:
                raise InterruptedError("cancelled")
            return 1
        search = BinaryRowSearch(24, probe)
        with self.assertRaises(InterruptedError):
            search.run()
        self.assertFalse(search.complete)
        self.assertEqual(search.status, "UNKNOWN")


class BinaryReadingTests(unittest.TestCase):
    def test_single_read_keeps_original_error_diagnostic(self):
        reading = topology.classify_measurement("ERR MEASURE TIMEOUT")
        self.assertIs(topology.confirm_binary_readings([reading]), reading)
        self.assertIsNone(reading.bit)
        self.assertEqual(reading.reason, "MEASUREMENT_ERROR")

    def test_two_disagreeing_reads_are_unknown(self):
        readings = [topology.classify_measurement(raw) for raw in (
            "OK MEASURE resistance=1.000 raw=1000 range=2", "ERR MEASURE OVERRANGE")]
        confirmed = topology.confirm_binary_readings(readings)
        self.assertIsNone(confirmed.bit)
        self.assertEqual(confirmed.reason, "UNSTABLE_READING")

    def test_even_repeat_count_keeps_an_actual_median_sample(self):
        readings = [topology.classify_measurement(f"OK MEASURE resistance={value}.000 raw={value * 1000} range=2")
                    for value in (4, 1, 3, 2)]
        self.assertIs(topology.confirm_binary_readings(readings), readings[2])

    def test_empty_and_excessive_readings_are_rejected(self):
        reading = topology.classify_measurement("ERR MEASURE OVERRANGE")
        for count in (0, topology.MAX_BINARY_REPEATS + 1):
            with self.subTest(count=count), self.assertRaises(ValueError):
                topology.confirm_binary_readings([reading] * count)


class BinaryControllerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.fake = CachedMasters()
        self.fake.binary = True
        self.fake.graph = {0: {0, 5, 12, 23}}
        self.events = []
        self.cancel_on_sample = False
        def publish(event, payload):
            self.events.append((event, payload))
            if self.cancel_on_sample and event == "topology_progress":
                self.controller.cancel()
        self.controller = topology.TopologyScanController(self.fake.send, publish, report_root=Path(self.directory.name),
                                                         response_timeout_seconds=0.1, stream_timeout_seconds=0.5)
        self.fake.controller = self.controller

    def run_scan(self, right_modules=1, binary_repeats=None):
        self.events.clear()
        options = {} if binary_repeats is None else {"binary_repeats": binary_repeats}
        self.assertTrue(self.controller.start("LEFT", "RIGHT", 1, right_modules, settle_seconds=0, scan_method="binary", **options))
        worker = self.controller._thread
        worker.join(timeout=15)
        if worker.is_alive():
            self.controller.cancel()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        terminal = [(kind, data) for kind, data in self.events if kind in {"topology_complete", "topology_stopped", "topology_error"}]
        self.assertEqual(len(terminal), 1)
        kind, result = terminal[0]
        report = json.loads(Path(result["json"]).read_text(encoding="utf-8"))
        return kind, result, report

    def test_complete_four_way_scan_uses_points_and_preserves_raw_trials(self):
        _, result, report = self.run_scan()
        self.assertIsNone(result["error"])
        self.assertEqual(result["completed"], 24)
        row = result["rows"][0]
        self.assertEqual(row["status"], "SHORT")
        self.assertEqual(row["targets"], ["slave1-G1", "slave1-G6", "slave1-G13", "slave1-G24"])
        self.assertEqual(len(row["connection_resistances"]), 4)
        self.assertTrue(all(item["resistance_ohm"] == 1 for item in row["connection_resistances"]))
        self.assertEqual(report["scan_method"], "binary")
        self.assertEqual(report["binary_confirmation_repeats"], 1)
        self.assertEqual({item["attempt"] for item in report["binary_samples"]}, {1})
        self.assertEqual(report["code_samples"], [])
        self.assertEqual(report["codebook"], [])
        self.assertEqual(result["measurements"], len(report["binary_samples"]))
        self.assertEqual(self.fake.acked, 2 * result["measurements"])
        self.assertFalse(any(command.startswith("TOPO_RUN") for _, command in self.fake.commands))
        self.assertEqual([row["status"] for row in result["rows"]][1:], ["NO_CONTINUITY"] * 23)
        with Path(result["csv"]).open(encoding="utf-8-sig", newline="") as stream:
            row = next(csv.DictReader(stream))
        self.assertEqual(row["scan_method"], "binary")
        self.assertEqual(row["binary_confirmation_repeats"], "1")
        self.assertEqual([row[f"slave1-G{port}"] for port in (1, 6, 13, 24)], ["1"] * 4)
        self.assertGreater(len(json.loads(row["binary_samples_raw"])), 3)

    def test_selected_repeats_scale_jobs_and_raw_readings_without_changing_wiring(self):
        _, baseline, _ = self.run_scan()
        for repeats in (2, 3, 5, topology.MAX_BINARY_REPEATS):
            with self.subTest(repeats=repeats):
                _, result, report = self.run_scan(binary_repeats=repeats)
                self.assertIsNone(result["error"])
                self.assertEqual(result["measurements"], baseline["measurements"] * repeats)
                self.assertEqual(len(self.fake.jobs), result["measurements"])
                self.assertEqual(result["rows"], baseline["rows"])
                self.assertEqual(result["binary_confirmation_repeats"], repeats)
                self.assertEqual(report["binary_confirmation_repeats"], repeats)
                samples = report["binary_samples"]
                for first in range(0, len(samples), repeats):
                    batch = samples[first:first + repeats]
                    self.assertEqual([item["attempt"] for item in batch], list(range(1, repeats + 1)))
                    self.assertEqual(len({(item["source"], item["first"], item["end"]) for item in batch}), 1)
                with closing(sqlite3.connect(result["durable_session"])) as reader:
                    configuration = json.loads(reader.execute("SELECT value FROM metadata WHERE name='configuration'").fetchone()[0])
                self.assertEqual(configuration["binary_confirmation_repeats"], repeats)
                with Path(result["csv"]).open(encoding="utf-8-sig", newline="") as stream:
                    self.assertEqual(next(csv.DictReader(stream))["binary_confirmation_repeats"], str(repeats))

    def test_multibranch_crosses_module_boundary_without_expected_wiring(self):
        self.fake.graph = {0: {11, 12, 23, 24, 47}, 1: {11, 47}}
        _, result, _ = self.run_scan(2)
        self.assertIsNone(result["error"])
        self.assertEqual(result["rows"][0]["targets"], ["slave1-G12", "slave1-G13", "slave1-G24", "slave2-G1", "slave2-G24"])
        self.assertEqual(result["rows"][1]["targets"], ["slave1-G12", "slave2-G24"])

    def test_single_false_negative_group_cannot_prune_four_branches(self):
        calls = 0
        def reading(source, first, end, raw):
            nonlocal calls
            calls += 1
            return "ERR MEASURE OVERRANGE status=1" if calls == 1 else raw
        self.fake.binary_reading = reading
        _, result, _ = self.run_scan(binary_repeats=3)
        self.assertIsNone(result["error"])
        self.assertEqual(len(result["rows"][0]["targets"]), 4)
        self.assertEqual(result["rows"][0]["status"], "INCONSISTENT")

    def test_three_wrong_group_reads_are_detected_by_later_complement(self):
        calls = 0
        def reading(source, first, end, raw):
            nonlocal calls
            calls += 1
            return "ERR MEASURE OVERRANGE status=1" if calls <= 3 else raw
        self.fake.binary_reading = reading
        _, result, _ = self.run_scan(binary_repeats=3)
        self.assertEqual(len(result["rows"][0]["targets"]), 4)
        self.assertEqual(result["rows"][0]["status"], "INCONSISTENT")

    def test_point_disagreement_is_unknown_and_does_not_confirm_a_branch(self):
        calls = 0
        def reading(source, first, end, raw):
            nonlocal calls
            if source == 0 and first == 5 and end == 6:
                calls += 1
                if calls % 3 == 2:
                    return "ERR MEASURE OVERRANGE status=1"
            return raw
        self.fake.binary_reading = reading
        _, result, report = self.run_scan(binary_repeats=3)
        self.assertEqual(result["rows"][0]["status"], "UNKNOWN")
        self.assertNotIn("slave1-G6", result["rows"][0]["targets"])
        point = next(item for item in report["point_samples"] if item["source"] == 0 and item["destination"] == 5)
        self.assertIsNone(point["bit"])
        self.assertEqual(point["reason"], "UNSTABLE_READING")

    def test_reconnect_lost_job_ack_and_missing_data_keep_same_measurements(self):
        self.fake.lost_job = self.fake.lost_ack = self.fake.disconnect = True
        self.fake.damage = "gap"
        _, result, report = self.run_scan()
        self.assertIsNone(result["error"])
        self.assertEqual(len(result["rows"][0]["targets"]), 4)
        self.assertEqual(self.fake.acked, 2 * result["measurements"])
        self.assertEqual(len(self.fake.jobs), result["measurements"])
        self.assertEqual(report["durable_sequence"], self.fake.acked)

    def test_cancellation_preserves_partial_samples_as_unknown(self):
        self.cancel_on_sample = True
        kind, result, report = self.run_scan()
        self.assertEqual(kind, "topology_stopped")
        self.assertEqual(result["completed"], 0)
        self.assertTrue(all(row["status"] == "UNKNOWN" for row in result["rows"]))
        self.assertGreater(len(report["binary_samples"]), 0)

    def test_unsupported_firmware_does_not_silently_run_coded_scan(self):
        self.fake.binary = False
        kind, result, _ = self.run_scan()
        self.assertEqual(kind, "topology_error")
        self.assertIn("BINARY_UNSUPPORTED", result["error"])
        self.assertFalse(any(command.startswith(("TOPO_RUN", "TOPO_RANGE", "TOPO_POINT")) for _, command in self.fake.commands))

    def test_invalid_method_rejected_before_any_commands(self):
        with self.assertRaises(ValueError):
            self.controller.start("LEFT", "RIGHT", scan_method="unknown")
        self.assertEqual(self.fake.commands, [])

    def test_invalid_repeats_are_rejected_before_any_commands(self):
        for repeats in (0, -1, topology.MAX_BINARY_REPEATS + 1, 1.5, True, "3", None):
            with self.subTest(repeats=repeats), self.assertRaises(ValueError):
                self.controller.start("LEFT", "RIGHT", scan_method="binary", binary_repeats=repeats)
        self.assertEqual(self.fake.commands, [])


if __name__ == "__main__":
    unittest.main()
