from __future__ import annotations

import csv
import itertools
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from cable_tester.analysis import topology_scan as topology


def observed_bits(codebook: topology.Codebook, targets: set[topology.TopologyPort]) -> list[int]:
    union = 0
    for port, code in zip(codebook.ports, codebook.codes):
        if port in targets:
            union |= code
    return [(union >> index) & 1 for index in range(codebook.rounds)]


def graph_reachability(
    left_count: int, right_count: int, edges: list[tuple[str, str]]
) -> dict[int, set[int]]:
    adjacency: dict[str, set[str]] = {}
    for first, second in edges:
        adjacency.setdefault(first, set()).add(second)
        adjacency.setdefault(second, set()).add(first)
    rows = {}
    for source in range(left_count):
        seen, pending = set(), [f"L{source}"]
        while pending:
            current = pending.pop()
            if current not in seen:
                seen.add(current)
                pending.extend(adjacency.get(current, ()))
        rows[source] = {target for target in range(right_count) if f"R{target}" in seen}
    return rows


class TopologyAlgorithmTests(unittest.TestCase):
    def test_adjacent_pairs_preserve_two_bank_kelvin_mapping(self) -> None:
        first = topology.TopologyPort(3, 0)
        boundary = topology.TopologyPort(3, 12)
        final = topology.TopologyPort(3, 23)
        self.assertEqual((first.global_port, first.bank, first.current_x, first.voltage_x), (72, "S1", 0, 1))
        self.assertEqual((boundary.bank, boundary.current_x, boundary.voltage_x), ("S2", 0, 1))
        self.assertEqual((final.bank, final.current_x, final.voltage_x), ("S2", 22, 23))
        with self.assertRaises(ValueError):
            topology.build_ports([1, 1])
        with self.assertRaises(ValueError):
            topology.TopologyPort(0, 24)
        with self.assertRaises(ValueError):
            topology.TopologyPort(10, 0)
        with self.assertRaises(ValueError):
            topology.TopologyPort(0.5, 0)

    def test_code_size_scales_with_actual_destination_count(self) -> None:
        for count, expected in ((1, (1, 1)), (24, (7, 3)), (48, (8, 3)), (72, (9, 3)), (168, (10, 4)), (169, (10, 4)), (240, (10, 5))):
            with self.subTest(count=count):
                ports = topology.build_ports(range((count + 23) // 24))[:count]
                book = topology.Codebook.create(ports)
                self.assertEqual((book.rounds, book.weight), expected)
                self.assertEqual(len(set(book.codes)), count)
                self.assertTrue(all(code.bit_count() == book.weight for code in book.codes))

    def test_masks_include_zero_modules_and_use_local_address_bits(self) -> None:
        ports = topology.build_ports([3, 8])
        book = topology.Codebook.create(ports)
        for index in range(book.rounds):
            masks = book.masks_for_round(index)
            self.assertEqual(set(masks), {3, 8})
            self.assertTrue(all(0 <= mask <= 0xFFFFFF for mask in masks.values()))
            for port, code in zip(ports, book.codes):
                self.assertEqual(bool(masks[port.module_id] & (1 << port.local_port)), bool(code & (1 << index)))

    def test_all_two_port_shorts_are_detected_and_candidates_resolve_exactly(self) -> None:
        book = topology.Codebook.create(topology.build_ports([0]))
        for pair in itertools.combinations(book.ports, 2):
            targets = set(pair)
            bits = observed_bits(book, targets)
            decoded = topology.decode_signature(book, bits)
            self.assertEqual(decoded.status, "SHORT_CANDIDATES")
            self.assertTrue(targets.issubset(decoded.candidates))
            resolved = topology.resolve_row(book, bits, {port: int(port in targets) for port in decoded.candidates})
            self.assertEqual(resolved.status, "SHORT")
            self.assertEqual(set(resolved.targets), targets)

    def test_rectangular_connected_components_include_indirect_connections(self) -> None:
        book = topology.Codebook.create(topology.build_ports([0, 1]))
        graph = graph_reachability(24, 48, [("L0", "R4"), ("L1", "R7"), ("R4", "R7"), ("L2", "R47")])
        rows = {}
        for source in topology.build_ports([0]):
            targets = {book.ports[index] for index in graph[source.global_port]}
            bits = observed_bits(book, targets)
            decoded = topology.decode_signature(book, bits)
            if decoded.status == "SHORT_CANDIDATES":
                decoded = topology.resolve_row(book, bits, {candidate: int(candidate in targets) for candidate in decoded.candidates})
            rows[source] = decoded
            self.assertEqual(set(decoded.targets), targets)
        findings = topology.assess_rows(rows)
        self.assertIn("DUPLICATE_TARGET", findings[topology.TopologyPort(0, 0)])
        self.assertIn("DUPLICATE_TARGET", findings[topology.TopologyPort(0, 1)])
        self.assertEqual(rows[topology.TopologyPort(0, 3)].status, "NO_CONTINUITY")

    def test_unknown_round_never_passes_without_point_evidence(self) -> None:
        book = topology.Codebook.create(topology.build_ports([0]))
        bits: list[int | None] = observed_bits(book, {book.ports[0]})
        bits[-1] = None
        self.assertEqual(topology.decode_signature(book, bits).status, "UNKNOWN")
        self.assertEqual(topology.resolve_row(book, bits, {book.ports[0]: 1}).status, "UNKNOWN")
        resolved = topology.resolve_row(book, bits, {port: int(port == book.ports[0]) for port in book.ports})
        self.assertEqual(resolved.status, "UNIQUE")
        self.assertEqual(resolved.targets, (book.ports[0],))

    def test_inconsistent_point_evidence_never_overrides_known_group_bits(self) -> None:
        book = topology.Codebook.create(topology.build_ports([0]))
        bits = observed_bits(book, {book.ports[0], book.ports[1]})
        candidates = topology.decode_signature(book, bits).candidates
        resolved = topology.resolve_row(book, bits, {port: 0 for port in candidates})
        self.assertEqual(resolved.status, "INCONSISTENT")

    def test_expected_mapping_and_duplicate_targets_are_separate_flags(self) -> None:
        sources = topology.build_ports([0])[:2]
        book = topology.Codebook.create(topology.build_ports([1]))
        decoded = topology.decode_signature(book, observed_bits(book, {book.ports[1]}))
        rows = {source: decoded for source in sources}
        findings = topology.assess_rows(rows, {sources[0]: book.ports[0], sources[1]: book.ports[1]})
        self.assertEqual(findings[sources[0]], ("DUPLICATE_TARGET", "MISWIRE"))
        self.assertEqual(findings[sources[1]], ("DUPLICATE_TARGET",))

    def test_duplicate_target_flag_does_not_turn_unknown_row_into_normal(self) -> None:
        sources = topology.build_ports([0])[:2]
        book = topology.Codebook.create(topology.build_ports([1]))
        unknown = topology.resolve_row(book, [None] * book.rounds, {book.ports[0]: 1})
        unique = topology.decode_signature(book, observed_bits(book, {book.ports[0]}))
        rows = {sources[0]: unknown, sources[1]: unique}
        findings = topology.assess_rows(rows, {source: book.ports[0] for source in sources})
        self.assertEqual(rows[sources[0]].status, "UNKNOWN")
        self.assertEqual(findings[sources[0]], ("DUPLICATE_TARGET",))

    def test_measurement_faults_and_hysteresis_band_stay_unknown(self) -> None:
        cases = {
            "OK MEASURE resistance=50.000 raw=500 range=0": 1,
            "OK MEASURE resistance=75.000 raw=750 range=0": None,
            "OK MEASURE resistance=100.000 raw=1000 range=0": 0,
            "ERR MEASURE OVERRANGE status=1 frame=0103": 0,
            "ERR MEASURE TIMEOUT received=0": None,
            "ERR MEASURE CRC expected=123 received=456": None,
            "OK MEASURE resistance=0.100 raw=100 range=3": None,
            "OK MEASURE resistance=nan raw=100 range=2": None,
        }
        for payload, expected in cases.items():
            with self.subTest(payload=payload):
                measurement = topology.classify_measurement(payload)
                self.assertEqual(measurement.bit, expected)
                self.assertEqual(measurement.raw, payload)
        with self.assertRaises(ValueError):
            topology.classify_measurement("", 100, 50)


class FakeMasters:
    def __init__(self) -> None:
        self.controller: topology.TopologyScanController
        self.commands: list[tuple[str, str]] = []
        self.masks: dict[tuple[int, int], int] = {}
        self.graph = {source: {source} for source in range(24)}
        self.group_fault: tuple[int, int] | None = None
        self.point_fault: tuple[int, int] | None = None
        self.point_readings: dict[tuple[int, int], str] = {}
        self.bus_ready = True
        self.missing_slave = False
        self.truncate_stream = False
        self.stale_session = False
        self.fail_reset = False
        self.silent_command: str | None = None
        self.info_override: str | None = None
        self.round_major = True
        self.bad_coordinates: tuple[int, int] | None = None
        self.disconnect_peer: str | None = None
        self.reconnect_peer = True
        self.stream_request: str | None = None
        self.stream_session = ""

    def measurement(self, connected: bool) -> str:
        return "OK MEASURE resistance=0.100 raw=100 range=2" if connected else "ERR MEASURE OVERRANGE status=1 frame=0103"

    def send(self, target: str, request_id: str, command: str) -> str:
        self.commands.append((target, command))
        fields = command.split()
        operation = fields[0]
        responses = []
        if operation == self.silent_command:
            return f"OK FORWARDED {target} {request_id}"
        if operation == "TOPO_INFO":
            route = int(target == "LEFT")
            responses = [self.info_override or f"OK TOPO_INFO role=MASTER capacity=10 configured=7 bus={int(self.bus_ready)} route={route}"]
        elif operation == "TOPO_DISCOVER":
            count = int(fields[1])
            online = 0 if self.missing_slave else (1 << count) - 1
            responses = [f"OK TOPO_DISCOVER count={count} online={online:08x}"]
        elif operation == "TOPO_MASK":
            self.masks[(int(fields[2]), int(fields[3]))] = int(fields[4], 16)
            responses = ["OK TOPO_MASK"]
        elif operation == "TOPO_RUN":
            sid, _, count, rounds, _ = fields[1:]
            self.stream_request, self.stream_session = request_id, sid
            responses = ["OK TOPO_RUN"]
            coordinates = [(source, index) for source in range(int(count) * 24) for index in range(int(rounds))]
            if self.round_major:
                coordinates.sort(key=lambda coordinate: (coordinate[1], coordinate[0]))
            if self.bad_coordinates is not None:
                coordinates[1] = self.bad_coordinates
            for source, index in coordinates:
                connected = any(self.masks.get((index, destination // 24), 0) & (1 << (destination % 24)) for destination in self.graph.get(source, ()))
                measurement = "ERR MEASURE CRC expected=123 received=456" if self.group_fault == (source, index) else self.measurement(connected)
                sample_sid = "0" if self.stale_session else sid
                responses.append(f"TOPO_SAMPLE {sample_sid} {source} {index} {measurement}")
            if self.truncate_stream:
                responses.pop()
            responses.append(f"TOPO_DONE {sid} {int(count) * 24 * int(rounds)}")
        elif operation == "TOPO_POINT":
            sid, _, _, source, destination, _ = fields[1:]
            key = int(source), int(destination)
            measurement = "ERR MEASURE TIMEOUT received=0" if self.point_fault == key else self.measurement(int(destination) in self.graph.get(int(source), ()))
            measurement = self.point_readings.get(key, measurement)
            responses = ["OK TOPO_POINT", f"TOPO_POINT_SAMPLE {sid} {source} {destination} {measurement}", f"TOPO_DONE {sid} 1"]
        else:
            responses = ["ERR TOPO_RESET BUS_FAILURE" if operation == "TOPO_RESET" and self.fail_reset else f"OK {operation}"]
        delivered_samples = 0
        for response in responses:
            if not self.controller.feed_result(target, request_id, response):
                raise AssertionError("controller dropped a correlated stream frame")
            if operation == "TOPO_RUN" and response.startswith("TOPO_SAMPLE "):
                delivered_samples += 1
                if self.disconnect_peer is not None and delivered_samples == 23:
                    # Reconnection invalidates the firmware job, so no terminal frame follows.
                    observe = getattr(self.controller, "feed_transport", lambda event: None)
                    observe({"event": "disconnected", "peer_id": self.disconnect_peer,
                             "reason": "peer_closed"})
                    if self.reconnect_peer:
                        observe({"event": "connected", "peer_id": self.disconnect_peer})
                    break
        if operation == "TOPO_ABORT" and self.disconnect_peer == "RIGHT":
            self.controller.feed_result("LEFT", self.stream_request,
                                        f"TOPO_STOPPED {self.stream_session} 23")
        return f"OK FORWARDED {target} {request_id}"


class TopologyControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fake = FakeMasters()
        self.events: list[tuple[str, object]] = []
        self.cancel_on_sample = False
        self.cancel_on_resistance = False

        def publish(event: str, payload: object) -> None:
            self.events.append((event, payload))
            if self.cancel_on_sample and event == "topology_progress":
                self.controller.cancel()
            if self.cancel_on_resistance and event == "topology_progress" and payload.get("phase") == "resistance" and payload["completed"] > 168:
                self.controller.cancel()

        self.controller = topology.TopologyScanController(
            self.fake.send, publish, report_root=Path(self.temporary.name),
            response_timeout_seconds=0.05, stream_timeout_seconds=0.1,
        )
        self.fake.controller = self.controller

    def run_scan(self, left_modules: int = 1, right_modules: int = 1, **kwargs: object) -> dict[str, object]:
        self.assertTrue(self.controller.start("LEFT", "RIGHT", left_modules, right_modules, settle_seconds=0, **kwargs))
        deadline = time.monotonic() + 3
        while self.controller.running and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertFalse(self.controller.running)
        terminal = [(event, payload) for event, payload in self.events if event in {"topology_complete", "topology_stopped", "topology_error"}]
        self.assertEqual(len(terminal), 1)
        self.last_event, payload = terminal[0]
        return payload

    def test_normal_scan_has_168_code_samples_and_24_resistance_queries(self) -> None:
        result = self.run_scan()
        self.assertEqual(self.last_event, "topology_complete")
        self.assertEqual(result["measurements"], 168 + 24)
        self.assertEqual(result["completed"], 24)
        self.assertTrue(all(row["status"] == "UNIQUE" for row in result["rows"]))
        self.assertEqual(sum(command.startswith("TOPO_POINT ") for _, command in self.fake.commands), 24)
        self.assertEqual([target for target, command in self.fake.commands[-2:]], ["LEFT", "RIGHT"])
        report = json.loads(Path(result["json"]).read_text(encoding="utf-8"))
        self.assertEqual(len(report["code_samples"]), 168)
        self.assertEqual(len(report["point_samples"]), 24)
        self.assertEqual(len(report["raw_frames"]), 170 + 24 * 3)

    def test_source_major_stream_is_also_accepted(self) -> None:
        self.fake.round_major = False
        result = self.run_scan()
        self.assertEqual(self.last_event, "topology_complete")
        self.assertEqual(result["measurements"], 168 + 24)
        self.assertTrue(all(row["status"] == "UNIQUE" for row in result["rows"]))

    def test_short_adds_candidate_point_queries_and_records_duplicates(self) -> None:
        self.fake.graph[0] = {0, 1}
        self.fake.graph[1] = {0, 1}
        result = self.run_scan()
        self.assertEqual(self.last_event, "topology_complete")
        self.assertGreater(result["measurements"], 168)
        self.assertLess(result["measurements"], 168 + 48)
        for row in result["rows"][:2]:
            self.assertEqual(row["status"], "SHORT")
            self.assertEqual(row["targets"], ["slave1-G1", "slave1-G2"])
            self.assertIn("DUPLICATE_TARGET", row["flags"])

    def test_seven_modules_scan_1680_measurements_and_preserve_last_address(self) -> None:
        self.fake.graph = {source: {source} for source in range(168)}
        result = self.run_scan(7, 7)
        self.assertEqual(self.last_event, "topology_complete")
        self.assertEqual(result["measurements"], 1680 + 168)
        self.assertEqual(result["completed"], 168)
        self.assertTrue(all(row["status"] == "UNIQUE" for row in result["rows"]))
        self.assertEqual(result["rows"][-1]["targets"], ["slave7-G24"])
        self.assertEqual(len(self.fake.masks), 70)

    def test_rectangular_scan_addresses_remote_second_module(self) -> None:
        self.fake.graph = {source: {source + 24} for source in range(24)}
        result = self.run_scan(1, 2)
        self.assertEqual(self.last_event, "topology_complete")
        self.assertEqual(result["measurements"], 24 * 8 + 24)
        self.assertEqual(result["rows"][0]["targets"], ["slave2-G1"])
        self.assertEqual(result["rows"][-1]["targets"], ["slave2-G24"])

    def test_ten_modules_scan_2400_samples(self) -> None:
        self.fake.graph = {source: {source} for source in range(240)}
        result = self.run_scan(10, 10)
        self.assertEqual(self.last_event, "topology_complete")
        self.assertEqual(result["measurements"], 2400 + 240)
        self.assertEqual(result["completed"], 240)
        self.assertEqual(result["rows"][-1]["targets"], ["slave10-G24"])
        self.assertTrue(all(row["status"] == "UNIQUE" for row in result["rows"]))

    def test_seven_by_six_scan_preserves_repeated_targets(self) -> None:
        self.fake.graph = {source: {source % 144} for source in range(168)}
        result = self.run_scan(7, 6)
        self.assertEqual(self.last_event, "topology_complete")
        self.assertEqual(result["measurements"], 1680 + 168)
        self.assertEqual(result["rows"][-1]["targets"], ["slave1-G24"])
        self.assertIn("DUPLICATE_TARGET", result["rows"][-1]["flags"])
        self.assertIn("DUPLICATE_TARGET", result["rows"][23]["flags"])
        self.assertEqual(result["rows"][24]["flags"], [])

    def test_expected_mapping_is_stored_and_csv_flags_miswire(self) -> None:
        self.fake.graph[0] = {1}
        expected = {topology.TopologyPort(0, 0): topology.TopologyPort(0, 0)}
        result = self.run_scan(expected_mapping=expected)
        report = json.loads(Path(result["json"]).read_text(encoding="utf-8"))
        self.assertEqual(report["expected_mapping"], [{"source": "slave1-G1", "destination": "slave1-G1"}])
        with Path(result["csv"]).open(encoding="utf-8-sig", newline="") as source:
            table = list(csv.DictReader(source))
        self.assertIn("MISWIRE", table[0]["flags"])

    def test_group_crc_fault_requires_full_row_but_can_resolve(self) -> None:
        self.fake.group_fault = (0, 0)
        result = self.run_scan()
        self.assertEqual(result["measurements"], 168 + 24 + 23)
        self.assertEqual(result["rows"][0]["status"], "UNIQUE")
        self.assertIn("?", result["rows"][0]["signature"])

    def test_one_wire_reports_its_single_point_value_not_the_group_value(self) -> None:
        self.fake.graph = {0: {0}}
        raw = "OK MEASURE resistance=25.380 raw=2538 range=1"
        self.fake.point_readings[(0, 0)] = raw
        result = self.run_scan()
        self.assertEqual(result["measurements"], 169)
        reading = result["rows"][0]["connection_resistances"][0]
        self.assertEqual(reading, {"target": "slave1-G1", "target_global": 0, **vars(topology.classify_measurement(raw))})
        self.assertEqual(result["rows"][1]["connection_resistances"], [])
        report = json.loads(Path(result["json"]).read_text(encoding="utf-8"))
        self.assertEqual(report["rows"], result["rows"])
        self.assertEqual(report["resistance_basis"], "single_point_uncalibrated")
        self.assertEqual(report["code_samples"][0]["resistance_ohm"], 0.1)
        with Path(result["csv"]).open(encoding="utf-8-sig", newline="") as source:
            table = list(csv.DictReader(source))
        self.assertEqual(table[0]["resistance_ohm:slave1-G1"], "25.380")
        self.assertEqual(table[0]["resistance_status:slave1-G1"], "CONTINUITY")
        self.assertEqual(table[1]["resistance_ohm:slave1-G1"], "")
        self.assertEqual(table[1]["resistance_status:slave1-G1"], "NOT_MEASURED")
        live_rows = [payload for event, payload in self.events if event == "topology_row" and payload["source"] == "slave1-G1"]
        self.assertEqual(live_rows[0]["connection_resistances"][0]["reason"], "NOT_MEASURED")
        self.assertEqual(live_rows[-1]["connection_resistances"][0]["resistance_ohm"], 25.38)
        progress = [payload for event, payload in self.events if event == "topology_progress"]
        self.assertEqual((progress[-1]["phase"], progress[-1]["completed"], progress[-1]["total"]), ("resistance", 169, 169))
        self.assertTrue(all(step["completed"] <= step["total"] for step in progress))

    def test_existing_followup_reading_is_reused_without_duplicate_queries(self) -> None:
        self.fake.graph = {0: {0}}
        self.fake.group_fault = (0, 0)
        self.fake.point_readings[(0, 0)] = "OK MEASURE resistance=25.380 raw=2538 range=1"
        result = self.run_scan()
        self.assertEqual(result["measurements"], 168 + 24)
        self.assertEqual(result["rows"][0]["connection_resistances"][0]["resistance_ohm"], 25.38)
        self.assertEqual(sum(command.startswith("TOPO_POINT ") for _, command in self.fake.commands), 24)

    def test_short_preserves_distinct_resistances_for_each_target(self) -> None:
        self.fake.graph = {0: {0, 1}}
        self.fake.point_readings[(0, 0)] = "OK MEASURE resistance=1.230 raw=1230 range=2"
        self.fake.point_readings[(0, 1)] = "OK MEASURE resistance=4.560 raw=4560 range=2"
        result = self.run_scan()
        readings = result["rows"][0]["connection_resistances"]
        self.assertEqual([(item["target"], item["resistance_ohm"]) for item in readings], [("slave1-G1", 1.23), ("slave1-G2", 4.56)])
        queries = [tuple(command.split()[4:6]) for _, command in self.fake.commands if command.startswith("TOPO_POINT ")]
        self.assertEqual(len(queries), len(set(queries)))
        progress = [payload for event, payload in self.events if event == "topology_progress"]
        self.assertEqual(progress[-1]["completed"], progress[-1]["total"])

    def test_no_wires_needs_no_resistance_queries(self) -> None:
        self.fake.graph = {}
        result = self.run_scan()
        self.assertEqual(result["measurements"], 168)
        self.assertTrue(all(row["connection_resistances"] == [] for row in result["rows"]))
        self.assertFalse(any(command.startswith("TOPO_POINT ") for _, command in self.fake.commands))

    def test_failed_resistance_verification_retains_pair_and_invalid_state(self) -> None:
        cases = (
            ("ERR MEASURE TIMEOUT received=0", "UNKNOWN", None, "MEASUREMENT_ERROR"),
            ("ERR MEASURE OVERRANGE status=1", "INCONSISTENT", None, "NO_CONTINUITY_OR_OVERRANGE"),
            ("OK MEASURE resistance=75.000 raw=7500 range=1", "UNKNOWN", 75.0, "THRESHOLD_UNCERTAIN"),
            ("OK MEASURE resistance=150.000 raw=1500 range=0", "INCONSISTENT", 150.0, "HIGH_RESISTANCE"),
        )
        for raw, status, resistance, reason in cases:
            with self.subTest(raw=raw):
                self.events.clear()
                self.fake.graph = {0: {0}}
                self.fake.point_readings[(0, 0)] = raw
                result = self.run_scan()
                row = result["rows"][0]
                self.assertEqual(row["status"], status)
                self.assertEqual(row["targets"], [])
                reading = row["connection_resistances"][0]
                self.assertEqual((reading["target"], reading["resistance_ohm"], reading["reason"]), ("slave1-G1", resistance, reason))
                with Path(result["csv"]).open(encoding="utf-8-sig", newline="") as source:
                    entry = next(csv.DictReader(source))
                self.assertEqual(entry["resistance_ohm:slave1-G1"], "" if resistance is None else f"{resistance:.3f}")
                self.assertEqual(entry["resistance_status:slave1-G1"], reason)

    def test_cancel_during_resistance_keeps_readings_and_pending_pairs(self) -> None:
        self.cancel_on_resistance = True
        result = self.run_scan()
        self.assertEqual(self.last_event, "topology_stopped")
        self.assertEqual(result["measurements"], 169)
        self.assertEqual(result["rows"][0]["connection_resistances"][0]["resistance_ohm"], 0.1)
        pending = result["rows"][1]["connection_resistances"][0]
        self.assertEqual(pending["reason"], "NOT_MEASURED")
        self.assertIsNone(pending["resistance_ohm"])

    def test_zero_ohms_is_a_numeric_reading(self) -> None:
        self.fake.graph = {0: {0}}
        self.fake.point_readings[(0, 0)] = "OK MEASURE resistance=0.000 raw=0 range=2"
        result = self.run_scan()
        self.assertEqual(result["rows"][0]["connection_resistances"][0]["resistance_ohm"], 0.0)
        with Path(result["csv"]).open(encoding="utf-8-sig", newline="") as source:
            self.assertEqual(next(csv.DictReader(source))["resistance_ohm:slave1-G1"], "0.000")

    def test_point_timeout_retains_unknown_in_report_matrix(self) -> None:
        self.fake.group_fault = (0, 0)
        self.fake.point_fault = (0, 0)
        result = self.run_scan()
        self.assertEqual(result["rows"][0]["status"], "UNKNOWN")
        with Path(result["csv"]).open(encoding="utf-8-sig", newline="") as source:
            table = list(csv.DictReader(source))
        self.assertEqual(table[0]["slave1-G1"], "?")
        self.assertEqual(table[0]["slave1-G2"], "0")

    def test_missing_sample_fails_and_unmeasured_row_stays_unknown(self) -> None:
        self.fake.truncate_stream = True
        result = self.run_scan()
        self.assertEqual(self.last_event, "topology_error")
        self.assertEqual(result["measurements"], 167)
        self.assertEqual(result["rows"][-1]["status"], "UNKNOWN")
        with Path(result["csv"]).open(encoding="utf-8-sig", newline="") as source:
            table = list(csv.DictReader(source))
        self.assertEqual(table[-1]["slave1-G24"], "?")

    def test_wrong_session_is_rejected_and_raw_frame_is_retained(self) -> None:
        self.fake.stale_session = True
        result = self.run_scan()
        self.assertEqual(self.last_event, "topology_error")
        self.assertEqual(result["measurements"], 0)
        self.assertIn("unexpected topology stream", result["error"])
        report = json.loads(Path(result["json"]).read_text(encoding="utf-8"))
        self.assertTrue(any(frame.startswith("TOPO_SAMPLE 0 ") for frame in report["raw_frames"]))

    def test_bus_disabled_stops_before_hardware_commands(self) -> None:
        self.fake.bus_ready = False
        result = self.run_scan()
        self.assertEqual(self.last_event, "topology_error")
        self.assertEqual(self.fake.commands, [("LEFT", "TOPO_INFO")])
        self.assertEqual(result["measurements"], 0)

    def test_offline_slave_stops_before_loading_masks(self) -> None:
        self.fake.missing_slave = True
        result = self.run_scan()
        self.assertEqual(self.last_event, "topology_error")
        self.assertFalse(any(command.startswith("TOPO_BEGIN") for _, command in self.fake.commands))

    def test_source_without_fixed_kelvin_route_stops_before_matrix_commands(self) -> None:
        self.fake.info_override = "OK TOPO_INFO role=MASTER capacity=10 configured=7 bus=1 route=0"
        result = self.run_scan()
        self.assertEqual(self.last_event, "topology_error")
        self.assertIn("fixed Kelvin route must be 1", result["error"])
        self.assertEqual(self.fake.commands, [("LEFT", "TOPO_INFO")])

    def test_capabilities_allow_reordered_fields_and_check_peer_route(self) -> None:
        self.fake.info_override = "OK TOPO_INFO route=1 bus=1 configured=7 capacity=10 role=MASTER extension=1"
        result = self.run_scan()
        self.assertEqual(self.last_event, "topology_error")
        self.assertIn("RIGHT right fixed Kelvin route must be 0", result["error"])
        self.assertTrue(any(command.startswith("TOPO_DISCOVER") for _, command in self.fake.commands))

    def test_duplicate_capability_field_is_rejected(self) -> None:
        self.fake.info_override = "OK TOPO_INFO role=MASTER capacity=10 configured=7 bus=1 route=0 route=1"
        result = self.run_scan()
        self.assertEqual(self.last_event, "topology_error")
        self.assertEqual(self.fake.commands, [("LEFT", "TOPO_INFO")])

    def test_duplicate_sample_coordinates_cannot_complete_a_scan(self) -> None:
        self.fake.bad_coordinates = (0, 0)
        result = self.run_scan()
        self.assertEqual(self.last_event, "topology_error")
        self.assertIn("duplicate sample", result["error"])
        self.assertEqual(result["measurements"], 1)

    def test_out_of_bounds_sample_is_retained_as_an_error(self) -> None:
        self.fake.bad_coordinates = (24, 0)
        result = self.run_scan()
        self.assertEqual(self.last_event, "topology_error")
        self.assertIn("outside scan plan", result["error"])
        self.assertEqual(result["measurements"], 1)

    def test_missing_route_capability_requires_updated_firmware(self) -> None:
        self.fake.info_override = "OK TOPO_INFO role=MASTER capacity=10 configured=7 bus=1"
        result = self.run_scan()
        self.assertEqual(self.last_event, "topology_error")
        self.assertIn("fixed Kelvin route", result["error"])

    def test_timeout_creates_partial_report(self) -> None:
        self.fake.silent_command = "TOPO_INFO"
        result = self.run_scan()
        self.assertEqual(self.last_event, "topology_error")
        self.assertIn("RESULT_TIMEOUT", result["error"])
        self.assertTrue(Path(result["json"]).is_file())

    def test_reconnect_ends_invalid_session_without_waiting_for_lost_terminal(self) -> None:
        self.fake.disconnect_peer = "LEFT"
        result = self.run_scan()
        self.assertEqual(self.last_event, "topology_error")
        self.assertIn("LEFT CONNECTION_LOST", result["error"])
        self.assertNotIn("stream timeout", result["error"])
        self.assertNotIn("abort failed", result["error"])
        self.assertEqual(result["measurements"], 23)
        self.assertEqual(result["cleanup_errors"], [])
        self.assertFalse(any(command.startswith("TOPO_ABORT") for _, command in self.fake.commands))
        report = json.loads(Path(result["json"]).read_text(encoding="utf-8"))
        self.assertEqual(len(report["code_samples"]), 23)
        self.assertEqual([entry["event"] for entry in report["transport_events"]
                          if entry["event"] in {"disconnected", "connected"}],
                         ["disconnected", "connected"])
        self.assertTrue(all("timestamp" in entry and "elapsed_ms" in entry
                            for entry in report["transport_events"]))

    def test_lost_receiver_aborts_source_and_preserves_partial_readings(self) -> None:
        self.fake.disconnect_peer = "RIGHT"
        result = self.run_scan()
        self.assertIn("RIGHT CONNECTION_LOST", result["error"])
        self.assertNotIn("stream timeout", result["error"])
        self.assertEqual(result["measurements"], 23)
        self.assertTrue(any(command.startswith("TOPO_ABORT") for _, command in self.fake.commands))
        self.assertEqual(result["cleanup_errors"], [])

    def test_offline_source_cleanup_is_unconfirmed_and_does_not_send_to_it(self) -> None:
        self.fake.disconnect_peer = "LEFT"
        self.fake.reconnect_peer = False
        result = self.run_scan()
        self.assertIn("LEFT CONNECTION_LOST", result["error"])
        self.assertEqual(result["measurements"], 23)
        self.assertEqual(len(result["cleanup_errors"]), 1)
        self.assertIn("LEFT", result["cleanup_errors"][0])
        self.assertEqual(self.fake.commands[-1][0], "RIGHT")

    def test_unrelated_slave_disconnect_does_not_cancel_master_scan(self) -> None:
        self.fake.disconnect_peer = "m1-s1"
        original = self.fake.send

        def send(target: str, request_id: str, command: str) -> str:
            if command.startswith("TOPO_RUN "):
                self.controller.feed_transport({"event": "disconnected", "peer_id": "m1-s1",
                                                "reason": "heartbeat_timeout"})
            self.fake.disconnect_peer = None
            return original(target, request_id, command)

        self.controller._send_request = send
        result = self.run_scan()
        self.assertEqual(self.last_event, "topology_complete")
        self.assertIsNone(result["error"])

    def test_recovery_waits_for_reconnect_and_busy_reset_before_releasing_owner(self) -> None:
        self.fake.disconnect_peer = "LEFT"
        self.fake.reconnect_peer = False
        self.controller._response_timeout = 0.3
        original_send = self.fake.send
        cleanup_attempts = []
        reconnect = None

        def send(target: str, request_id: str, command: str) -> str:
            nonlocal reconnect
            if target == "LEFT" and command.startswith("TOPO_RESET ") and self.fake.stream_request:
                cleanup_attempts.append(command)
                if len(cleanup_attempts) == 1:
                    self.controller.feed_result(target, request_id, "ERR TOPO_RESET RECOVERY_PENDING")
                    return f"OK FORWARDED {target} {request_id}"
            acknowledgement = original_send(target, request_id, command)
            if command.startswith("TOPO_RUN "):
                reconnect = threading.Timer(0.02, lambda: self.controller.feed_transport(
                    {"event": "connected", "peer_id": "LEFT"}))
                reconnect.start()
            return acknowledgement

        self.controller._send_request = send
        try:
            result = self.run_scan()
        finally:
            if reconnect is not None:
                reconnect.join(timeout=1)
        self.assertIn("LEFT CONNECTION_LOST", result["error"])
        self.assertEqual(len(cleanup_attempts), 2)
        self.assertEqual(result["cleanup_errors"], [])

    def test_next_scan_starts_a_fresh_session_after_reconnect(self) -> None:
        self.fake.disconnect_peer = "LEFT"
        failed = self.run_scan()
        self.assertIn("CONNECTION_LOST", failed["error"])
        self.events.clear()
        self.fake.disconnect_peer = None
        complete = self.run_scan()
        self.assertEqual(self.last_event, "topology_complete")
        self.assertIsNone(complete["connection_error"])
        self.assertEqual(complete["measurements"], 192)

    def test_cleanup_retries_reset_when_its_connection_is_replaced(self) -> None:
        original_send = self.fake.send
        for acknowledgement in ("ERR DELIVERY_FAILED", "OK FORWARDED"):
            with self.subTest(acknowledgement=acknowledgement):
                self.events.clear()
                self.fake.stream_request = None
                self.fake.disconnect_peer = "LEFT"
                attempts = []

                def send(target: str, request_id: str, command: str) -> str:
                    if target == "LEFT" and command.startswith("TOPO_RESET ") and self.fake.stream_request:
                        attempts.append(command)
                        if len(attempts) == 1:
                            self.controller.feed_transport({"event": "disconnected", "peer_id": "LEFT",
                                                            "reason": "delivery_failed"})
                            self.controller.feed_transport({"event": "connected", "peer_id": "LEFT"})
                            return f"{acknowledgement} {target} {request_id}"
                    return original_send(target, request_id, command)

                self.controller._send_request = send
                self.controller._response_timeout = 0.2
                result = self.run_scan()
                self.assertEqual(len(attempts), 2)
                self.assertEqual(result["cleanup_errors"], [])
                self.assertIn("LEFT CONNECTION_LOST", result["error"])

    def test_reliable_cleanup_retries_busy_worker_without_connection_error(self) -> None:
        self.controller._reliable = True
        self.controller._response_timeout = 0.3
        attempts = []

        def send(target: str, request_id: str, command: str) -> str:
            attempts.append((target, command))
            reply = "ERR TOPO_RESET BUSY_USE_ABORT" if len(attempts) == 1 else "OK TOPO_RESET"
            self.assertTrue(self.controller.feed_result(target, request_id, reply))
            return f"OK FORWARDED {target} {request_id}"

        self.controller._send_request = send
        self.assertIsNone(self.controller._connection_error)
        self.controller._reset_master("LEFT", 123)
        self.assertEqual(attempts, [("LEFT", "TOPO_RESET 123")] * 2)
        self.assertEqual(self.controller._pending, {})

    def test_reliable_cleanup_busy_worker_has_bounded_deadline_even_when_cancelled(self) -> None:
        self.controller._reliable = True
        self.controller._response_timeout = 0.12
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):
                attempts = []
                if cancelled:
                    self.controller._cancel.set()

                def send(target: str, request_id: str, command: str) -> str:
                    attempts.append(command)
                    self.assertTrue(self.controller.feed_result(target, request_id, "ERR TOPO_RESET BUSY_USE_ABORT"))
                    return f"OK FORWARDED {target} {request_id}"

                self.controller._send_request = send
                started = time.monotonic()
                with self.assertRaises(TimeoutError):
                    self.controller._reset_master("LEFT", 123)
                elapsed = time.monotonic() - started
                self.assertGreaterEqual(elapsed, 0.1)
                self.assertLess(elapsed, 1.0)
                self.assertGreaterEqual(len(attempts), 2)
                self.assertEqual(self.controller._pending, {})

    def test_reliable_cleanup_does_not_retry_session_mismatch(self) -> None:
        self.controller._reliable = True
        attempts = []

        def send(target: str, request_id: str, command: str) -> str:
            attempts.append(command)
            self.assertTrue(self.controller.feed_result(target, request_id, "ERR TOPO_RESET SESSION_MISMATCH"))
            return f"OK FORWARDED {target} {request_id}"

        self.controller._send_request = send
        with self.assertRaisesRegex(RuntimeError, "SESSION_MISMATCH"):
            self.controller._reset_master("LEFT", 123)
        self.assertEqual(attempts, ["TOPO_RESET 123"])
        self.assertEqual(self.controller._pending, {})

    def test_source_disconnect_during_abort_does_not_wait_for_old_terminal(self) -> None:
        self.fake.disconnect_peer = "RIGHT"
        original_send = self.fake.send

        def send(target: str, request_id: str, command: str) -> str:
            if command.startswith("TOPO_ABORT "):
                self.controller.feed_result(target, request_id, "OK TOPO_ABORT")
                self.controller.feed_transport({"event": "disconnected", "peer_id": "LEFT",
                                                "reason": "peer_closed"})
                self.controller.feed_transport({"event": "connected", "peer_id": "LEFT"})
                return f"OK FORWARDED {target} {request_id}"
            return original_send(target, request_id, command)

        self.controller._send_request = send
        result = self.run_scan()
        self.assertIn("RIGHT CONNECTION_LOST", result["error"])
        self.assertNotIn("abort failed", result["error"])
        self.assertEqual(result["cleanup_errors"], [])
        self.assertEqual(result["measurements"], 23)

    def test_old_disconnect_callback_cannot_hide_a_newer_connection(self) -> None:
        self.fake.disconnect_peer = "LEFT"
        original_observe = self.controller.feed_transport

        def observe(event: dict[str, object]) -> None:
            if event["event"] == "disconnected":
                observed = time.monotonic()
                original_observe({"event": "connected", "peer_id": "LEFT", "monotonic": observed + 0.001})
                original_observe({**event, "monotonic": observed})

        self.controller.feed_transport = observe
        result = self.run_scan()
        self.assertIn("LEFT CONNECTION_LOST", result["error"])
        self.assertEqual(result["cleanup_errors"], [])

    def test_cancel_sends_abort_and_resets_both_masters(self) -> None:
        self.cancel_on_sample = True
        result = self.run_scan()
        self.assertEqual(self.last_event, "topology_stopped")
        self.assertTrue(any(command.startswith("TOPO_ABORT") for _, command in self.fake.commands))
        self.assertEqual([target for target, command in self.fake.commands[-2:]], ["LEFT", "RIGHT"])
        self.assertTrue(all(command.startswith("TOPO_RESET") for _, command in self.fake.commands[-2:]))

    def test_reset_failure_cannot_publish_success(self) -> None:
        self.fake.fail_reset = True
        result = self.run_scan()
        self.assertEqual(self.last_event, "topology_error")
        self.assertEqual(len(result["cleanup_errors"]), 2)

    def test_validation_rejects_same_master_and_more_than_ten_slaves(self) -> None:
        with self.assertRaises(ValueError):
            self.controller.start("ESP1", "ESP1", 1, 1)
        with self.assertRaises(ValueError):
            self.controller.start("ESP1", "ESP2", 11, 1)
        with self.assertRaises(ValueError):
            self.controller.start("ESP1", "ESP2", 1, 1, settle_seconds=5.1)
        self.assertFalse(self.controller.feed_result("ESP1", "old", "TOPO_DONE 1 0"))


if __name__ == "__main__":
    unittest.main()
