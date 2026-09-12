"""Exercise the real RS485 GUI startup and manual-operation protocol boundaries."""

import queue
import time
import tkinter as tk
import unittest
from unittest import mock

import cable_tester_gui as gui
from node_measurement import NodeMeasurementController
from all_matrix_reset import AllMatrixResetController


def point_replies(target, command):
    """Reply with the firmware's real text protocol, including stream acknowledgement."""
    fields = command.split()
    if command == 'TOPO_INFO':
        route = int(target == 'master1')
        return [f'OK TOPO_INFO role=MASTER capacity=10 configured=7 bus=1 route={route} reliable=1 cache_ready=1 cache_session=0 plan_session=0']
    if fields[0] == 'TOPO_DISCOVER':
        count = int(fields[1])
        return [f'OK TOPO_DISCOVER count={count} online={(1 << count) - 1:08x}']
    if fields[0] == 'TOPO_POINT':
        return ['OK TOPO_POINT', f'TOPO_POINT_SAMPLE {fields[1]} {fields[4]} {fields[5]} OK MEASURE resistance=1.234 raw=1234 range=2', f'TOPO_DONE {fields[1]} 1']
    return ['OK ' + fields[0]]


def destroy_root(root):
    """Remove pending Tk callbacks before destroying a short-lived test application."""
    for callback in root.tk.call('after', 'info'):
        root.after_cancel(callback)
    root.destroy()


class ConsoleStartupTests(unittest.TestCase):
    def test_real_application_builds_console_debug_and_status_pages(self):
        root = tk.Tk()
        root.withdraw()
        try:
            app = gui.CableTesterApp(root)
            root.update_idletasks()
            self.assertEqual([app.notebook.tab(tab, 'text') for tab in app.notebook.tabs()],
                             ['控制台', '调试', '矩阵状态'])
            self.assertTrue(str(app.dual_node_measurement_button).startswith(app.notebook.tabs()[1]))
            self.assertTrue(str(app.node_measurement_button).startswith(app.notebook.tabs()[0]))
            self.assertEqual(tuple(app.node_left_selector['values'])[:2], ('slave1-G1', 'slave1-G2'))
            app.node_left_modules.set('2')
            self.assertIn('slave2-G24', app.node_left_selector['values'])
        finally:
            destroy_root(root)

    def test_gui_result_queue_completes_measurement_and_restores_controls(self):
        root = tk.Tk()
        root.withdraw()
        app = None
        commands = []

        def send(target, request, command):
            commands.append((target, command))
            for reply in point_replies(target, command):
                app.events.put(('result_frame', (target, request, reply)))
            return f'OK FORWARDED {target} {request}'

        try:
            with mock.patch.object(gui.RouterService, 'send_from_controller', side_effect=send), \
                 mock.patch.object(gui.RouterService, 'running', new_callable=mock.PropertyMock, return_value=True):
                app = gui.CableTesterApp(root)
                app.events.put(('devices', ('master1', 'master2')))
                app._poll_events()
                app.node_settle_seconds.set('0.017')
                app.node_measurement_button.invoke()
                self.assertTrue(app._manual_operation_running())
                self.assertTrue(app.reset_all_button.instate(['disabled']))
                self.assertFalse(app._start_topology_scan(left_master='master1', right_master='master2'))
                app._send('RESET')
                app._run_dual_node_measurement()
                deadline = time.monotonic() + 4
                while app._manual_operation_running() and time.monotonic() < deadline:
                    root.update()
                    time.sleep(0.005)
                self.assertFalse(app._manual_operation_running())
                self.assertIn('1.234', app.node_measurement_status.get())
                self.assertIn('17 ms', app.node_measurement_status.get())
                self.assertTrue(app.reset_all_button.instate(['!disabled']))
                self.assertTrue(all(command.startswith('TOPO_') for _, command in commands))
        finally:
            destroy_root(root)

    def test_gui_all_reset_uses_all_online_devices_even_in_slave_mode(self):
        root = tk.Tk()
        root.withdraw()
        app = None
        commands = []
        devices = ('master1', 'master2', 'm2-s4')

        def send(target, request, command):
            commands.append((target, command))
            reply = 'OK BUS_SENT broadcast RESET' if command == 'BUS broadcast RESET' else 'OK RESET'
            app.events.put(('result_frame', (target, request, reply)))
            return f'OK FORWARDED {target} {request}'

        try:
            with mock.patch.object(gui.RouterService, 'send_from_controller', side_effect=send), \
                 mock.patch.object(gui.RouterService, 'connected_ids', return_value=devices), \
                 mock.patch.object(gui.RouterService, 'running', new_callable=mock.PropertyMock, return_value=True):
                app = gui.CableTesterApp(root)
                app.mode.set('slave')
                app.reset_all_button.invoke()
                self.assertTrue(app.node_measurement_button.instate(['disabled']))
                app.master_id.set('master2')
                app.target_id.set('m2-s4')
                deadline = time.monotonic() + 5
                while app._manual_operation_running() and time.monotonic() < deadline:
                    root.update()
                    time.sleep(0.005)
                self.assertFalse(app._manual_operation_running())
                for master in ('master1', 'master2'):
                    self.assertIn((master, 'RESET'), commands)
                    self.assertIn((master, 'BUS broadcast RESET'), commands)
                    for index in range(1, 11):
                        self.assertIn((master, f'BUS slave{index} RESET'), commands)
                self.assertIn(('m2-s4', 'RESET'), commands)
                self.assertIn('已确认 23 个目标', app.all_reset_status.get())
                self.assertTrue(app.measure_button.instate(['disabled']))  # slave mode
        finally:
            destroy_root(root)


class NodeProtocolTests(unittest.TestCase):
    def test_real_info_payload_and_point_result_reach_completion_after_cleanup(self):
        events = queue.Queue()
        commands = []
        controller = None

        def send(target, request, command):
            commands.append((target, command))
            for reply in point_replies(target, command):
                controller.feed_result(target, request, reply)
            return f'OK FORWARDED {target} {request}'

        controller = NodeMeasurementController(send, lambda *event: events.put(event), response_timeout_seconds=0.2)
        controller.start('master1', 'master2', 2, 1, 'slave2-G24', 'slave1-G4', 0.123)
        deadline = time.monotonic() + 2
        recorded = []
        while time.monotonic() < deadline:
            event = events.get(timeout=2)
            recorded.append(event)
            if event[0] in {'node_measure_complete', 'node_measure_error', 'node_measure_stopped'}:
                self.assertFalse(controller.running, 'terminal event must follow matrix cleanup')
                break
        errors = [message for kind, message in recorded if kind == 'node_measure_error']
        self.assertFalse(errors, errors)
        result = next(message for kind, message in recorded if kind == 'node_measure_complete')
        self.assertEqual(result['sample'], 'OK MEASURE resistance=1.234 raw=1234 range=2')
        point = next(command for _, command in commands if command.startswith('TOPO_POINT '))
        self.assertTrue(point.endswith(' master2 2 47 3 123'), point)
        self.assertTrue(all(command.startswith('TOPO_RESET ') for _, command in commands[-2:]))

    def run_case(self, alter):
        """Run one failure/cancellation scenario with bounded in-memory replies."""
        events, commands = queue.Queue(), []
        controller = None

        def send(target, request, command):
            commands.append((target, command))
            replies = alter(controller, target, request, command, point_replies(target, command))
            for reply in replies:
                controller.feed_result(target, request, reply)
            return f'OK FORWARDED {target} {request}'

        controller = NodeMeasurementController(send, lambda *event: events.put(event), response_timeout_seconds=0.1)
        controller.start('master1', 'master2', 1, 1, 'slave1-G1', 'slave1-G4', 0.02)
        while True:
            event = events.get(timeout=3)
            if event[0] in {'node_measure_complete', 'node_measure_error', 'node_measure_stopped'}:
                self.assertFalse(controller.running)
                return event, commands

    def test_wrong_point_coordinates_and_session_are_rejected(self):
        for bad in ('TOPO_POINT_SAMPLE 999 0 3 OK MEASURE resistance=1.0 raw=1 range=2',
                    'TOPO_DONE 999 1', 'ERR TOPO_POINT BUSY'):
            with self.subTest(reply=bad):
                event, commands = self.run_case(lambda c, t, r, command, replies:
                    [bad] if command.startswith('TOPO_POINT ') else replies)
                self.assertEqual(event[0], 'node_measure_error')
                self.assertTrue(any(command.startswith('TOPO_ABORT ') for _, command in commands))
                self.assertTrue(all(command.startswith('TOPO_RESET ') for _, command in commands[-2:]))

    def test_timeout_and_cancel_abort_source_then_reset_both_sides(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                def alter(controller, target, request, command, replies):
                    if command.startswith('TOPO_POINT '):
                        if cancel:
                            controller.cancel()
                        return []
                    return replies
                event, commands = self.run_case(alter)
                self.assertEqual(event[0], 'node_measure_stopped' if cancel else 'node_measure_error')
                self.assertTrue(commands[-3][1].startswith('TOPO_ABORT '))
                self.assertEqual([target for target, _ in commands[-2:]], ['master1', 'master2'])

    def test_pending_flash_data_is_not_reset(self):
        event, commands = self.run_case(lambda c, t, r, command, replies:
            [replies[0].replace('cache_session=0', 'cache_session=8 cache_ack=2 cache_next=5')]
            if command == 'TOPO_INFO' else replies)
        self.assertEqual(event[0], 'node_measure_error')
        self.assertEqual(commands, [('master1', 'TOPO_INFO')])

    def test_already_acknowledged_cache_does_not_block_point_measurement(self):
        event, commands = self.run_case(lambda c, t, r, command, replies:
            [replies[0].replace('cache_session=0', 'cache_session=8 cache_ack=4 cache_next=5')]
            if command == 'TOPO_INFO' else replies)
        self.assertEqual(event[0], 'node_measure_complete')
        self.assertIn(('master1', 'TOPO_RESET 8'), commands)
        self.assertFalse(any('TOPO_ACK' in command or 'TOPO_OPEN' in command for _, command in commands))

    def test_failed_cleanup_is_not_reported_as_success(self):
        seen = set()

        def alter(c, target, request, command, replies):
            if command.startswith('TOPO_RESET '):
                if target in seen:
                    return ['ERR TOPO_RESET FAILURE']
                seen.add(target)
            return replies
        event, _ = self.run_case(alter)
        self.assertEqual(event[0], 'node_measure_error')
        self.assertIn('复位未确认', event[1])


class ResetProtocolTests(unittest.TestCase):
    def test_broadcast_and_forwarded_receipt_cannot_fake_confirmation(self):
        events, commands = queue.Queue(), []
        controller = None

        def send(target, request, command):
            commands.append((target, command))
            if command == 'BUS slave3 RESET':
                return f'OK FORWARDED {target} {request}'  # no hardware reply
            if command == 'BUS slave5 RESET':
                return f'ERR DELIVERY_FAILED {target} {request}'
            payload = 'OK BUS_SENT broadcast RESET' if command == 'BUS broadcast RESET' else 'OK RESET'
            self.assertFalse(controller.feed_result('wrong-device', request, payload))
            controller.feed_result(target, request, payload)
            return f'OK FORWARDED {target} {request}'

        controller = AllMatrixResetController(send, lambda *event: events.put(event), response_timeout_seconds=0.02)
        controller.start(('master1', 'master2'), ('m1-s1',))
        while True:
            event, result = events.get(timeout=3)
            if event == 'all_reset_complete':
                break
        self.assertFalse(controller.running)
        self.assertIn(('master2', 'BUS slave10 RESET'), commands)
        self.assertIn(('m1-s1', 'RESET'), commands)
        self.assertEqual(len(result['broadcast']), 2)
        self.assertEqual(len(result['unconfirmed']), 4)
        self.assertNotIn('master1-slave3', result['confirmed'])
        self.assertIn('master1-slave10', result['confirmed'])


if __name__ == '__main__':
    unittest.main()
