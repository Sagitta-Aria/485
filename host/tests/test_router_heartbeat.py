from __future__ import annotations

import queue
import socket
import threading
import time
import unittest
from unittest import mock

from cable_tester.ui import gui as gui


class RouterHeartbeatTests(unittest.TestCase):
    """Exercise heartbeat expiry through the real threaded TCP handler."""

    def setUp(self) -> None:
        # A short injected lease keeps the test fast while production uses seconds.
        self.timeout_patch = mock.patch.object(
            gui, "HEARTBEAT_TIMEOUT_SECONDS", 0.25, create=True
        )
        self.timeout_patch.start()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.server = gui.RouterServer(("127.0.0.1", 0), self.events)
        self.server_thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.server_thread.start()
        self.clients: list[socket.socket] = []

    def tearDown(self) -> None:
        for client in self.clients:
            client.close()
        self.server.shutdown()
        self.server.state.close_all()
        self.server.server_close()
        self.server_thread.join(timeout=1.0)
        self.timeout_patch.stop()

    def connect_peer(
        self, peer_id: str, role: str = "NODE"
    ) -> tuple[socket.socket, str]:
        """Connect and complete the HELLO exchange for one test peer."""
        client = socket.create_connection(self.server.server_address, timeout=1.0)
        client.settimeout(1.0)
        self.clients.append(client)
        client.sendall(f"HELLO {role} {peer_id}\n".encode("utf-8"))
        return client, self.receive_line(client)

    @staticmethod
    def receive_line(client: socket.socket) -> str:
        """Read one newline-delimited protocol frame from a test socket."""
        data = bytearray()
        while not data.endswith(b"\n"):
            chunk = client.recv(128)
            if not chunk:
                break
            data.extend(chunk)
        return data.decode("utf-8").strip()

    @staticmethod
    def wait_until(predicate, timeout: float = 1.5) -> bool:
        """Poll asynchronous server state until it matches the expectation."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

    def test_silent_peer_expires_and_same_id_can_reconnect(self) -> None:
        silent_client, reply = self.connect_peer("ESP1")
        self.assertEqual(reply, "OK REGISTERED ESP1")
        self.assertEqual(self.server.state.connected_ids(), ("ESP1",))

        self.assertTrue(
            self.wait_until(lambda: self.server.state.get("ESP1") is None),
            "a registered peer without heartbeats remained online",
        )

        replacement, replacement_reply = self.connect_peer("ESP1")
        self.assertEqual(replacement_reply, "OK REGISTERED ESP1")
        silent_client.close()
        replacement.close()

    def test_periodic_ping_keeps_peer_online(self) -> None:
        client, reply = self.connect_peer("ESP2")
        self.assertEqual(reply, "OK REGISTERED ESP2")

        deadline = time.monotonic() + 0.9
        while time.monotonic() < deadline:
            client.sendall(b"PING\n")
            self.assertEqual(self.receive_line(client), "OK PONG")
            time.sleep(0.08)

        self.assertIsNotNone(self.server.state.get("ESP2"))

    def test_two_distinct_master_ids_can_register_together(self) -> None:
        master1, reply1 = self.connect_peer("master1", "MASTER")
        master2, reply2 = self.connect_peer("master2", "MASTER")

        self.assertEqual(reply1, "OK REGISTERED master1")
        self.assertEqual(reply2, "OK REGISTERED master2")
        self.assertEqual(
            self.server.state.connected_ids(),
            ("master1", "master2"),
        )
        master1.close()
        master2.close()

    def test_legacy_unnumbered_master_id_is_rejected(self) -> None:
        master, reply = self.connect_peer("master", "MASTER")

        self.assertEqual(reply, "ERR INVALID_ID")
        self.assertIsNone(self.server.state.get("master"))
        master.close()

    def test_both_sides_slave1_can_register_and_receive_separate_requests(self) -> None:
        left, left_reply = self.connect_peer("m1-s1", "SLAVE")
        right, right_reply = self.connect_peer("m2-s1", "SLAVE")
        self.assertEqual(left_reply, "OK REGISTERED m1-s1")
        self.assertEqual(right_reply, "OK REGISTERED m2-s1")
        self.server.state.send_from_controller("m1-s1", "L1", "PING")
        self.server.state.send_from_controller("m2-s1", "R1", "STATUS")
        self.assertEqual(self.receive_line(left), "FROM GUI L1 PING")
        self.assertEqual(self.receive_line(right), "FROM GUI R1 STATUS")

    def test_tenth_slave_on_each_side_can_register(self) -> None:
        for peer_id in ("m1-s10", "m2-s10"):
            with self.subTest(peer_id=peer_id):
                client, reply = self.connect_peer(peer_id, "SLAVE")
                self.assertEqual(reply, f"OK REGISTERED {peer_id}")
                client.close()

    def test_out_of_range_scoped_slave_ids_are_rejected(self) -> None:
        for peer_id in ("m0-s1", "m3-s1", "m1-s0", "m1-s11", "m1-s01"):
            with self.subTest(peer_id=peer_id):
                client, reply = self.connect_peer(peer_id, "SLAVE")
                self.assertEqual(reply, "ERR INVALID_ID")
                client.close()

    def test_legacy_slave_debug_id_remains_compatible(self) -> None:
        client, reply = self.connect_peer("slave1", "SLAVE")
        self.assertEqual(reply, "OK REGISTERED slave1")
        client.close()

    def test_topology_result_is_consumed_without_gui_polling(self) -> None:
        delivered: queue.Queue[tuple[str, str, str, int]] = queue.Queue()

        def consume(target_id, request_id, payload):
            delivered.put((target_id, request_id, payload, threading.get_ident()))
            return request_id == "TOPO-1"

        self.server.state = gui.RouterState(self.events, on_result=consume)
        client, reply = self.connect_peer("master1", "MASTER")
        self.assertEqual(reply, "OK REGISTERED master1")
        client.sendall(b"RESULT master1 GUI TOPO-1 TOPO_SAMPLE 7 0 0 OK MEASURE resistance=1.000 raw=1000 range=2\n")

        target_id, request_id, payload, callback_thread = delivered.get(timeout=1)
        self.assertEqual((target_id, request_id), ("master1", "TOPO-1"))
        self.assertTrue(payload.startswith("TOPO_SAMPLE 7 0 0"))
        self.assertNotEqual(callback_thread, threading.get_ident())
        self.assertFalse(any(kind == "result_frame" for kind, _ in list(self.events.queue)))

        client.sendall(b"RESULT master1 GUI LEGACY-1 OK RESET\n")
        self.assertEqual(delivered.get(timeout=1)[:3], ("master1", "LEGACY-1", "OK RESET"))
        self.assertTrue(self.wait_until(lambda: any(
            kind == "result_frame" and value == ("master1", "LEGACY-1", "OK RESET")
            for kind, value in list(self.events.queue)
        )))

    def test_transport_reports_heartbeat_expiry_with_last_received_frame(self) -> None:
        transport: list[dict] = []
        self.server.state = gui.RouterState(self.events, on_transport=transport.append)
        client, _ = self.connect_peer("master1", "MASTER")
        client.sendall(b"PING\n")
        self.assertEqual(self.receive_line(client), "OK PONG")
        self.assertTrue(self.wait_until(lambda: any(
            event["event"] == "disconnected" for event in transport
        )))

        self.assertEqual([event["event"] for event in transport], ["connected", "heartbeat", "disconnected"])
        disconnected = transport[-1]
        self.assertEqual(disconnected["reason"], "heartbeat_timeout")
        self.assertEqual(disconnected["peer_id"], "master1")
        self.assertEqual(disconnected["last_rx_frame"], "PING")
        self.assertGreaterEqual(disconnected["last_rx_age_seconds"], 0.2)
        self.assertEqual(disconnected["address"][0], "127.0.0.1")

    def test_peer_eof_is_reported_as_peer_closed(self) -> None:
        transport: list[dict] = []
        self.server.state = gui.RouterState(self.events, on_transport=transport.append)
        client, _ = self.connect_peer("master1", "MASTER")
        client.shutdown(socket.SHUT_WR)
        self.assertTrue(self.wait_until(lambda: any(
            event["event"] == "disconnected" for event in transport
        )))
        self.assertEqual(transport[-1]["reason"], "peer_closed")

    def test_split_frame_delayed_within_heartbeat_timeout_stays_connected(self) -> None:
        transport: list[dict] = []
        self.server.state = gui.RouterState(self.events, on_transport=transport.append)
        client, _ = self.connect_peer("master1", "MASTER")
        for _ in range(4):
            client.sendall(b"PI")
            time.sleep(0.08)
            client.sendall(b"NG\n")
            self.assertEqual(self.receive_line(client), "OK PONG")
            time.sleep(0.03)
        self.assertIsNotNone(self.server.state.get("master1"))
        self.assertEqual(sum(event["event"] == "heartbeat" for event in transport), 4)
        self.assertFalse(any(event["event"] == "disconnected" for event in transport))


class RouterCallbackTests(unittest.TestCase):
    """Check callback isolation and transport facts without real hardware."""

    def test_callback_failure_preserves_legacy_result_delivery(self) -> None:
        events: queue.Queue[tuple[str, object]] = queue.Queue()
        state = gui.RouterState(events, on_result=mock.Mock(side_effect=RuntimeError("consumer failure")))
        target = mock.Mock(peer_id="master1")
        self.assertIsNone(state.route_result(target, "master1", "GUI", "R1", "OK RESET"))
        self.assertIn(("result_frame", ("master1", "R1", "OK RESET")), list(events.queue))

    def test_peer_requests_and_results_keep_original_request_identity(self) -> None:
        events: queue.Queue[tuple[str, object]] = queue.Queue()
        transport: list[dict] = []
        state = gui.RouterState(events, on_transport=transport.append)
        first = gui.Peer("master1", "MASTER", mock.Mock(), ("127.0.0.1", 1001))
        second = gui.Peer("master2", "MASTER", mock.Mock(), ("127.0.0.1", 1002))
        state.register(first)
        state.register(second)
        self.assertEqual(state.route_send(first, "master1", "master2", "P1", "TOPO_PREPARE 7"), "OK FORWARDED master2 P1")
        self.assertIsNone(state.route_result(second, "master2", "master1", "P1", "OK TOPO_PREPARE"))
        self.assertEqual(state.send_from_controller("master1", "G1", "TOPO_RUN 7"), "OK FORWARDED master1 G1")
        commands = [event for event in transport if event["event"] in {"request", "result"}]
        self.assertEqual([(event["event"], event["source_id"], event["target_id"], event["request_id"], event["payload"]) for event in commands], [
            ("request", "master1", "master2", "P1", "TOPO_PREPARE 7"),
            ("result", "master1", "master2", "P1", "OK TOPO_PREPARE"),
            ("request", "GUI", "master1", "G1", "TOPO_RUN 7"),
        ])

    def test_failed_delivery_records_connection_error_and_unregisters(self) -> None:
        transport: list[dict] = []
        state = gui.RouterState(queue.Queue(), on_transport=transport.append)
        connection = mock.Mock()
        connection.sendall.side_effect = ConnectionResetError("link reset")
        peer = gui.Peer("master1", "MASTER", connection, ("127.0.0.1", 1001))
        state.register(peer)
        self.assertEqual(state.send_from_controller("master1", "G1", "TOPO_RUN 7"), "ERR DELIVERY_FAILED master1 G1")
        self.assertIsNone(state.get("master1"))
        self.assertEqual(transport[-1]["reason"], "delivery_failed")
        self.assertIn("link reset", transport[-1]["detail"])

    def test_socket_error_is_not_misreported_as_heartbeat_timeout(self) -> None:
        transport: list[dict] = []
        state = gui.RouterState(queue.Queue(), on_transport=transport.append)
        handler = gui.RouterRequestHandler.__new__(gui.RouterRequestHandler)
        handler.request = mock.Mock()
        handler.client_address = ("127.0.0.1", 1001)
        handler.server = mock.Mock(state=state)
        handler.rfile = mock.Mock()
        handler.rfile.readline.side_effect = [b"HELLO MASTER master1\n", ConnectionResetError("connection reset")]
        handler.handle()
        self.assertEqual(transport[-1]["reason"], "socket_error")
        self.assertIn("connection reset", transport[-1]["detail"])

    def test_pong_send_timeout_is_socket_error(self) -> None:
        transport: list[dict] = []
        state = gui.RouterState(queue.Queue(), on_transport=transport.append)
        handler = gui.RouterRequestHandler.__new__(gui.RouterRequestHandler)
        handler.request = mock.Mock()
        handler.request.sendall.side_effect = [None, TimeoutError("write timed out")]
        handler.client_address = ("127.0.0.1", 1001)
        handler.server = mock.Mock(state=state)
        handler.rfile = mock.Mock()
        handler.rfile.readline.side_effect = [b"HELLO MASTER master1\n", b"PING\n"]
        handler.handle()
        self.assertEqual(transport[-1]["reason"], "socket_error")
        self.assertEqual(transport[-1]["last_rx_frame"], "PING")

    def test_failed_registration_reply_does_not_leave_registered_peer(self) -> None:
        transport: list[dict] = []
        state = gui.RouterState(queue.Queue(), on_transport=transport.append)
        handler = gui.RouterRequestHandler.__new__(gui.RouterRequestHandler)
        handler.request = mock.Mock()
        handler.request.sendall.side_effect = TimeoutError("register reply timed out")
        handler.client_address = ("127.0.0.1", 1001)
        handler.server = mock.Mock(state=state)
        handler.rfile = mock.Mock()
        handler.rfile.readline.return_value = b"HELLO MASTER master1\n"
        handler.handle()
        self.assertIsNone(state.get("master1"))
        self.assertEqual(transport[-1]["reason"], "socket_error")

    def test_service_passes_callbacks_to_network_handler_and_records_stop(self) -> None:
        delivered: queue.Queue[tuple[str, str, str]] = queue.Queue()
        transport: list[dict] = []
        service = gui.RouterService(
            queue.Queue(),
            on_result=lambda target, request, payload: (delivered.put((target, request, payload)) or True),
            on_transport=transport.append,
        )
        client = None
        try:
            service.start("127.0.0.1", 0)
            self.assertTrue(service.running)
            client = socket.create_connection(service._server.server_address, timeout=1)
            client.sendall(b"HELLO MASTER master1\n")
            self.assertEqual(RouterHeartbeatTests.receive_line(client), "OK REGISTERED master1")
            self.assertEqual(service.send_from_controller("master1", "G1", "TOPO_RUN 7"), "OK FORWARDED master1 G1")
            self.assertEqual(RouterHeartbeatTests.receive_line(client), "FROM GUI G1 TOPO_RUN 7")
            client.sendall(b"RESULT master1 GUI G1 TOPO_DONE 7 0\n")
            self.assertEqual(delivered.get(timeout=1), ("master1", "G1", "TOPO_DONE 7 0"))
            service.stop()
            self.assertEqual(transport[-1]["reason"], "server_stopped")
            self.assertEqual(transport[0]["heartbeat_timeout_seconds"], gui.HEARTBEAT_TIMEOUT_SECONDS)
        finally:
            service.stop()
            if client is not None:
                client.close()

    def test_server_stop_records_reason_without_holding_peer_map_lock(self) -> None:
        transport: list[dict] = []
        state = gui.RouterState(queue.Queue(), on_transport=lambda event: (state.connected_ids(), transport.append(event)))
        peer = gui.Peer("master1", "MASTER", mock.Mock(), ("127.0.0.1", 1001))
        state.register(peer)
        state.close_all()
        state.unregister(peer)
        self.assertEqual([event["event"] for event in transport], ["connected", "disconnected"])
        self.assertEqual(transport[-1]["reason"], "server_stopped")


if __name__ == "__main__":
    unittest.main()
