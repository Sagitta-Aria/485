import unittest
from unittest import mock

import cable_tester_gui as gui


class _Value:
    def __init__(self, value: str) -> None:
        self.value = value

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value


class _Selector:
    def __init__(self) -> None:
        self.values: tuple[str, ...] = ()

    def configure(self, *, values: tuple[str, ...]) -> None:
        self.values = values


class ModeRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        # Avoid creating a Tk window; _map_mode_request only needs the active role.
        self.app = gui.CableTesterApp.__new__(gui.CableTesterApp)
        self.app.master_id = _Value("master1")

    def test_master_wraps_matrix_commands_for_selected_slave(self) -> None:
        self.app._active_mode = "master"
        self.assertEqual(
            self.app._map_mode_request("slave2", "SWITCH S1 0 Y4 ON"),
            ("master1", "BUS slave2 SWITCH S1 0 Y4 ON"),
        )

    def test_master_maps_broadcast_without_waiting_for_a_slave_target(self) -> None:
        self.app._active_mode = "master"
        self.assertEqual(
            self.app._map_mode_request("broadcast", "RESET"),
            ("master1", "BUS broadcast RESET"),
        )

    def test_master_target_routes_matrix_command_directly(self) -> None:
        self.app._active_mode = "master"
        self.assertEqual(
            self.app._map_mode_request("master1", "SWITCH S1 0 Y4 ON"),
            ("master1", "SWITCH S1 0 Y4 ON"),
        )

    def test_master_target_is_available_in_master_mode(self) -> None:
        self.app._active_mode = "master"
        self.app._online_slave_ids = ("slave2",)
        self.assertEqual(
            self.app._available_target_options(),
            (
                "master1",
                "slave1",
                "slave2",
                "slave3",
                "slave4",
                "slave5",
                "slave6",
                "slave7",
                "slave8",
                "slave9",
                "slave10",
                gui.BROADCAST_TARGET,
            ),
        )

    def test_master_mode_keeps_legacy_slave_ids_available_without_wifi(self) -> None:
        self.app._active_mode = "master"
        self.app._online_slave_ids = ()
        self.app.target_id = _Value("slave1")
        self.app.target_selector = _Selector()

        self.app._refresh_target_selector()

        self.assertEqual(self.app.target_id.get(), "slave1")
        self.assertEqual(self.app.target_selector.values[0], "master1")

    def test_master_keeps_local_commands_on_master(self) -> None:
        self.app._active_mode = "master"
        self.assertEqual(
            self.app._map_mode_request("slave2", "HELP"),
            ("master1", "HELP"),
        )
        self.assertEqual(
            self.app._map_mode_request("slave2", "MEASURE"),
            ("master1", "MEASURE"),
        )

    def test_same_slave_id_routes_through_selected_master(self) -> None:
        self.app._active_mode = "master"
        self.assertEqual(
            self.app._logical_target_id("slave1"),
            "master1-slave1",
        )
        self.app.master_id.set("master2")
        self.assertEqual(
            self.app._logical_target_id("slave1"),
            "master2-slave1",
        )
        self.assertEqual(
            self.app._map_mode_request("master2-slave1", "RESET"),
            ("master2", "BUS slave1 RESET"),
        )

    def test_scoped_request_keeps_its_master_after_ui_switches(self) -> None:
        self.app._active_mode = "slave"
        self.app.master_id.set("master2")

        self.assertEqual(
            self.app._map_mode_request("master1-slave2", "MEASURE"),
            ("master1", "MEASURE"),
        )

    def test_same_slave_requests_keep_independent_master_routes(self) -> None:
        self.app._active_mode = "master"
        self.app._request_routes = {}
        self.app.router = mock.Mock()
        self.app.router.send_from_controller.return_value = (
            "OK FORWARDED master1 R1"
        )

        self.app._send_via_mode("master1-slave1", "R1", "RESET")
        self.app.router.send_from_controller.return_value = (
            "OK FORWARDED master2 R2"
        )
        self.app._send_via_mode("master2-slave1", "R2", "RESET")

        self.assertEqual(
            self.app.router.send_from_controller.call_args_list,
            [
                mock.call("master1", "R1", "BUS slave1 RESET"),
                mock.call("master2", "R2", "BUS slave1 RESET"),
            ],
        )
        self.assertEqual(
            self.app._logical_result_target("master1", "R1"),
            "master1-slave1",
        )
        self.assertEqual(
            self.app._logical_result_target("master2", "R2"),
            "master2-slave1",
        )

    def test_slave_sends_matrix_command_directly(self) -> None:
        self.app._active_mode = "slave"
        self.assertEqual(
            self.app._map_mode_request("slave3", "SWITCH S2 1 Y0 OFF"),
            ("slave3", "SWITCH S2 1 Y0 OFF"),
        )

    def test_scoped_wifi_debug_targets_stay_direct_after_mode_switch(self) -> None:
        for mode in ("slave", "master"):
            self.app._active_mode = mode
            for target in ("m1-s1", "m2-s1", "m2-s10"):
                with self.subTest(mode=mode, target=target):
                    self.assertEqual(self.app._logical_target_id(target), target)
                    self.assertEqual(
                        self.app._map_mode_request(target, "PING"), (target, "PING")
                    )

    def test_wifi_debug_and_bus_requests_do_not_share_route_keys(self) -> None:
        self.app._active_mode = "slave"
        self.app._request_routes = {}
        self.app.router = mock.Mock()
        self.app.router.send_from_controller.return_value = "OK FORWARDED target R1"
        for target in ("m1-s1", "m2-s1", "master1-slave1", "master2-slave1"):
            self.app._send_via_mode(target, "R1", "PING")
        self.assertEqual(self.app.router.send_from_controller.call_args_list, [
            mock.call("m1-s1", "R1", "PING"),
            mock.call("m2-s1", "R1", "PING"),
            mock.call("master1", "R1", "BUS slave1 PING"),
            mock.call("master2", "R1", "BUS slave1 PING"),
        ])
        for transport, logical in (("m1-s1", "m1-s1"), ("m2-s1", "m2-s1"),
                                   ("master1", "master1-slave1"), ("master2", "master2-slave1")):
            self.assertEqual(self.app._logical_result_target(transport, "R1"), logical)

    def test_wifi_debug_target_cannot_measure_even_in_master_mode(self) -> None:
        self.app._active_mode = "master"
        self.app.router = mock.Mock()
        self.app._request_routes = {}
        self.assertEqual(self.app._send_via_mode("m2-s1", "R1", "MEASURE"),
                         "ERR MEASURE UNSUPPORTED_ON_SLAVE")
        self.app.router.send_from_controller.assert_not_called()

    def test_slave_mode_lists_all_twenty_scoped_wifi_ids(self) -> None:
        self.app._active_mode = "slave"
        self.app._online_slave_ids = ()
        options = self.app._available_target_options()
        for side in (1, 2):
            for index in range(1, 11):
                self.assertIn(f"m{side}-s{index}", options)

    def test_slave_mode_lists_online_scoped_wifi_ids(self) -> None:
        self.app._active_mode = "slave"
        self.app._online_slave_ids = ("m1-s1", "m2-s1")
        self.assertEqual(self.app._available_target_options(), ("m1-s1", "m2-s1"))

    def test_slave_mode_keeps_dual_node_closure_available(self) -> None:
        self.app._active_mode = "slave"
        self.app.measure_button = mock.Mock()
        self.app.batch_calibration_button = mock.Mock()
        self.app.auxiliary_test_button = mock.Mock()
        self.app.sequential_test_button = mock.Mock()
        self.app.pairwise_test_button = mock.Mock()
        self.app.dual_node_measurement_button = mock.Mock()

        self.app._apply_mode_state()

        for button in (
            self.app.measure_button,
            self.app.batch_calibration_button,
            self.app.auxiliary_test_button,
            self.app.sequential_test_button,
            self.app.pairwise_test_button,
        ):
            button.state.assert_called_once_with(["disabled"])
        self.app.dual_node_measurement_button.state.assert_not_called()

    def test_slave_mode_restores_dual_node_without_enabling_measurement(self) -> None:
        self.app._active_mode = "slave"
        self.app.measure_button = mock.Mock()
        self.app.batch_calibration_button = mock.Mock()
        self.app.auxiliary_test_button = mock.Mock()
        self.app.sequential_test_button = mock.Mock()
        self.app.pairwise_test_button = mock.Mock()
        self.app.dual_node_measurement_button = mock.Mock()

        self.app._restore_controls_after_dual_node()

        self.app.dual_node_measurement_button.state.assert_called_once_with(
            ["!disabled"]
        )
        self.app.measure_button.state.assert_called_once_with(["disabled"])

    def test_status_payload_decodes_each_bank_and_crosspoint(self) -> None:
        s1 = bytearray(15)
        s1[0] |= 1 << 0       # Y0/X0
        s1[14] |= 1 << 7      # Y4/X23
        s2 = bytearray(15)
        s2[3] |= 1 << 2       # Y1/X2
        status = gui.parse_status_payload(
            f"OK STATUS S1 {s1.hex().upper()} S2 {s2.hex().upper()}"
        )

        self.assertIsNotNone(status)
        assert status is not None
        self.assertTrue(status["S1"][0][0])
        self.assertTrue(status["S1"][4][23])
        self.assertTrue(status["S2"][1][2])
        self.assertFalse(status["S1"][0][1])

    def test_matrix_cell_tag_round_trips_coordinate(self) -> None:
        tag = gui.matrix_cell_tag("S2", 23, 4)

        self.assertEqual(
            gui.parse_matrix_cell_tags(("closed", tag)),
            ("S2", 23, 4),
        )

    def _interactive_status_app(
        self, target_id: str, bank: str, x: int, y: int, state: bool | None
    ) -> tuple[gui.CableTesterApp, mock.Mock, mock.Mock]:
        app = gui.CableTesterApp.__new__(gui.CableTesterApp)
        app.matrix_status_target = _Value(target_id)
        app.matrix_status_summary = mock.Mock()
        app._matrix_selected_cell = None
        app._append_log = mock.Mock()
        app._send = mock.Mock()
        view = mock.Mock()
        view.index.return_value = "2.3"
        view.tag_names.return_value = (gui.matrix_cell_tag(bank, x, y),)
        view.tag_ranges.return_value = ("2.3", "2.5")
        app._status_views = {bank: view}
        if state is None:
            app._matrix_status = {}
        else:
            rows = [[False for _x in range(24)] for _y in range(5)]
            rows[y][x] = state
            app._matrix_status = {
                target_id: (
                    "12:00:00",
                    {bank: tuple(tuple(row) for row in rows)},
                )
            }
        event = mock.Mock(x=10, y=10)
        return app, view, event

    def test_double_click_opens_a_closed_matrix_cell(self) -> None:
        app, view, event = self._interactive_status_app(
            "slave1", "S1", 3, 2, True
        )

        result = app._toggle_matrix_cell(view, event)

        self.assertEqual(result, "break")
        app._send.assert_called_once_with(
            "SWITCH S1 3 Y2 OFF", target_id="slave1"
        )

    def test_double_click_closes_an_open_matrix_cell(self) -> None:
        app, view, event = self._interactive_status_app(
            "slave2", "S2", 7, 4, False
        )

        app._toggle_matrix_cell(view, event)

        app._send.assert_called_once_with(
            "SWITCH S2 7 Y4 ON", target_id="slave2"
        )

    def test_double_click_unknown_cell_refreshes_without_switching(self) -> None:
        app, view, event = self._interactive_status_app(
            "slave1", "S1", 0, 0, None
        )

        app._toggle_matrix_cell(view, event)

        app._send.assert_called_once_with("STATUS", target_id="slave1")

    def test_master_s2_cell_is_not_actionable(self) -> None:
        app, view, event = self._interactive_status_app(
            "master1", "S2", 0, 0, None
        )
        app._active_mode = "master"
        app.master_id = _Value("master1")

        app._toggle_matrix_cell(view, event)

        app._send.assert_not_called()
        app._append_log.assert_called_once_with(
            "错误", "master1 只配置了 S1 矩阵，不能操作 S2"
        )


if __name__ == "__main__":
    unittest.main()
