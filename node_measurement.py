"""Run one two-ended topology node measurement from the main console.

This controller deliberately uses the existing legacy ``TOPO_POINT`` protocol
for a single debug measurement. The right master prepares one destination node,
the left master closes one source node, waits the requested settling interval,
and reads the left master's XD31H. It owns both masters until the temporary
topology plan has been reset.
"""

from __future__ import annotations

import queue
import re
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable


PORTS_PER_MODULE = 24
MAX_MODULES = 10
DEFAULT_SETTLE_SECONDS = 0.1
DEFAULT_RESPONSE_TIMEOUT_SECONDS = 30.0

SendRequest = Callable[[str, str, str], str]
PublishEvent = Callable[[str, object], None]


@dataclass(frozen=True)
class NodeAddress:
    """Identify one local slave module and its paired topology group."""

    module: int
    port: int

    def __post_init__(self) -> None:
        if not 0 <= self.module < MAX_MODULES:
            raise ValueError("从机编号必须在 1 到 10 之间")
        if not 0 <= self.port < PORTS_PER_MODULE:
            raise ValueError("节点编号必须在 G1 到 G24 之间")

    @property
    def global_port(self) -> int:
        """Return the global topology index used by TOPO_POINT."""
        return self.module * PORTS_PER_MODULE + self.port

    @property
    def label(self) -> str:
        """Return the user-facing slave/module node label."""
        return f"slave{self.module + 1}-G{self.port + 1}"


def parse_node_label(label: str) -> NodeAddress:
    """Parse a label such as ``slave2-G7`` and reject ambiguous input."""
    match = re.fullmatch(r"slave([1-9]|10)-G(\d+)", str(label).strip(), re.IGNORECASE)
    if match is None:
        raise ValueError("节点格式必须是 slave1-G1 到 slave10-G24")
    return NodeAddress(int(match.group(1)) - 1, int(match.group(2)) - 1)


def node_labels(module_count: int) -> tuple[str, ...]:
    """Return all node labels covered by a selected contiguous module count."""
    if type(module_count) is not int or not 1 <= module_count <= MAX_MODULES:
        raise ValueError("从机数量必须在 1 到 10 之间")
    return tuple(NodeAddress(module, port).label
                 for module in range(module_count)
                 for port in range(PORTS_PER_MODULE))


class NodeMeasurementController:
    """Own and execute one two-ended node measurement worker."""

    def __init__(
        self,
        send_request: SendRequest,
        publish_event: PublishEvent,
        *,
        response_timeout_seconds: float = DEFAULT_RESPONSE_TIMEOUT_SECONDS,
    ) -> None:
        if response_timeout_seconds <= 0:
            raise ValueError("response_timeout_seconds must be positive")
        self._send_request = send_request
        self._publish_event = publish_event
        self._response_timeout = response_timeout_seconds
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[str, queue.Queue[str]]] = {}
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._sequence = 0

    @property
    def running(self) -> bool:
        """Return whether a measurement worker currently owns the masters."""
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(
        self,
        left_master: str,
        right_master: str,
        left_modules: int,
        right_modules: int,
        left_node: str,
        right_node: str,
        settle_seconds: float,
    ) -> bool:
        """Validate one node pair and start its asynchronous measurement."""
        left_master, right_master = str(left_master).strip(), str(right_master).strip()
        if (not left_master or not right_master or left_master == right_master
                or any(c.isspace() for c in left_master + right_master)):
            raise ValueError("左右 Master 必须是两个不同的在线设备")
        if type(left_modules) is not int or not 1 <= left_modules <= MAX_MODULES:
            raise ValueError("左侧从机数量必须在 1 到 10 之间")
        if type(right_modules) is not int or not 1 <= right_modules <= MAX_MODULES:
            raise ValueError("右侧从机数量必须在 1 到 10 之间")
        left = parse_node_label(left_node)
        right = parse_node_label(right_node)
        if left.module >= left_modules or right.module >= right_modules:
            raise ValueError("所选节点超出对应的计划从机数量")
        if not isinstance(settle_seconds, (int, float)) or not 0 <= float(settle_seconds) <= 5:
            raise ValueError("测量等待时间必须在 0 到 5 秒之间")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._cancel.clear()
            thread = threading.Thread(
                target=self._run,
                args=(left_master, right_master, left_modules, right_modules,
                      left, right, round(float(settle_seconds) * 1000)),
                daemon=True,
                name="cable-node-measurement",
            )
            self._thread = thread
            thread.start()
        return True

    def cancel(self) -> None:
        """Request cancellation; the worker still performs matrix cleanup."""
        self._cancel.set()

    def feed_result(self, target_id: str, request_id: str, payload: str) -> bool:
        """Deliver one correlated router result frame to the worker."""
        with self._lock:
            pending = self._pending.get(request_id)
        if pending is None or pending[0] != target_id:
            return False
        try:
            pending[1].put_nowait(payload)
        except queue.Full:
            return False
        return True

    def _next_request(self, sid: int) -> str:
        with self._lock:
            self._sequence += 1
            return f"NM-{sid:08x}-{self._sequence}"

    def _request_one(self, target: str, sid: int, command: str, *, cleanup: bool = False) -> str:
        """Send one command and wait for its first matching RESULT payload."""
        request_id = self._next_request(sid)
        inbox: queue.Queue[str] = queue.Queue(maxsize=1)
        with self._lock:
            self._pending[request_id] = (target, inbox)
        try:
            self._publish_event("sent", f"GUI -> {target} {request_id} {command}")
            acknowledgement = self._send_request(target, request_id, command)
            self._publish_event("received", acknowledgement)
            if not acknowledgement.startswith("OK FORWARDED "):
                raise RuntimeError(acknowledgement)
            deadline = time.monotonic() + self._response_timeout
            while time.monotonic() < deadline:
                if self._cancel.is_set() and not cleanup:
                    raise RuntimeError("节点测量已取消")
                try:
                    return inbox.get(timeout=min(0.1, max(0.001, deadline - time.monotonic())))
                except queue.Empty:
                    pass
            raise TimeoutError(f"{target} {command.split()[0]} RESULT_TIMEOUT")
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def _stream_point(
        self,
        target: str,
        sid: int,
        command: str,
        source: NodeAddress,
        destination: NodeAddress,
    ) -> tuple[str, str]:
        """Collect the sample and terminal frame emitted by one TOPO_POINT job."""
        request_id = self._next_request(sid)
        inbox: queue.Queue[str] = queue.Queue()
        with self._lock:
            self._pending[request_id] = (target, inbox)
        sample: str | None = None
        terminal: str | None = None
        deadline = time.monotonic() + self._response_timeout
        try:
            self._publish_event("sent", f"GUI -> {target} {request_id} {command}")
            acknowledgement = self._send_request(target, request_id, command)
            self._publish_event("received", acknowledgement)
            if not acknowledgement.startswith("OK FORWARDED "):
                raise RuntimeError(acknowledgement)
            while time.monotonic() < deadline:
                if self._cancel.is_set():
                    raise RuntimeError("节点测量已取消")
                try:
                    payload = inbox.get(timeout=min(0.2, max(0.01, deadline - time.monotonic())))
                except queue.Empty:
                    continue
                self._publish_event("received", f"{target} {payload}")
                if payload.startswith("ERR "):
                    raise RuntimeError(f"{target}: {payload}")
                fields = payload.split(maxsplit=5)
                if fields[:1] == ["TOPO_POINT_SAMPLE"]:
                    if len(fields) < 5 or fields[1:4] != [str(sid), str(source.global_port), str(destination.global_port)]:
                        raise RuntimeError(f"节点测量坐标不匹配：{payload}")
                    sample = " ".join(fields[4:])
                elif fields[:1] in (["TOPO_DONE"], ["TOPO_FAILED"], ["TOPO_STOPPED"]):
                    if len(fields) < 3 or fields[1] != str(sid):
                        raise RuntimeError(f"节点测量会话不匹配：{payload}")
                    terminal = payload
                    break
            if terminal is None:
                raise TimeoutError(f"{target} TOPO_POINT RESULT_TIMEOUT")
            if terminal.startswith("TOPO_FAILED") or terminal.startswith("TOPO_STOPPED"):
                raise RuntimeError(terminal)
            if sample is None:
                raise RuntimeError("TOPO_POINT 未返回测量结果")
            if terminal != f"TOPO_DONE {sid} 1":
                raise RuntimeError(f"节点测量数量不匹配：{terminal}")
            if not re.fullmatch(r"OK MEASURE resistance=\d+(?:\.\d+)? raw=\d+ range=\d+", sample):
                raise RuntimeError(f"低阻测量失败：{sample}")
            return sample, terminal
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def _run(
        self,
        left_master: str,
        right_master: str,
        left_modules: int,
        right_modules: int,
        left: NodeAddress,
        right: NodeAddress,
        settle_ms: int,
    ) -> None:
        sid = (uuid.uuid4().int & 0xFFFFFFFF) or 1
        cleanup_errors: list[str] = []
        owned: list[str] = []
        point_submitted = False
        sample = ""
        terminal = ""
        final_event: tuple[str, object] = ("node_measure_error", "节点测量未完成")
        try:
            self._publish_event("node_measure_started", {
                "left_master": left_master, "right_master": right_master,
                "left": left.label, "right": right.label, "settle_ms": settle_ms,
            })
            for target, count, route in ((left_master, left_modules, "1"), (right_master, right_modules, "0")):
                self._check_master(target, sid, count, route)
            for target in (left_master, right_master):
                self._expect(target, sid, f"TOPO_RESET {sid}", "OK TOPO_RESET")
                owned.append(target)
            self._expect(right_master, sid, f"TOPO_BEGIN {sid} {right_modules} 1", "OK TOPO_BEGIN")
            for module in range(right_modules):
                mask = 1 << right.port if module == right.module else 0
                self._expect(right_master, sid, f"TOPO_MASK {sid} 0 {module} {mask:06x}", "OK TOPO_MASK")
            self._expect(right_master, sid, f"TOPO_SEAL {sid}", "OK TOPO_SEAL")
            if self._cancel.is_set():
                raise RuntimeError("节点测量已取消")
            self._publish_event("node_measure_progress", {
                "phase": "measuring", "left": left.label, "right": right.label,
                "settle_ms": settle_ms,
            })
            command = f"TOPO_POINT {sid} {right_master} {left_modules} {left.global_port} {right.global_port} {settle_ms}"
            point_submitted = True
            sample, terminal = self._stream_point(left_master, sid, command, left, right)
            final_event = ("node_measure_complete", {
                "left_master": left_master, "right_master": right_master,
                "left": left.label, "right": right.label,
                "sample": sample, "terminal": terminal, "settle_ms": settle_ms,
            })
        except Exception as error:
            final_event = ("node_measure_stopped" if self._cancel.is_set() else "node_measure_error", str(error))
        finally:
            # Abort a potentially active source before releasing the receiver plan.
            if point_submitted and not terminal:
                try:
                    self._request_one(left_master, sid, f"TOPO_ABORT {sid}", cleanup=True)
                except Exception as error:
                    cleanup_errors.append(f"{left_master} 停止未确认：{error}")
            for target in owned:
                try:
                    self._reset_owned(target, sid)
                except Exception as error:
                    cleanup_errors.append(f"{target}: {error}")
            if cleanup_errors:
                self._publish_event("node_measure_cleanup_warning", "; ".join(cleanup_errors))
                final_event = ("node_measure_error", "矩阵复位未确认：" + "; ".join(cleanup_errors)
                               + (f"；已取得读数：{sample}" if sample else ""))
            with self._lock:
                self._pending.clear()
                self._thread = None
            self._publish_event(*final_event)

    def _check_master(self, target: str, sid: int, count: int, route: str) -> None:
        """Validate real TOPO_INFO fields and discover the requested RS485 slaves."""
        info = self._request_one(target, sid, "TOPO_INFO")
        fields = info.split()
        values = {}
        if fields[:2] != ["OK", "TOPO_INFO"]:
            raise RuntimeError(f"{target}: {info}")
        for field in fields[2:]:
            key, sep, value = field.partition("=")
            if not sep or not value or key in values:
                raise RuntimeError(f"{target} TOPO_INFO 格式无效：{info}")
            values[key] = value
        if values.get("role") != "MASTER" or values.get("bus") != "1":
            raise RuntimeError(f"{target} 不是支持 RS485 的主机：{info}")
        if values.get("route") != route:
            raise RuntimeError(f"{target} 仪表路由不符；左端须为 master1，右端须为 master2")
        if not values.get("capacity", "").isdigit() or int(values["capacity"]) < count:
            raise RuntimeError(f"{target} 从机容量不足：{info}")
        # Single-point debug must not discard a saved acquisition session.
        if values.get("plan_session", "0") != "0":
            raise RuntimeError(f"{target} 存在未结束的拓扑会话，请先在拓扑扫描中恢复或结束")
        cache_session = values.get("cache_session", "0")
        if cache_session != "0":
            acknowledged, next_sequence = values.get("cache_ack", ""), values.get("cache_next", "")
            if (not cache_session.isdigit() or not acknowledged.isdigit() or not next_sequence.isdigit()
                    or int(next_sequence) != int(acknowledged) + 1):
                raise RuntimeError(f"{target} 尚有未确认的扫描缓存，请先在拓扑扫描中恢复")
            # Already acknowledged records need no replay. RESET releases runtime
            # ownership only and firmware rejects it if a worker is still active.
            self._expect(target, sid, f"TOPO_RESET {cache_session}", "OK TOPO_RESET")
        discovered = self._request_one(target, sid, f"TOPO_DISCOVER {count}")
        match = re.fullmatch(r"OK TOPO_DISCOVER count=(\d+) online=([0-9a-fA-F]{8})", discovered)
        if match is None or int(match[1]) != count or int(match[2], 16) != (1 << count) - 1:
            raise RuntimeError(f"{target} 计划从机未全部在线：{discovered}")

    def _reset_owned(self, target: str, sid: int) -> None:
        """Wait for an aborted firmware worker to finish before resetting its session."""
        deadline = time.monotonic() + self._response_timeout
        while True:
            reply = self._request_one(target, sid, f"TOPO_RESET {sid}", cleanup=True)
            if reply == "OK TOPO_RESET":
                return
            if reply != "ERR TOPO_RESET BUSY_USE_ABORT" or time.monotonic() >= deadline:
                raise RuntimeError(reply)
            time.sleep(0.05)

    def _expect(self, target: str, sid: int, command: str, expected: str) -> str:
        """Require one exact command response before advancing the plan."""
        response = self._request_one(target, sid, command)
        if response != expected:
            raise RuntimeError(f"{target}: 期望 {expected}，收到 {response}")
        return response
