"""Background controller for one selectable dual-node Kelvin closure.

The user selects two X ports for the positive node and two X ports for the
negative node. The four selected ports are mapped to the same Y0-Y3
four-wire arrangement used by the measurement path. This module owns only the
four SWITCH commands and deliberately leaves the resulting matrix closed;
RESET remains an explicit user command.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Callable


PORTS_PER_BANK = 24
DEFAULT_SETTLE_SECONDS = 1.0
DEFAULT_RESPONSE_TIMEOUT_SECONDS = 5.0

SendRequest = Callable[[str, str, str], str]
PublishEvent = Callable[[str, object], None]


@dataclass(frozen=True)
class MatrixPort:
    """Identify one selectable S1/S2 X port."""

    bank: str
    x: int

    def __post_init__(self) -> None:
        if self.bank not in {"S1", "S2"}:
            raise ValueError(f"invalid matrix bank: {self.bank}")
        if not 0 <= self.x < PORTS_PER_BANK:
            raise ValueError(f"X channel outside 0..23: {self.x}")

    @property
    def label(self) -> str:
        """Return the stable port label used in events and logs."""
        return f"{self.bank}_X{self.x}"


@dataclass(frozen=True)
class DualNodeSpec:
    """Describe both Kelvin nodes and their fixed current/voltage mapping."""

    positive_current: MatrixPort
    positive_voltage: MatrixPort
    negative_current: MatrixPort
    negative_voltage: MatrixPort

    def __post_init__(self) -> None:
        if self.positive_current == self.positive_voltage:
            raise ValueError("正端两个 X 不能相同")
        if self.negative_current == self.negative_voltage:
            raise ValueError("负端两个 X 不能相同")
        ports = (
            self.positive_current,
            self.positive_voltage,
            self.negative_current,
            self.negative_voltage,
        )
        if len(set(ports)) != len(ports):
            raise ValueError("正负端不能重复选择同一个 X 节点")

    @property
    def positive_label(self) -> str:
        """Return the positive node as current/voltage labels."""
        return f"{self.positive_current.label} / {self.positive_voltage.label}"

    @property
    def negative_label(self) -> str:
        """Return the negative node as current/voltage labels."""
        return f"{self.negative_current.label} / {self.negative_voltage.label}"

    @property
    def label(self) -> str:
        """Return a concise positive-to-negative measurement label."""
        return f"{self.positive_label} -> {self.negative_label}"

    @property
    def switch_commands(self) -> tuple[str, ...]:
        """Return safe closure order: voltage leads, then current leads."""
        return (
            f"SWITCH {self.positive_voltage.bank} {self.positive_voltage.x} Y2 ON",
            f"SWITCH {self.negative_voltage.bank} {self.negative_voltage.x} Y1 ON",
            f"SWITCH {self.negative_current.bank} {self.negative_current.x} Y0 ON",
            f"SWITCH {self.positive_current.bank} {self.positive_current.x} Y3 ON",
        )


def build_dual_node_spec(
    positive_bank: str,
    positive_current_x: int,
    positive_voltage_x: int,
    negative_bank: str,
    negative_current_x: int,
    negative_voltage_x: int,
) -> DualNodeSpec:
    """Validate GUI selections and build the fixed four-wire mapping."""
    return DualNodeSpec(
        positive_current=MatrixPort(positive_bank, int(positive_current_x)),
        positive_voltage=MatrixPort(positive_bank, int(positive_voltage_x)),
        negative_current=MatrixPort(negative_bank, int(negative_current_x)),
        negative_voltage=MatrixPort(negative_bank, int(negative_voltage_x)),
    )


class DualNodeMeasurementController:
    """Own one dual-node matrix closure until all four switches are applied."""

    def __init__(
        self,
        send_request: SendRequest,
        publish_event: PublishEvent,
        *,
        settle_seconds: float = DEFAULT_SETTLE_SECONDS,
        response_timeout_seconds: float = DEFAULT_RESPONSE_TIMEOUT_SECONDS,
    ) -> None:
        if settle_seconds < 0.0:
            raise ValueError("settle_seconds must not be negative")
        if response_timeout_seconds <= 0.0:
            raise ValueError("response_timeout_seconds must be positive")
        self._send_request = send_request
        self._publish_event = publish_event
        self._settle_seconds = settle_seconds
        self._response_timeout_seconds = response_timeout_seconds
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[str, queue.Queue[str]]] = {}
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()

    @property
    def running(self) -> bool:
        """Return whether the worker currently owns the matrix path."""
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(
        self,
        target_id: str,
        positive_bank: str,
        positive_current_x: int,
        positive_voltage_x: int,
        negative_bank: str,
        negative_current_x: int,
        negative_voltage_x: int,
    ) -> bool:
        """Start one validated measurement and reject overlapping runs."""
        target = str(target_id).strip()
        if not target or any(character.isspace() for character in target):
            raise ValueError("目标ID不能为空且不能包含空格")
        spec = build_dual_node_spec(
            positive_bank,
            positive_current_x,
            positive_voltage_x,
            negative_bank,
            negative_current_x,
            negative_voltage_x,
        )
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._cancel.clear()
            thread = threading.Thread(
                target=self._run,
                args=(target, spec),
                daemon=True,
                name="cable-dual-node-closure",
            )
            self._thread = thread
            thread.start()
        return True

    def cancel(self) -> None:
        """Request cooperative cancellation; applied switches remain closed."""
        self._cancel.set()

    def feed_result(self, target_id: str, request_id: str, payload: str) -> bool:
        """Deliver one correlated RESULT frame to the waiting worker."""
        with self._lock:
            pending = self._pending.get(request_id)
        if pending is None:
            return False
        expected_target_id, destination = pending
        if target_id != expected_target_id:
            return False
        try:
            destination.put_nowait(payload)
        except queue.Full:
            return False
        return True

    def _run(self, target_id: str, spec: DualNodeSpec) -> None:
        started_at = datetime.now().astimezone()
        request_prefix = f"DNM-{started_at.strftime('%H%M%S')}"
        terminal_event: tuple[str, object] | None = None
        switch_responses: list[str] = []
        cancelled = False
        self._publish_event(
            "dual_node_started",
            {
                "target_id": target_id,
                "positive": spec.positive_label,
                "negative": spec.negative_label,
                "mapping": "正端第一个X=I+、第二个X=V+；负端第一个X=I-、第二个X=V-",
            },
        )
        try:
            for index, command in enumerate(spec.switch_commands, start=1):
                if self._cancel.is_set():
                    cancelled = True
                    break
                response = self._request_one(
                    target_id, f"{request_prefix}-S{index}", command
                )
                switch_responses.append(response)
                if response != f"OK {command}":
                    raise RuntimeError(f"闭合失败：{response}")
                self._publish_event(
                    "dual_node_progress",
                    {"phase": "connecting", "index": index, "total": 4, "command": command},
                )
            if not cancelled:
                if self._settle_seconds > 0.0 and self._cancel.wait(self._settle_seconds):
                    cancelled = True
                if not cancelled:
                    terminal_event = (
                        "dual_node_complete",
                        {
                            "target_id": target_id,
                            "positive": spec.positive_label,
                            "negative": spec.negative_label,
                            "switch_responses": tuple(switch_responses),
                            "cancelled": False,
                            "kept_closed": True,
                        },
                    )
            if cancelled:
                terminal_event = (
                    "dual_node_stopped",
                    {
                        "target_id": target_id,
                        "positive": spec.positive_label,
                        "negative": spec.negative_label,
                        "switch_responses": tuple(switch_responses),
                        "kept_closed": True,
                    },
                )
        except Exception as error:
            terminal_event = ("dual_node_error", f"双节点四线闭合失败：{error}")
        finally:
            with self._lock:
                self._pending.clear()
                self._thread = None
            if terminal_event is not None:
                self._publish_event(*terminal_event)

    def _request_one(self, target_id: str, request_id: str, command: str) -> str:
        """Send one routed command and await its matching RESULT payload."""
        response_queue: queue.Queue[str] = queue.Queue(maxsize=1)
        with self._lock:
            self._pending[request_id] = (target_id, response_queue)
        try:
            self._publish_event("sent", f"GUI -> {target_id} {request_id} {command}")
            acknowledgement = self._send_request(target_id, request_id, command)
            self._publish_event("received", acknowledgement)
            if not acknowledgement.startswith("OK FORWARDED "):
                return acknowledgement
            try:
                return response_queue.get(timeout=self._response_timeout_seconds)
            except queue.Empty:
                return f"ERR DUAL_NODE RESULT_TIMEOUT request_id={request_id}"
        finally:
            with self._lock:
                self._pending.pop(request_id, None)
