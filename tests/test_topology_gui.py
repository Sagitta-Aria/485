"""GUI boundary tests for topology routing, ownership, and expandable plans."""

from __future__ import annotations

import queue
import tempfile
import tkinter as tk
from tkinter import ttk
import unittest
from pathlib import Path
from unittest import mock

import cable_tester_gui as gui
from topology_panel import TopologyPanel, read_expected_mapping
from topology_scan import TopologyPort, classify_measurement


class TopologyRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = gui.CableTesterApp.__new__(gui.CableTesterApp)
        self.app.root = mock.Mock()
        self.app.status = mock.Mock()
        self.app.router = mock.Mock(running=True)
        self.app._active_mode = "master"
        self.app._topology_owned = False
        self.app._shutdown_pending = False
        self.app._online_master_ids = ("master1", "master2")
        self.app._pending_commands = {}
        self.app._request_routes = {}
        self.app._active_routes = {}
        self.app._matrix_status = {}
        self.app.matrix_status_target = mock.Mock()
        self.app._render_matrix_status = mock.Mock()
        self.app._append_log = mock.Mock()
        self.app._set_topology_interlock = mock.Mock()
        self.controller_names = (
            "batch_calibration", "auxiliary_loop_test", "sequential_loop_test",
            "pairwise_resistance_test", "dual_node_measurement", "topology_scan",
        )
        for name in self.controller_names:
            controller = mock.Mock(running=False)
            controller.feed_result.return_value = False
            controller.start.return_value = True
            setattr(self.app, name, controller)

    def test_all_legacy_entrypoints_reject_topology_owner(self) -> None:
        self.app._topology_owned = True
        self.app._send("RESET")
        for method in (
            "_run_batch_calibration", "_run_auxiliary_loop_test",
            "_toggle_sequential_loop_test", "_toggle_pairwise_resistance_test",
            "_run_dual_node_measurement",
        ):
            getattr(self.app, method)()
        for name in self.controller_names:
            getattr(self.app, name).start.assert_not_called()
        self.app.router.send_from_controller.assert_not_called()
        self.assertEqual(self.app._append_log.call_count, 6)

    def test_delayed_manual_worker_cannot_bypass_topology_interlock(self) -> None:
        self.app.topology_scan.running = True
        acknowledgement = self.app._send_via_mode("master1-slave1", "GUI-3", "RESET")
        self.assertEqual(acknowledgement, "ERR TOPOLOGY_BUSY")
        self.app.router.send_from_controller.assert_not_called()

    def test_topology_rejects_every_other_running_controller(self) -> None:
        for name in self.controller_names[:-1]:
            with self.subTest(controller=name):
                getattr(self.app, name).running = True
                self.assertFalse(self.app._start_topology_scan(left_master="master1", right_master="master2"))
                getattr(self.app, name).running = False
        self.app.topology_scan.start.assert_not_called()

    def test_topology_waits_for_pending_manual_result(self) -> None:
        self.app._pending_commands[("master1-slave7", "GUI-17")] = "SWITCH S1 0 Y4 ON"
        self.assertFalse(self.app._start_topology_scan(left_master="master1", right_master="master2"))
        self.app.topology_scan.start.assert_not_called()

    def test_topology_accepts_asymmetric_module_counts_and_clears_cached_routes(self) -> None:
        self.app._active_routes["master1-slave1"] = ("S1_X0", "S2_X0")
        self.assertTrue(self.app._start_topology_scan(left_master="master1", right_master="master2", left_modules=7, right_modules=10))
        self.app.topology_scan.start.assert_called_once_with(left_master="master1", right_master="master2", left_modules=7, right_modules=10)
        self.assertTrue(self.app._topology_owned)
        self.assertFalse(self.app._active_routes)
        self.app._set_topology_interlock.assert_called_once_with(True)

    def test_recheck_uses_the_same_hardware_interlock_and_forwards_saved_session(self) -> None:
        parameters = dict(left_master="master1", right_master="master2", recheck_session_id=71)
        self.app.pairwise_resistance_test.running = True
        self.assertFalse(self.app._start_topology_scan(**parameters))
        self.app.topology_scan.start.assert_not_called()
        self.app.pairwise_resistance_test.running = False
        self.assertTrue(self.app._start_topology_scan(**parameters))
        self.app.topology_scan.start.assert_called_once_with(**parameters)
        self.assertTrue(self.app._topology_owned)

    def test_offline_master_does_not_receive_scan_start(self) -> None:
        self.app._online_master_ids = ("master1",)
        self.assertFalse(self.app._start_topology_scan(left_master="master1", right_master="master2"))
        self.app.topology_scan.start.assert_not_called()

    def test_invalid_controller_plan_releases_owner(self) -> None:
        self.app.topology_scan.start.side_effect = ValueError("bad count")
        self.assertFalse(self.app._start_topology_scan(left_master="master1", right_master="master2", left_modules=11))
        self.assertFalse(self.app._topology_owned)

    def test_topology_frames_bypass_legacy_controllers_and_display_calibration(self) -> None:
        self.app.topology_scan.feed_result.return_value = True
        payload = "OK TOPO_SAMPLE session 0 0 OK MEASURE resistance=3.000 raw=3000 range=2"
        self.app._handle_result_frame("master1", "TOPO-1", payload)
        self.app.topology_scan.feed_result.assert_called_once_with("master1", "TOPO-1", payload)
        for name in self.controller_names[:-1]:
            getattr(self.app, name).feed_result.assert_not_called()
        self.app._append_log.assert_not_called()

    def test_poll_preserves_master_id_for_streamed_topology_samples(self) -> None:
        self.app.topology_scan.feed_result.return_value = True
        self.app.events = queue.Queue()
        self.app.events.put(("result_frame", ("master2", "TOPO-9", "OK TOPO_DONE session count=168")))
        self.app._poll_events()
        self.app.topology_scan.feed_result.assert_called_once_with("master2", "TOPO-9", "OK TOPO_DONE session count=168")

    def test_server_stop_waits_for_topology_cleanup(self) -> None:
        self.app.topology_scan.running = True
        with mock.patch("cable_tester_gui.threading.Thread") as worker:
            self.app._stop_server()
            self.app.topology_scan.cancel.assert_called_once()
            worker.assert_not_called()
            self.app.router.stop.assert_not_called()
            self.app.root.after.assert_called_with(50, self.app._finish_server_stop)
            self.app.topology_scan.running = False
            self.app._finish_server_stop()
            worker.assert_called_once()

    def test_window_close_retains_event_loop_until_cleanup_completes(self) -> None:
        self.app.topology_scan.running = True
        self.app._close()
        self.app.topology_scan.cancel.assert_called_once()
        self.app.router.stop.assert_not_called()
        self.app.root.destroy.assert_not_called()
        self.app.topology_scan.running = False
        self.app._finish_close()
        self.app.router.stop.assert_called_once()
        self.app.root.destroy.assert_called_once()

    def test_terminal_event_releases_owner_and_reports_unconfirmed_cleanup(self) -> None:
        self.app._topology_owned = True
        self.app._handle_topology_event("topology_stopped", {"completed": 2, "total": 168, "measurements": 20, "cleanup_errors": ["master2 timeout"]})
        self.assertFalse(self.app._topology_owned)
        self.app._set_topology_interlock.assert_called_once_with(False)
        self.assertTrue(any("master2 timeout" in str(call) for call in self.app._append_log.call_args_list))


class ExpectedMappingTests(unittest.TestCase):
    def test_map_import_keeps_slave10_and_group24(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "expected.csv"
            path.write_text("source,target\nslave7-G24,slave10-G24\n", encoding="utf-8")
            self.assertEqual(read_expected_mapping(path, 7, 10), {TopologyPort(6, 23): TopologyPort(9, 23)})

    def test_map_rejects_duplicates_and_out_of_plan_ports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "expected.csv"
            for contents in (
                "source,target\nslave1-G1,slave1-G1\nslave1-G1,slave1-G2\n",
                "source,target\nslave2-G1,slave1-G1\n",
                "source,target\nslave1-G0,slave1-G1\n",
                "source,target\nslave1-G1,slave11-G1\n",
            ):
                with self.subTest(contents=contents):
                    path.write_text(contents, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        read_expected_mapping(path, 1, 10)


class TopologyPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.root = tk.Tk()
        except tk.TclError as error:
            raise unittest.SkipTest(f"Tk display unavailable: {error}") from error
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.root.destroy()

    def setUp(self) -> None:
        self.start = mock.Mock(return_value=True)
        self.panel = TopologyPanel(self.root, self.start, mock.Mock())
        self.panel.window.withdraw()
        self.panel.set_devices(("master1", "master2"))

    def tearDown(self) -> None:
        self.panel.window.destroy()

    def test_defaults_and_asymmetric_plan_are_passed_to_controller(self) -> None:
        self.assertIn("1680", self.panel.plan_summary.get())
        self.panel.right_modules.set("10")
        self.assertIn("240", self.panel.plan_summary.get())
        self.panel._start()
        kwargs = self.start.call_args.kwargs
        self.assertEqual((kwargs["left_modules"], kwargs["right_modules"]), (7, 10))
        self.assertEqual(kwargs["binary_repeats"], 1)
        self.assertEqual(len(kwargs["expected_mapping"]), 168)
        self.assertTrue(self.panel.start_button.instate(["disabled"]))

    def test_cancelled_unmeasured_rows_stay_unknown(self) -> None:
        self.panel.handle_event("topology_started", {"left_ports": 24, "total": 168})
        self.panel.handle_event("topology_row", {"source": "slave1-G1", "status": "UNIQUE", "targets": ["slave1-G1"], "signature": "1110000"})
        self.panel.handle_event("topology_stopped", {"completed": 1, "total": 24, "measurements": 7, "rows": []})
        self.assertEqual(len(self.panel.table.get_children()), 24)
        self.assertEqual(self.panel.table.item("slave1-G2", "values")[1], "未确定")
        self.assertTrue(self.panel.start_button.instate(["!disabled"]))

    def test_recheck_button_reuses_saved_parameters_and_restores_displayed_plan(self) -> None:
        self.assertTrue(self.panel.recheck_button.instate(["disabled"]))
        self.panel.left_modules.set("1")
        self.panel.right_modules.set("2")
        self.panel._start()
        parameters = dict(self.start.call_args.kwargs, recheck_session_id=123)
        self.panel.handle_event("topology_complete", {"rows": [], "pending_recheck_count": 48,
                                                     "recheck_parameters": parameters})
        self.assertIn("48", self.panel.recheck_button.cget("text"))
        self.assertTrue(self.panel.recheck_button.instate(["!disabled"]))
        self.panel.right_modules.set("10")
        self.panel.on_threshold.set("1")
        self.panel._recheck()
        self.assertEqual(self.start.call_args.kwargs, parameters)
        self.assertEqual(self.panel.right_modules.get(), "2")
        self.assertEqual(self.panel.on_threshold.get(), "50")
        self.assertTrue(self.panel.recheck_button.instate(["disabled"]))
        self.assertTrue(self.panel.stop_button.instate(["!disabled"]))
        self.panel._recheck()
        self.assertEqual(self.start.call_count, 2)

    def test_recheck_button_follows_connectivity_and_new_scan_invalidates_old_plan(self) -> None:
        parameters = dict(left_master="master1", right_master="master2", recheck_session_id=123)
        self.panel.handle_event("topology_complete", {"rows": [], "pending_recheck_count": 24,
                                                     "recheck_parameters": parameters})
        self.panel.set_devices(("master1",))
        self.assertTrue(self.panel.recheck_button.instate(["disabled"]))
        self.panel.set_devices(("master1", "master2"))
        self.assertTrue(self.panel.recheck_button.instate(["!disabled"]))
        self.panel.handle_event("topology_started", {"left_ports": 24, "total": 168})
        self.assertTrue(self.panel.recheck_button.instate(["disabled"]))
        self.panel.handle_event("topology_complete", {"rows": []})
        self.assertTrue(self.panel.recheck_button.instate(["disabled"]))
        self.panel._recheck()
        self.start.assert_not_called()

    def test_failed_recheck_start_keeps_the_saved_result_available(self) -> None:
        parameters = dict(left_master="master1", right_master="master2", recheck_session_id=123)
        self.panel.handle_event("topology_complete", {"rows": [], "pending_recheck_count": 24,
                                                     "recheck_parameters": parameters})
        self.start.return_value = False
        self.panel._recheck()
        self.assertTrue(self.panel.recheck_button.instate(["!disabled"]))
        self.assertIn("补测未启动", self.panel.status.get())

    def test_rechecked_conflict_stays_visible_without_fake_pending_ports(self) -> None:
        row = {"source": "slave1-G1", "status": "INCONSISTENT", "targets": [],
               "candidates": [], "signature": "1000000", "point_recheck_complete": True,
               "point_recheck_measured": 24, "point_recheck_total": 24}
        self.panel.handle_event("topology_complete", {"rows": [row], "recheck": {"completed_pairs": 24},
            "completed": 24, "total": 24, "measurements": 24, "pending_recheck_count": 0})
        self.assertIn("补测完成", self.panel.status.get())
        self.assertIn("24/24 对", self.panel.status.get())
        self.assertEqual(self.panel.table.set("slave1-G1", "candidates"), "-")
        self.panel.table.selection_set("slave1-G1")
        self.panel._show_row()
        self.assertIn("编码与逐点复核结果不一致", self.panel.detail.get("1.0", "end"))
        self.assertTrue(self.panel.recheck_button.instate(["disabled"]))

    def test_binary_mode_reaches_controller_and_is_locked_while_running(self) -> None:
        self.panel.set_devices(("master1", "master2"))
        self.assertEqual(self.panel.binary_repeats.get(), "1")
        self.assertTrue(self.panel.binary_repeat_spinner.instate(["disabled"]))
        self.panel.scan_method.set("binary")
        self.assertTrue(self.panel.binary_repeat_spinner.instate(["!disabled"]))
        self.assertIn("每项 1 次", self.panel.plan_summary.get())
        self.panel.binary_repeats.set("5")
        self.assertIn("每项 5 次", self.panel.plan_summary.get())
        self.panel.expected_mode.set("仅记录")
        self.assertIn("二分扫描", self.panel.plan_summary.get())
        self.panel._start()
        self.assertEqual(self.start.call_args.kwargs["scan_method"], "binary")
        self.assertEqual(self.start.call_args.kwargs["binary_repeats"], 5)
        self.assertTrue(self.panel.binary_repeat_spinner.instate(["disabled"]))
        self.assertIsNone(self.start.call_args.kwargs["expected_mapping"])
        methods = [widget for widget in self.panel._inputs if isinstance(widget, ttk.Radiobutton)]
        self.assertEqual(len(methods), 2)
        self.assertTrue(all(widget.instate(["disabled"]) for widget in methods))
        self.panel.handle_event("topology_complete", {"scan_method": "binary", "rows": []})
        self.assertTrue(all(widget.instate(["!disabled"]) for widget in methods))
        self.assertTrue(self.panel.binary_repeat_spinner.instate(["!disabled"]))
        self.panel.scan_method.set("coded")
        self.assertTrue(self.panel.binary_repeat_spinner.instate(["disabled"]))
        self.panel.scan_method.set("binary")
        self.assertEqual(self.panel.binary_repeats.get(), "5")

    def test_invalid_binary_repeats_do_not_submit_a_scan(self) -> None:
        self.panel.scan_method.set("binary")
        with mock.patch("topology_panel.messagebox.showerror") as show_error:
            for value in ("", "0", "-1", "11", "1.5", "abc"):
                with self.subTest(value=value):
                    self.panel.binary_repeats.set(value)
                    self.assertIn("二分采样次数必须", self.panel.plan_summary.get())
                    self.panel._start()
                    self.start.assert_not_called()
            self.assertEqual(show_error.call_count, 6)

    def test_coded_scan_does_not_apply_the_binary_repeat_setting(self) -> None:
        self.panel.binary_repeats.set("5")
        self.panel._start()
        self.assertEqual(self.start.call_args.kwargs["scan_method"], "coded")
        self.assertEqual(self.start.call_args.kwargs["binary_repeats"], 1)
        self.panel.handle_event("topology_complete", {"scan_method": "coded", "rows": []})
        self.assertTrue(self.panel.binary_repeat_spinner.instate(["disabled"]))

    def test_binary_row_keeps_all_four_targets_and_has_no_fake_signature(self) -> None:
        self.panel.handle_event("topology_started", {"left_ports": 24, "total": 0, "scan_method": "binary"})
        targets = [f"slave1-G{index}" for index in (1, 6, 13, 24)]
        self.panel.handle_event("topology_row", {"source": "slave1-G1", "scan_method": "binary",
            "status": "SHORT", "targets": targets, "signature": "", "connection_resistances": [
                {"target": target, **vars(classify_measurement("OK MEASURE resistance=1.000 raw=1000 range=2"))}
                for target in targets]})
        self.assertEqual(self.panel.table.set("slave1-G1", "signature"), "二分")
        self.assertEqual(self.panel.table.heading("signature", "text"), "扫描方法")
        self.panel.table.selection_set("slave1-G1")
        self.panel._show_row()
        detail = self.panel.detail.get("1.0", "end")
        for target in targets:
            self.assertIn(target, detail)

    def test_disconnect_keeps_controls_locked_until_cleanup_finishes(self) -> None:
        self.panel.handle_event("topology_started", {"left_ports": 24, "total": 168})
        self.panel.handle_event("topology_recovering", {"error": "master1 CONNECTION_LOST"})
        self.assertIn("连接中断", self.panel.status.get())
        self.assertTrue(self.panel.start_button.instate(["disabled"]))
        self.assertTrue(self.panel.stop_button.instate(["disabled"]))
        self.panel.handle_event("topology_error", {
            "completed": 0, "total": 24, "measurements": 23,
            "error": "master1 CONNECTION_LOST reason=peer_closed; scan session invalidated",
            "connection_error": "master1 CONNECTION_LOST", "cleanup_errors": ["master1 offline"],
        })
        self.assertIn("重新扫描", self.panel.status.get())
        self.assertIn("矩阵复位未确认", self.panel.status.get())
        self.assertTrue(self.panel.start_button.instate(["!disabled"]))
        self.assertEqual(self.panel.table.item("slave1-G1", "values")[1], "未确定")

    def test_fault_filter_uses_final_miswire_and_duplicate_flags(self) -> None:
        self.panel.handle_event("topology_started", {"left_ports": 2, "total": 14})
        rows = [
            {"source": "slave1-G1", "status": "UNIQUE", "targets": ["slave1-G2"], "flags": ["MISWIRE"]},
            {"source": "slave1-G2", "status": "UNIQUE", "targets": ["slave1-G2"], "flags": [], "connection_resistances": [{"target": "slave1-G2", **vars(classify_measurement("OK MEASURE resistance=0.100 raw=100 range=2"))}]},
        ]
        self.panel.handle_event("topology_complete", {"completed": 2, "total": 2, "measurements": 14, "rows": rows})
        self.panel.anomalies_only.set(True)
        self.panel._refresh_rows()
        self.assertEqual(self.panel.table.get_children(), ("slave1-G1",))

    def test_resistance_column_and_detail_show_each_short_connection(self) -> None:
        row = {"source": "slave1-G1", "status": "SHORT", "targets": ["slave1-G1", "slave1-G2"], "connection_resistances": [
            {"target": "slave1-G1", **vars(classify_measurement("OK MEASURE resistance=25.380 raw=2538 range=1"))},
            {"target": "slave1-G2", **vars(classify_measurement("OK MEASURE resistance=0.000 raw=0 range=2"))},
        ]}
        self.panel.handle_event("topology_row", row)
        cell = self.panel.table.set("slave1-G1", "resistance")
        self.assertIn("slave1-G1: 25.380", cell)
        self.assertIn("slave1-G2: 0.000", cell)
        self.panel.table.selection_set("slave1-G1")
        self.panel._show_row()
        detail = self.panel.detail.get("1.0", "end")
        self.assertIn("slave1-G1 -> slave1-G1: 25.380 ohm", detail)
        self.assertIn("slave1-G1 -> slave1-G2: 0.000 ohm", detail)
        self.assertIn("未校准", detail)
        self.assertIn("raw=2538", detail)

    def test_pending_resistance_is_visible_in_anomaly_filter_and_refreshes_live(self) -> None:
        row = {"source": "slave1-G1", "status": "UNIQUE", "targets": ["slave1-G1"], "connection_resistances": [
            {"target": "slave1-G1", "resistance_ohm": None, "reason": "NOT_MEASURED", "bit": None},
        ]}
        self.panel.anomalies_only.set(True)
        self.panel.handle_event("topology_row", row)
        self.assertTrue(self.panel.table.exists("slave1-G1"))
        self.assertIn("未测", self.panel.table.set("slave1-G1", "resistance"))
        self.panel.anomalies_only.set(False)
        self.panel.table.selection_set("slave1-G1")
        row["connection_resistances"] = [{"target": "slave1-G1", **vars(classify_measurement("OK MEASURE resistance=25.380 raw=2538 range=1"))}]
        self.panel.handle_event("topology_row", row)
        self.assertIn("25.380 ohm", self.panel.detail.get("1.0", "end"))
        self.panel.handle_event("topology_complete", {"rows": [row]})
        self.assertEqual(self.panel.table.selection(), ("slave1-G1",))
        self.assertIn("25.380 ohm", self.panel.detail.get("1.0", "end"))
        self.panel.handle_event("topology_started", {"left_ports": 24, "total": 168})
        self.assertNotIn("25.380", self.panel.detail.get("1.0", "end"))

    def test_failed_point_reading_does_not_display_zero_or_hide_attempted_pair(self) -> None:
        cases = (
            ("ERR MEASURE TIMEOUT received=0", "测量失败"),
            ("ERR MEASURE OVERRANGE status=1", "OL"),
            ("OK MEASURE resistance=75.000 raw=7500 range=1", "75.000"),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.panel.handle_event("topology_row", {"source": "slave1-G1", "status": "UNKNOWN", "targets": [], "connection_resistances": [{"target": "slave1-G1", **vars(classify_measurement(raw))}]})
                cell = self.panel.table.set("slave1-G1", "resistance")
                self.assertIn("slave1-G1:", cell)
                self.assertIn(expected, cell)
                self.assertNotIn("0.000", cell)
                self.panel.table.selection_set("slave1-G1")
                self.panel._show_row()
                self.assertIn("未确认", self.panel.detail.get("1.0", "end"))

    def test_resistance_phase_displays_progress_and_destination(self) -> None:
        self.panel.handle_event("topology_progress", {"phase": "resistance", "completed": 169, "total": 192, "source": "slave1-G1", "target": "slave2-G3"})
        self.assertIn("接线电阻", self.panel.status.get())
        self.assertIn("169/192", self.panel.status.get())
        self.assertIn("slave1-G1 -> slave2-G3", self.panel.status.get())

    def test_discovery_marks_missing_module_without_declaring_ready(self) -> None:
        self.panel.handle_event("topology_discovered", {"side": "left", "master": "master1", "count": 3, "online": 5})
        self.assertIn("2/3", self.panel.left_discovery.get())
        self.assertIn("缺失 slave2", self.panel.left_discovery.get())


if __name__ == "__main__":
    unittest.main()
