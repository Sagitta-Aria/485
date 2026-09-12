"""Guard the real router paths against the measured master1 Kelvin bypass."""

import queue
import unittest
from unittest import mock

from cable_tester.ui.gui import Peer, RouterState
from cable_tester.worker.dual_node_measurement import build_dual_node_spec


class MasterFixedRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = RouterState(queue.Queue())
        self.master = Peer("master1", "MASTER", mock.Mock(), ("127.0.0.1", 1))
        self.source = Peer("probe", "NODE", mock.Mock(), ("127.0.0.1", 2))
        self.state.register(self.master)

    def test_dual_node_commands_cannot_bridge_fixed_instrument_leads(self) -> None:
        commands = build_dual_node_spec("S1", 0, 1, "S1", 2, 3).switch_commands
        for index, command in enumerate(commands):
            with self.subTest(command=command):
                reply = self.state.send_from_controller("master1", f"R{index}", command)
                self.assertTrue(reply.startswith("ERR FIXED_KELVIN "), reply)
        self.master.connection.sendall.assert_not_called()

    def test_network_client_cannot_bypass_the_same_guard(self) -> None:
        for command in ("SWITCH S1 0 Y4 ON", "switch s1 2 y0 on",
                        "SWITCH S1 1 Y1 OFF", "CONNECT S1 0 S1 4",
                        "CONNECT S1 4 S1 3"):
            with self.subTest(command=command):
                reply = self.state.route_send(self.source, "probe", "master1", "R1", command)
                self.assertTrue(reply.startswith("ERR FIXED_KELVIN "), reply)
        self.master.connection.sendall.assert_not_called()

    def test_cleanup_fixed_route_and_slave_commands_are_still_routed(self) -> None:
        for command in ("RESET", "MEASURE", "SWITCH S1 0 Y0 ON",
                        "SWITCH S1 0 Y3 OFF", "SWITCH S1 8 Y4 ON",
                        "CONNECT S1 4 S1 5", "BUS slave1 SWITCH S1 0 Y3 ON",
                        "BUS slave1 CONNECT S1 0 S1 2"):
            with self.subTest(command=command):
                self.assertEqual(self.state.send_from_controller("master1", "R1", command),
                                 "OK FORWARDED master1 R1")
                self.master.connection.sendall.assert_called_with(
                    f"FROM GUI R1 {command}\n".encode())

    def test_second_master_and_debug_slave_keep_their_own_matrix_policy(self) -> None:
        for target in ("master2", "m1-s1", "m2-s1"):
            peer = Peer(target, "NODE", mock.Mock(), ("127.0.0.1", 3))
            self.state.register(peer)
            self.assertEqual(self.state.send_from_controller(target, "R1", "SWITCH S1 0 Y3 ON"),
                             f"OK FORWARDED {target} R1")
            peer.connection.sendall.assert_called_once()
