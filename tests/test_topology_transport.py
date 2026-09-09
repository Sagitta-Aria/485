from __future__ import annotations

import json
import queue
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from contextlib import closing

import cable_tester_gui as gui
import topology_scan as topology
from test_topology_transfer import CachedMasters


class LoopbackMaster:
    """Serve one-module topology frames on an isolated test router socket."""

    def __init__(self, server: gui.RouterServer, peer_id: str, masks: dict[int, int]) -> None:
        self.server = server
        self.peer_id = peer_id
        self.masks = masks
        self.commands: list[str] = []
        self.errors: list[Exception] = []
        self.disconnect_after: int | None = None
        self.disconnected_at: float | None = None
        self.reconnected = threading.Event()
        self.stopping = threading.Event()
        self.client = self._connect()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _connect(self) -> socket.socket:
        client = socket.create_connection(self.server.server_address, timeout=1)
        try:
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            client.sendall(f"HELLO MASTER {self.peer_id}\n".encode("ascii"))
            reply = bytearray()
            while not reply.endswith(b"\n"):
                chunk = client.recv(1)
                if not chunk:
                    raise AssertionError("router closed before registration")
                reply.extend(chunk)
            if reply.decode("ascii").strip() != f"OK REGISTERED {self.peer_id}":
                raise AssertionError(f"registration failed: {reply!r}")
            client.settimeout(0.1)
            return client
        except Exception:
            client.close()
            raise

    def close(self) -> None:
        self.stopping.set()
        try:
            self.client.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.client.close()
        self.thread.join(timeout=2)

    @staticmethod
    def _measurement(connected: bool) -> str:
        return (
            "OK MEASURE resistance=25.380 raw=2538 range=1"
            if connected else "ERR MEASURE OVERRANGE status=1 frame=0103"
        )

    def _responses(self, command: str) -> list[str]:
        fields = command.split()
        operation = fields[0]
        self.commands.append(command)
        if operation == "TOPO_INFO":
            route = int(self.peer_id == "master1")
            return [f"OK TOPO_INFO role=MASTER capacity=10 configured=7 bus=1 route={route}"]
        if operation == "TOPO_DISCOVER":
            return ["OK TOPO_DISCOVER count=1 online=00000001"]
        if operation == "TOPO_MASK":
            self.masks[int(fields[2])] = int(fields[4], 16)
        if operation == "TOPO_RUN":
            session, _, count, rounds, _ = fields[1:]
            responses = ["OK TOPO_RUN"]
            for index in range(int(rounds)):
                for source in range(int(count) * 24):
                    connected = source == 0 and bool(self.masks[index] & 1)
                    responses.append(
                        f"TOPO_SAMPLE {session} {source} {index} {self._measurement(connected)}"
                    )
                    if len(responses) - 1 == self.disconnect_after:
                        return responses
            responses.append(f"TOPO_DONE {session} {int(count) * 24 * int(rounds)}")
            return responses
        if operation == "TOPO_POINT":
            session, _, _, source, destination, _ = fields[1:]
            return [
                "OK TOPO_POINT",
                f"TOPO_POINT_SAMPLE {session} {source} {destination} "
                f"{self._measurement(source == destination == '0')}",
                f"TOPO_DONE {session} 1",
            ]
        return [f"OK {operation}"]

    def _reconnect(self) -> None:
        self.disconnected_at = time.monotonic()
        self.client.shutdown(socket.SHUT_RDWR)
        self.client.close()
        deadline = time.monotonic() + 1
        while self.server.state.get(self.peer_id) is not None:
            if self.stopping.wait(0.005):
                return
            if time.monotonic() >= deadline:
                raise AssertionError("router did not remove the disconnected socket")
        if not self.stopping.is_set():
            self.client = self._connect()
            self.reconnected.set()

    def _should_reconnect(self, command: str) -> bool:
        """Let cached-protocol tests disconnect after a deliberately incomplete FETCH."""
        return command.startswith("TOPO_RUN ") and self.disconnect_after is not None

    def _serve(self) -> None:
        buffer = bytearray()
        try:
            while not self.stopping.is_set():
                try:
                    chunk = self.client.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    return
                buffer.extend(chunk)
                while b"\n" in buffer:
                    raw, _, remainder = buffer.partition(b"\n")
                    buffer = bytearray(remainder)
                    prefix, owner, request_id, command = raw.decode("ascii").split(maxsplit=3)
                    if prefix != "FROM":
                        raise AssertionError(f"unexpected router frame: {raw!r}")
                    responses = self._responses(command)
                    packet = "".join(
                        f"RESULT {self.peer_id} {owner} {request_id} {payload}\n"
                        for payload in responses
                    )
                    self.client.sendall(packet.encode("ascii"))
                    if self._should_reconnect(command):
                        self._reconnect()
                        buffer.clear()
        except Exception as error:
            if not self.stopping.is_set():
                self.errors.append(error)


class CachedReplyCollector:
    """Capture fake device replies; only the real TCP router delivers them to the host."""

    def __init__(self, controller: topology.TopologyScanController) -> None:
        self.controller = controller
        self.responses: list[str] = []
        self.lock = threading.Lock()

    @property
    def _reliable_store(self):
        return self.controller._reliable_store

    def feed_result(self, _target: str, _request: str, payload: str) -> bool:
        self.responses.append(payload)
        return True


class CachedLoopbackMaster(LoopbackMaster):
    """Keep CachedMasters journal state across actual socket replacement."""

    def __init__(self, server, peer_id, backend: CachedMasters, collector: CachedReplyCollector):
        self.backend = backend
        self.collector = collector
        self.partial_fetch_sent = False
        self.reconnect_after_response = False
        self.cursor_at_disconnect: tuple[int, int] | None = None
        self.partial_count = 5
        super().__init__(server, peer_id, backend.masks)

    def _responses(self, command: str) -> list[str]:
        self.commands.append(command)
        with self.collector.lock:
            self.collector.responses = []
            self.backend.send("LEFT" if self.peer_id == "master1" else "RIGHT", "cached-test", command)
            responses = list(self.collector.responses)
        if self.peer_id == "master1" and command.startswith("TOPO_FETCH ") and not self.partial_fetch_sent:
            if not all(payload.startswith("TOPO_DATA ") for payload in responses[:self.partial_count]):
                raise AssertionError("test requires cached records before the dropped manifest")
            self.partial_fetch_sent = True
            self.reconnect_after_response = True
            return responses[:self.partial_count]
        return responses

    def _should_reconnect(self, command: str) -> bool:
        if not self.reconnect_after_response:
            return False
        self.reconnect_after_response = False
        with closing(sqlite3.connect(self.collector._reliable_store.path)) as reader:
            durable = reader.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        self.cursor_at_disconnect = durable, self.backend.acked
        return True


class TopologyTransportIntegrationTests(unittest.TestCase):
    """Exercise real TCP routing without draining the Tk event queue."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.report: dict[str, object] | None = None
        self.worker_thread: threading.Thread | None = None
        self.finished = threading.Event()

        def publish(kind: str, payload: object) -> None:
            self.worker_thread = threading.current_thread()
            self.events.put((kind, payload))
            if kind in {"topology_complete", "topology_stopped", "topology_error"}:
                self.report = payload
                self.finished.set()

        self.controller = topology.TopologyScanController(
            lambda target, request, payload: self.server.state.send_from_controller(target, request, payload),
            publish,
            report_root=Path(self.temporary.name),
            response_timeout_seconds=1,
            stream_timeout_seconds=10,
        )
        self.server = gui.RouterServer(
            ("127.0.0.1", 0), self.events,
            on_result=self.controller.feed_result,
            on_transport=self.controller.feed_transport,
        )
        self.server_thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True,
        )
        self.server_thread.start()
        self.peers: list[LoopbackMaster] = []
        self.addCleanup(self._close)
        masks: dict[int, int] = {}
        for peer_id in ("master1", "master2"):
            self.peers.append(LoopbackMaster(self.server, peer_id, masks))

    def _close(self) -> None:
        if self.controller.running:
            self.controller.cancel()
        for peer in self.peers:
            peer.close()
        self.server.state.close_all()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        if self.worker_thread is not None:
            self.worker_thread.join(timeout=4)
            self.assertFalse(self.worker_thread.is_alive())
        self.assertFalse(self.server_thread.is_alive())
        self.assertFalse(self.controller.running)
        for peer in self.peers:
            self.assertFalse(peer.thread.is_alive())
            self.assertEqual(peer.errors, [])

    def _run_scan(self) -> dict[str, object]:
        self.assertTrue(self.controller.start("master1", "master2", 1, 1, settle_seconds=0))
        self.assertTrue(self.finished.wait(4), "scan needed GUI polling or waited for the old stream")
        self.worker_thread.join(timeout=1)
        self.assertIsNotNone(self.report)
        with self.events.mutex:
            self.assertFalse(any(kind == "result_frame" for kind, _ in self.events.queue))
        reports = list(Path(self.temporary.name).glob("*.json"))
        self.assertEqual(len(reports), 1)
        return json.loads(reports[0].read_text(encoding="utf-8"))

    def test_complete_scan_does_not_need_gui_event_consumption(self) -> None:
        report = self._run_scan()
        self.assertIsNone(report["error"])
        self.assertEqual(report["cleanup_errors"], [])
        self.assertEqual(len(report["code_samples"]), 168)
        self.assertEqual(len(report["point_samples"]), 1)
        self.assertEqual(report["rows"][0]["connection_resistances"][0]["resistance_ohm"], 25.38)
        self.assertTrue(all(any(command.startswith("TOPO_RESET ") for command in peer.commands) for peer in self.peers))

    def test_disconnect_reconnect_preserves_partial_samples_and_ends_old_scan(self) -> None:
        master = self.peers[0]
        master.disconnect_after = 23
        report = self._run_scan()
        self.assertTrue(master.reconnected.is_set())
        self.assertLess(time.monotonic() - master.disconnected_at, 3)
        self.assertIn("master1 CONNECTION_LOST reason=peer_closed", report["error"])
        self.assertNotIn("abort failed", report["error"])
        self.assertNotIn("stream timeout", report["error"])
        self.assertEqual(report["cleanup_errors"], [])
        self.assertEqual(len(report["code_samples"]), 23)
        self.assertEqual(len(report["point_samples"]), 0)
        self.assertEqual([(sample["source"], sample["round"]) for sample in report["code_samples"]], [(source, 0) for source in range(23)])
        self.assertFalse(any(command.startswith(("TOPO_ABORT ", "TOPO_POINT ")) for command in master.commands))
        self.assertEqual(sum(command.startswith("TOPO_RUN ") for command in master.commands), 1)
        self.assertTrue(all(any(command.startswith("TOPO_RESET ") for command in peer.commands) for peer in self.peers))
        lifecycle = [event["event"] for event in report["transport_events"] if event.get("peer_id") == "master1" and event["event"] in {"connected", "disconnected"}]
        self.assertEqual(lifecycle, ["disconnected", "connected"])

    def test_cached_fetch_reconnect_replays_unconfirmed_batch_without_gui_polling(self) -> None:
        for peer in self.peers:
            peer.close()
        deadline = time.monotonic() + 1
        while self.server.state.connected_ids() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(self.server.state.connected_ids(), ())
        self.peers.clear()
        backend = CachedMasters()
        collector = CachedReplyCollector(self.controller)
        backend.controller = collector
        for peer_id in ("master1", "master2"):
            self.peers.append(CachedLoopbackMaster(self.server, peer_id, backend, collector))

        report = self._run_scan()
        source = self.peers[0]
        self.assertTrue(source.reconnected.is_set())
        self.assertEqual(source.cursor_at_disconnect, (0, 0))
        self.assertIsNone(report["error"])
        self.assertIsNone(report["connection_error"])
        self.assertEqual(report["cleanup_errors"], [])
        self.assertEqual(report["transfer_mode"], "durable_cached")
        self.assertEqual(len(report["code_samples"]), 168)
        self.assertEqual(len(report["point_samples"]), 24)
        self.assertEqual(len({(sample["source"], sample["round"]) for sample in report["code_samples"]}), 168)
        self.assertEqual(len({(sample["source"], sample["destination"]) for sample in report["point_samples"]}), 24)
        self.assertEqual(len(report["raw_frames"]), 217)
        self.assertEqual(report["durable_sequence"], 217)
        self.assertEqual(backend.acked, 217)
        self.assertEqual(len(backend.records), 217)
        fetch_starts = [int(command.split()[2]) for command in source.commands if command.startswith("TOPO_FETCH ")]
        self.assertEqual(fetch_starts[:2], [1, 1])
        self.assertEqual(sum(command.startswith("TOPO_RUN2 ") for command in source.commands), 1)
        self.assertEqual(sum(command.startswith("TOPO_POINT2 ") for command in source.commands), 24)
        self.assertEqual(backend.ack_checked[0], 16)
        resumes = [target for target, command in backend.commands if command.startswith("TOPO_RESUME ")]
        self.assertEqual(resumes[:2], ["RIGHT", "LEFT"])
        lifecycle = [event["event"] for event in report["transport_events"]
                     if event.get("peer_id") == "master1" and event["event"] in {"connected", "disconnected"}]
        self.assertEqual(lifecycle, ["disconnected", "connected"])
        with closing(sqlite3.connect(report["durable_session"])) as reader:
            self.assertEqual(reader.execute("SELECT COUNT(*), MAX(sequence) FROM records").fetchone(), (217, 217))
        with self.events.mutex:
            self.assertTrue(any(kind == "topology_paused" for kind, _ in self.events.queue))
            self.assertFalse(any(kind == "result_frame" for kind, _ in self.events.queue))

    def test_post_scan_point_recheck_survives_tcp_replacement_in_a_new_session(self) -> None:
        for peer in self.peers:
            peer.close()
        deadline = time.monotonic() + 1
        while self.server.state.connected_ids() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(self.server.state.connected_ids(), ())
        self.peers.clear()
        backend = CachedMasters()
        backend.graph = {source: {source} for source in range(24)}
        backend.binary_reading = lambda source, first, end, raw: "ERR MEASURE TIMEOUT" if (source, first) == (0, 0) else raw
        collector = CachedReplyCollector(self.controller)
        backend.controller = collector
        for peer_id in ("master1", "master2"):
            self.peers.append(CachedLoopbackMaster(self.server, peer_id, backend, collector))
        original = self._run_scan()
        parameters = self.report["recheck_parameters"]
        self.assertEqual(self.report["pending_recheck_count"], 24)
        source = self.peers[0]
        source.partial_fetch_sent = False
        source.partial_count = 1
        source.reconnected.clear()
        source.commands.clear()
        backend.binary_reading = None
        self.finished.clear()
        self.assertTrue(self.controller.start(**parameters))
        self.assertTrue(self.finished.wait(8))
        self.worker_thread.join(timeout=1)
        self.assertIsNone(self.report["error"])
        self.assertEqual(self.report["rows"][0]["status"], "UNIQUE")
        self.assertEqual(self.report["measurements"], 24)
        self.assertEqual(self.report["pending_recheck_count"], 0)
        self.assertTrue(source.reconnected.is_set())
        self.assertEqual(source.cursor_at_disconnect, (0, 0))
        self.assertEqual(backend.acked, 48)
        self.assertEqual(len(backend.jobs), 24)
        self.assertFalse(any(command.startswith("TOPO_RUN") for command in source.commands))
        report = json.loads(Path(self.report["json"]).read_text(encoding="utf-8"))
        self.assertNotEqual(report["session_id"], original["session_id"])
        self.assertEqual(report["recheck"]["parent_session_id"], original["session_id"])
        self.assertEqual(report["code_samples"], original["code_samples"])
        with self.events.mutex:
            self.assertFalse(any(kind == "result_frame" for kind, _ in self.events.queue))

    def test_binary_four_way_scan_survives_actual_tcp_replacement(self) -> None:
        for peer in self.peers:
            peer.close()
        deadline = time.monotonic() + 1
        while self.server.state.connected_ids() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(self.server.state.connected_ids(), ())
        self.peers.clear()
        backend = CachedMasters()
        backend.binary = True
        backend.graph = {0: {0, 5, 12, 23}}
        collector = CachedReplyCollector(self.controller)
        backend.controller = collector
        for peer_id in ("master1", "master2"):
            peer = CachedLoopbackMaster(self.server, peer_id, backend, collector)
            peer.partial_count = 1
            self.peers.append(peer)
        self.assertTrue(self.controller.start("master1", "master2", 1, 1, settle_seconds=0, scan_method="binary", binary_repeats=2))
        self.assertTrue(self.finished.wait(12))
        self.worker_thread.join(timeout=1)
        self.assertIsNone(self.report["error"])
        self.assertEqual(self.report["rows"][0]["targets"], ["slave1-G1", "slave1-G6", "slave1-G13", "slave1-G24"])
        self.assertEqual(self.report["binary_confirmation_repeats"], 2)
        self.assertEqual(self.report["rows"][0]["status"], "SHORT")
        self.assertTrue(self.peers[0].reconnected.is_set())
        self.assertEqual(self.peers[0].cursor_at_disconnect, (0, 0))
        self.assertEqual(len(backend.jobs), self.report["measurements"])
        self.assertEqual(backend.acked, self.report["measurements"] * 2)
        with self.events.mutex:
            self.assertFalse(any(kind == "result_frame" for kind, _ in self.events.queue))


if __name__ == "__main__":
    unittest.main()
