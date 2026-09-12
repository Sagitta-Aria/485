from __future__ import annotations

import sqlite3
import json
import tempfile
import time
import unittest
import zlib
from contextlib import closing
from pathlib import Path
from unittest import mock

from cable_tester.analysis import topology_scan as topology

from cable_tester.protocol.topology_transfer import ReplayConflict, SequenceGap, TopologyTransferStore, TransferCorruption, TransferRecord


def frame(sequence=1, job=1, payload="TOPO_SAMPLE 7 0 0 OK MEASURE resistance=1.000 raw=1000 range=2"):
    data = f"7 {sequence} {job} {payload}"
    return f"TOPO_DATA 7 {sequence} {job} {zlib.crc32(data.encode('ascii')):08x} {payload}"


class TopologyTransferStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "sessions" / "scan.sqlite3"
        self.store = TopologyTransferStore(self.path, 7, {"left_master": "master1"})
        self.addCleanup(lambda: self.store.close())

    def test_acknowledgement_survives_reopen_and_duplicate_is_not_reapplied(self):
        record = TransferRecord.parse(frame(), 7)
        self.assertTrue(self.store.accept(record))
        self.assertEqual(self.store.acknowledgement(), (1, record.crc))
        self.store.close()
        self.store = TopologyTransferStore(self.path, 7)
        self.assertFalse(self.store.accept(record))
        self.assertEqual(self.store.cursor, 1)
        with closing(sqlite3.connect(self.path)) as reader:
            self.assertEqual(reader.execute("SELECT payload FROM records").fetchone()[0], record.payload)

    def test_crc_damage_and_missing_sequence_do_not_advance_ack(self):
        with self.assertRaises(TransferCorruption):
            TransferRecord.parse(frame().replace("1.000", "2.000"), 7)
        with self.assertRaises(SequenceGap):
            self.store.accept(TransferRecord.parse(frame(sequence=2), 7))
        self.assertIsNone(self.store.acknowledgement())

    def test_valid_but_conflicting_replay_is_fatal(self):
        self.store.accept(TransferRecord.parse(frame(), 7))
        with self.assertRaises(ReplayConflict):
            self.store.accept(TransferRecord.parse(frame(payload="TOPO_DONE 7 0"), 7))
        self.assertEqual(self.store.cursor, 1)

    def test_failed_sqlite_write_cannot_advance_ack(self):
        self.store._db.set_authorizer(lambda action, *args: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT else sqlite3.SQLITE_OK)
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.accept(TransferRecord.parse(frame(), 7))
        self.assertEqual(self.store.cursor, 0)
        self.assertIsNone(self.store.acknowledgement())

    def test_jobs_are_idempotent_and_journal_requires_same_session(self):
        self.store.record_job(1, "TOPO_RUN2 7 1 master2 1 7 1000")
        self.store.record_job(1, "TOPO_RUN2 7 1 master2 1 7 1000")
        with self.assertRaises(ReplayConflict):
            self.store.record_job(1, "TOPO_POINT2 7 1 master2 1 0 0 1000")
        with self.assertRaises(ReplayConflict):
            TopologyTransferStore(self.path, 8)
        self.assertEqual(self.store._db.execute("PRAGMA synchronous").fetchone()[0], 2)

    def test_salvage_anchor_does_not_claim_previously_acknowledged_records_are_local(self):
        self.store.close()
        self.path = Path(self.temporary.name) / "salvage.sqlite3"
        self.store = TopologyTransferStore(self.path, 7, acknowledged_prefix=2)
        self.assertEqual(self.store.cursor, 2)
        self.assertIsNone(self.store.acknowledgement())
        self.store.accept(TransferRecord.parse(frame(sequence=3), 7))
        self.store.close()
        self.store = TopologyTransferStore(self.path, 7)
        self.assertEqual(self.store.cursor, 3)
        exported = self.store.export()
        self.assertEqual(exported["acknowledged_prefix_without_local_records"], 2)
        self.assertEqual([record["sequence"] for record in exported["records"]], [3])


class CachedMasters:
    """Firmware-shaped durable jobs with controllable loss, replay, and cache states."""

    def __init__(self):
        self.controller = None
        self.commands = []
        self.records = []
        self.jobs = {}
        self.masks = {}
        self.acked = 0
        self.sid = 0
        self.lost_ack = False
        self.lost_job = False
        self.damage = None
        self.disconnect = False
        self.offline = False
        self.recovered = False
        self.cache_pause = False
        self.fetches = 0
        self.ack_checked = []
        self.old_cache = False
        self.storage_failed = False
        self.cache_ready = True
        self.binary = False
        self.graph = None
        self.binary_reading = None

    def append(self, job, payload):
        sequence = len(self.records) + 1
        raw = f"{self.sid} {sequence} {job} {payload}"
        crc = f"{zlib.crc32(raw.encode('ascii')):08x}"
        self.records.append((sequence, job, crc, payload))

    def send(self, target, request_id, command):
        self.commands.append((target, command))
        fields = command.split()
        operation = fields[0]
        responses = []
        if operation == "TOPO_INFO":
            cache = f"cache_session={self.sid} cache_ack={self.acked} cache_next={len(self.records) + 1}" if target == "LEFT" else "cache_session=0 cache_ack=0 cache_next=1"
            responses = [f"OK TOPO_INFO role=MASTER capacity=10 configured=1 bus=1 route={int(target == 'LEFT')} reliable=1 cache_ready={int(self.cache_ready)} cache_high=70 cache_low=50 {cache} plan_session={7 if self.old_cache and target == 'RIGHT' else 0} binary={int(self.binary)}"]
        elif operation == "TOPO_DISCOVER":
            responses = [f"OK TOPO_DISCOVER count={fields[1]} online={(1 << int(fields[1])) - 1:08x}"]
        elif operation == "TOPO_OPEN":
            if self.sid != int(fields[1]):
                self.records.clear()
                self.jobs.clear()
                self.acked = 0
            self.sid = int(fields[1])
            responses = ["OK TOPO_OPEN"]
        elif operation == "TOPO_MASK":
            self.masks[int(fields[2])] = int(fields[4], 16)
            responses = ["OK TOPO_MASK"]
        elif operation in {"TOPO_RUN2", "TOPO_POINT2", "TOPO_RANGE2"}:
            job = int(fields[2])
            if job in self.jobs:
                if self.jobs[job] != command:
                    raise AssertionError("job retry changed its command")
            else:
                self.jobs[job] = command
                if operation == "TOPO_RUN2":
                    for index in range(int(fields[5])):
                        for source in range(24):
                            reading = ("OK MEASURE resistance=1.000 raw=1000 range=2"
                                       if self.masks[index] & (1 << source) else "ERR MEASURE OVERRANGE status=1 frame=0103")
                            self.append(job, f"TOPO_SAMPLE {self.sid} {source} {index} {reading}")
                    if not self.storage_failed:
                        self.append(job, f"TOPO_DONE {self.sid} 168")
                else:
                    source, first = int(fields[5]), int(fields[6])
                    end = int(fields[7]) if operation == "TOPO_RANGE2" else first + 1
                    connected = self.graph is None or any(first <= port < end for port in self.graph.get(source, ()))
                    raw = "OK MEASURE resistance=1.000 raw=1000 range=2" if connected else "ERR MEASURE OVERRANGE status=1 frame=0103"
                    if self.binary_reading:
                        raw = self.binary_reading(source, first, end, raw)
                    payload = (f"TOPO_RANGE_SAMPLE {self.sid} {source} {first} {end} {raw}" if operation == "TOPO_RANGE2"
                               else f"TOPO_POINT_SAMPLE {self.sid} {source} {first} {raw}")
                    self.append(job, payload)
                    self.append(job, f"TOPO_DONE {self.sid} 1")
            if self.lost_job:
                self.lost_job = False
                return f"OK FORWARDED {target} {request_id}"
            responses = [f"OK {operation}"]
        elif operation == "TOPO_FETCH":
            self.fetches += 1
            start = int(fields[2])
            selected = [record for record in self.records if record[0] >= start][:16]
            if self.damage == "duplicate" and self.fetches == 1:
                selected = [selected[0], *selected[:15]]
            if self.damage == "gap" and self.fetches == 1:
                selected = selected[1:]
            responses = [f"TOPO_DATA {self.sid} {sequence} {job} {crc} {payload}" for sequence, job, crc, payload in selected]
            if responses and (self.damage == "persistent" or (self.damage == "crc" and self.fetches == 1)):
                responses[0] += " damaged"
            state = "RECOVERED" if self.recovered else "DONE"
            reason = "NONE"
            if self.storage_failed:
                state, reason = "FAILED", "STORAGE"
            used = 20
            if self.cache_pause and self.fetches <= 2:
                state, reason, used = "PAUSED", "CACHE", 70 if self.fetches == 1 else 40
            responses.append(f"OK TOPO_FETCH session={self.sid} first={self.acked + 1} next={len(self.records) + 1} ack={self.acked} used={used} job={max(self.jobs)} state={state} reason={reason} high=70 low=50")
        elif operation == "TOPO_ACK":
            sequence, crc = int(fields[2]), fields[3]
            with closing(sqlite3.connect(self.controller._reliable_store.path)) as reader:
                committed = reader.execute("SELECT crc FROM records WHERE sequence=?", (sequence,)).fetchone()
                count = reader.execute("SELECT COUNT(*) FROM records WHERE sequence<=?", (sequence,)).fetchone()[0]
                prefix = int(reader.execute("SELECT value FROM metadata WHERE name='acknowledged_prefix'").fetchone()[0])
            if committed != (crc,) or count != sequence - prefix:
                raise AssertionError("host acknowledged data before its durable commit")
            self.ack_checked.append(sequence)
            self.acked = max(self.acked, sequence)
            if self.lost_ack:
                self.lost_ack = False
                return f"OK FORWARDED {target} {request_id}"
            responses = ["OK TOPO_ACK"]
        else:
            responses = [f"OK {operation}"]
        for response in responses:
            self.controller.feed_result(target, request_id, response)
        if operation == "TOPO_FETCH" and self.disconnect:
            self.disconnect = False
            self.controller.feed_transport({"event": "disconnected", "peer_id": "LEFT", "reason": "socket_error"})
            if not self.offline:
                self.controller.feed_transport({"event": "connected", "peer_id": "LEFT"})
        return f"OK FORWARDED {target} {request_id}"


class ReliableTopologyControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fake = CachedMasters()
        self.events = []
        self.cancel_paused = False
        self.cancel_progress = False

        def publish(event, payload):
            self.events.append((event, payload))
            if self.cancel_paused and event == "topology_paused":
                self.controller.cancel()
            if self.cancel_progress and event == "topology_progress":
                self.controller.cancel()

        self.controller = topology.TopologyScanController(
            self.fake.send, publish, report_root=Path(self.temporary.name),
            response_timeout_seconds=0.05, stream_timeout_seconds=0.1,
        )
        self.fake.controller = self.controller

    def run_scan(self):
        self.assertTrue(self.controller.start("LEFT", "RIGHT", 1, 1, settle_seconds=0))
        deadline = time.monotonic() + 8
        while self.controller.running and time.monotonic() < deadline:
            time.sleep(0.005)
        if self.controller.running:
            self.controller.cancel()
            self.controller._thread.join(timeout=2)
        self.assertFalse(self.controller.running)
        terminals = [payload for event, payload in self.events if event in {"topology_complete", "topology_error", "topology_stopped"}]
        self.assertEqual(len(terminals), 1)
        return terminals[0]

    def test_durable_commit_precedes_every_ack_in_complete_scan(self):
        result = self.run_scan()
        self.assertIsNone(result["error"])
        self.assertEqual(result["measurements"], 192)
        self.assertEqual(result["transfer_mode"], "durable_cached")
        self.assertEqual(self.fake.acked, 217)
        self.assertTrue(Path(result["durable_session"]).is_file())

    def test_lost_job_reply_retries_same_identity_without_remeasurement(self):
        self.fake.lost_job = True
        result = self.run_scan()
        self.assertIsNone(result["error"])
        jobs = [command for _, command in self.fake.commands if command.startswith("TOPO_RUN2 ")]
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0], jobs[1])
        self.assertEqual(len(self.fake.records), 217)

    def test_lost_ack_is_retried_and_does_not_duplicate_samples(self):
        self.fake.lost_ack = True
        result = self.run_scan()
        self.assertIsNone(result["error"])
        self.assertEqual(result["measurements"], 192)
        self.assertEqual(self.fake.ack_checked[:2], [16, 16])

    def test_crc_gap_and_duplicate_batches_recover_without_remeasurement(self):
        for damage in ("crc", "gap", "duplicate"):
            with self.subTest(damage=damage):
                self.fake.damage = damage
                result = self.run_scan()
                self.assertIsNone(result["error"])
                self.assertEqual(result["measurements"], 192)
                self.fake = CachedMasters()
                self.fake.controller = self.controller
                self.controller._send_request = self.fake.send
                self.events.clear()

    def test_connection_replacement_resumes_receiver_before_source(self):
        self.fake.disconnect = True
        result = self.run_scan()
        self.assertIsNone(result["error"])
        self.assertIsNone(result["connection_error"])
        resumes = [target for target, command in self.fake.commands if command.startswith("TOPO_RESUME ")]
        self.assertEqual(resumes[:2], ["RIGHT", "LEFT"])
        self.assertEqual(len([command for _, command in self.fake.commands if command.startswith("TOPO_RUN2 ")]), 1)

    def test_persistent_corruption_aborts_without_acknowledging_bad_data(self):
        self.fake.damage = "persistent"
        result = self.run_scan()
        self.assertIn("three fetches", result["error"])
        self.assertEqual(self.fake.ack_checked, [])
        self.assertTrue(any(command.startswith("TOPO_ABORT ") for _, command in self.fake.commands))

    def test_disk_failure_aborts_and_retains_device_records_without_ack(self):
        with mock.patch.object(TopologyTransferStore, "accept", side_effect=OSError("disk full")):
            result = self.run_scan()
        self.assertIn("disk full", result["error"])
        self.assertEqual(self.fake.ack_checked, [])
        self.assertEqual(len(self.fake.records), 169)

    def test_reboot_recovers_cached_data_then_requires_new_scan(self):
        self.fake.recovered = True
        result = self.run_scan()
        self.assertIn("SOURCE_REBOOTED", result["error"])
        self.assertEqual(result["measurements"], 168)
        self.assertEqual(self.fake.acked, 169)

    def test_offline_pause_can_be_cancelled(self):
        self.fake.disconnect = self.fake.offline = self.cancel_paused = True
        result = self.run_scan()
        self.assertLess(result["measurements"], 168)
        self.assertTrue(any(event == "topology_paused" for event, _ in self.events))

    def test_cache_high_pause_and_below_low_resume_are_visible(self):
        self.fake.cache_pause = True
        result = self.run_scan()
        self.assertIsNone(result["error"])
        pauses = [payload for event, payload in self.events if event == "topology_paused" and payload.get("reason") == "CACHE"]
        self.assertEqual([payload["used"] for payload in pauses], [70, 40])
        self.assertTrue(any(command.startswith("TOPO_RESUME ") for _, command in self.fake.commands))

    def test_online_cancel_drains_committed_source_data_and_acks_terminal(self):
        self.cancel_progress = True
        self.controller._response_timeout = 2
        result = self.run_scan()
        self.assertIsNone(result["error"])
        self.assertEqual(result["measurements"], 168)
        self.assertEqual(self.fake.acked, 169)
        self.assertTrue(any(event == "topology_stopped" for event, _ in self.events))
        commands = [command.split()[0] for _, command in self.fake.commands]
        self.assertLess(commands.index("TOPO_ABORT"), len(commands) - 1 - commands[::-1].index("TOPO_ACK"))

    def test_failed_storage_manifest_drains_records_then_stops_without_busy_polling(self):
        self.fake.storage_failed = True
        result = self.run_scan()
        self.assertIn("FAILED reason=STORAGE", result["error"])
        self.assertEqual(result["measurements"], 168)
        self.assertEqual(self.fake.acked, 168)

    def test_previous_flash_cache_is_exported_separately_before_new_scan(self):
        self.fake.sid = 7
        self.fake.old_cache = True
        self.fake.jobs[1] = "old_command"
        self.fake.append(1, "TOPO_SAMPLE 7 0 0 OK MEASURE resistance=9.000 raw=9000 range=2")
        self.fake.append(1, "TOPO_SAMPLE 7 1 0 ERR MEASURE OVERRANGE status=1 frame=0103")
        self.fake.append(1, "TOPO_STOPPED 7 2")
        self.fake.acked = 1
        self.controller._response_timeout = 2
        result = self.run_scan()
        self.assertIsNone(result["error"])
        self.assertEqual(result["measurements"], 192)
        self.assertEqual(len(result["recovered_sessions"]), 1)
        recovered = result["recovered_sessions"][0]
        exported = json.loads(Path(recovered["json"]).read_text(encoding="utf-8"))
        self.assertEqual(exported["session_id"], 7)
        self.assertEqual(exported["acknowledged_prefix_without_local_records"], 1)
        self.assertEqual([record["sequence"] for record in exported["records"]], [2, 3])
        self.assertIn(("RIGHT", "TOPO_RESET 7"), self.fake.commands)
        commands = [command.split()[0] for _, command in self.fake.commands]
        self.assertLess(commands.index("TOPO_ACK"), commands.index("TOPO_OPEN"))

    def _previous_cache_with_module_limits(self, previous_count, online_count, *, live_job=False):
        """Model reboot-lost module count; a live job keeps its authoritative count."""
        self.fake.sid = 7
        self.fake.jobs[1] = "old_command"
        self.fake.append(1, "TOPO_STOPPED 7 0")
        self.fake.acked = 1
        path = Path(self.temporary.name) / "sessions" / "session_previous_00000007.sqlite3"
        with closing(TopologyTransferStore(path, 7, {
            "left_master": "LEFT", "right_master": "RIGHT",
            "left_modules": previous_count, "right_modules": 1,
        }, acknowledged_prefix=1)):
            pass
        original_send = self.fake.send
        selected = 7
        recovering = True

        def send(target, request_id, command):
            nonlocal selected, recovering
            response = None
            if recovering and target == "LEFT":
                if command.startswith("TOPO_DISCOVER "):
                    if live_job:
                        response = "ERR TOPO_DISCOVER BUSY"
                    else:
                        selected = int(command.split()[1])
                        online = (1 << min(selected, online_count)) - 1
                        response = f"OK TOPO_DISCOVER count={selected} online={online:08x}"
                elif command == "TOPO_RESET 7":
                    if (previous_count if live_job else selected) > online_count:
                        response = "ERR TOPO_RESET ESP_ERR_TIMEOUT"
                    else:
                        recovering = False
            if response is not None:
                self.fake.commands.append((target, command))
                self.controller.feed_result(target, request_id, response)
                return f"OK FORWARDED {target} {request_id}"
            return original_send(target, request_id, command)

        self.controller._send_request = send

    def test_rebooted_single_slave_cache_restores_count_before_reset(self):
        self._previous_cache_with_module_limits(1, 1)
        result = self.run_scan()
        self.assertIsNone(result["error"])
        self.assertLess(self.fake.commands.index(("LEFT", "TOPO_DISCOVER 1")),
                        self.fake.commands.index(("LEFT", "TOPO_RESET 7")))

    def test_old_cleanup_uses_previous_count_before_smaller_new_scan(self):
        self._previous_cache_with_module_limits(3, 3)
        result = self.run_scan()
        self.assertIsNone(result["error"])
        commands = self.fake.commands
        self.assertLess(commands.index(("LEFT", "TOPO_DISCOVER 3")),
                        commands.index(("LEFT", "TOPO_RESET 7")))
        self.assertLess(commands.index(("LEFT", "TOPO_RESET 7")),
                        commands.index(("LEFT", "TOPO_DISCOVER 1")))

    def test_live_previous_job_keeps_firmware_cleanup_count(self):
        self._previous_cache_with_module_limits(3, 3, live_job=True)
        result = self.run_scan()
        self.assertIsNone(result["error"])
        self.assertIn(("LEFT", "TOPO_DISCOVER 3"), self.fake.commands)

    def test_missing_previous_slave_cannot_be_hidden_by_smaller_new_scan(self):
        self._previous_cache_with_module_limits(3, 1)
        result = self.run_scan()
        self.assertIn("previous session 7 missing configured slaves", result["error"])
        self.assertNotIn(("LEFT", "TOPO_RESET 7"), self.fake.commands)
        self.assertFalse(any(command.startswith("TOPO_OPEN ") for _, command in self.fake.commands))

    def test_upgraded_firmware_without_cache_fails_instead_of_silent_legacy_fallback(self):
        self.fake.cache_ready = False
        result = self.run_scan()
        self.assertIn("CACHE_UNAVAILABLE", result["error"])
        self.assertEqual(result["transfer_mode"], "durable_cache_unavailable")
        self.assertEqual(self.fake.jobs, {})


if __name__ == "__main__":
    unittest.main()
