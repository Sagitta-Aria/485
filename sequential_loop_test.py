"""Background controller for the GUI Y0 sequential loop test.

Y0 remains the fixed common starting point.  The test opens one route at a
time: Y0 to S1_X0, Y0 to S2_X0, Y0 to S1_X1, Y0 to S2_X1, and so on through
Y0 to S2_X23.  Every accepted ON command is paired with an OFF command before
the next route opens, and the matrix is reset before and after the run.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Callable


DEFAULT_STEP_SECONDS = 0.5
DEFAULT_RESPONSE_TIMEOUT_SECONDS = 5.0
PORTS_PER_BANK = 24

SendRequest = Callable[[str, str, str], str]
PublishEvent = Callable[[str, object], None]


@dataclass(frozen=True)
class SequencePoint:
    """Identify one matrix crosspoint in the fixed Y0 traversal order."""

    bank: str
    x: int

    def __post_init__(self) -> None:
        if self.bank not in {"S1", "S2"}:
            raise ValueError(f"invalid bank: {self.bank}")
        if not 0 <= self.x < PORTS_PER_BANK:
            raise ValueError(f"X channel outside 0..23: {self.x}")

    @property
    def label(self) -> str:
        """Return the connector label shown in GUI progress messages."""
        return f"{self.bank}_X{self.x}"

    @property
    def route_label(self) -> str:
        """Describe the tested route with Y0 explicitly shown as its start."""
        return f"Y0 -> {self.label}"

    def command(self, closed: bool) -> str:
        """Return the firmware command for this point on the shared Y0 bus."""
        state = "ON" if closed else "OFF"
        return f"SWITCH {self.bank} {self.x} Y0 {state}"


def build_y0_sequence() -> tuple[SequencePoint, ...]:
    """Build alternating Y0-to-S1 and Y0-to-S2 routes for X0 through X23."""
    return tuple(
        SequencePoint(bank, x)
        for x in range(PORTS_PER_BANK)
        for bank in ("S1", "S2")
    )


class SequentialLoopTestController:
    """Own the matrix command path while a repeating Y0 sequence is running."""

    def __init__(
        self,
        send_request: SendRequest,
        publish_event: PublishEvent,
        *,
        step_seconds: float = DEFAULT_STEP_SECONDS,
        response_timeout_seconds: float = DEFAULT_RESPONSE_TIMEOUT_SECONDS,
    ) -> None:
        if step_seconds < 0.0:
            raise ValueError("step_seconds must not be negative")
        self._send_request = send_request
        self._publish_event = publish_event
        self._step_seconds = step_seconds
        self._response_timeout_seconds = response_timeout_seconds
        self._points = build_y0_sequence()
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[str, queue.Queue[str]]] = {}
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()

    @property
    def running(self) -> bool:
        """Return whether the sequence worker currently owns matrix control."""
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self, target_id: str) -> bool:
        """Start the repeating sequence for one target and reject a second run."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._cancel.clear()
            thread = threading.Thread(
                target=self._run,
                args=(target_id,),
                daemon=True,
                name="cable-y0-sequential-loop-test",
            )
            self._thread = thread
            thread.start()
        return True

    def cancel(self) -> None:
        """Request a cooperative stop; the active point is still opened safely."""
        self._cancel.set()

    def feed_result(self, target_id: str, request_id: str, payload: str) -> bool:
        """Deliver one correlated ESP RESULT to the waiting sequence worker."""
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

    def _run(self, target_id: str) -> None:
        started_at = datetime.now().astimezone()
        request_prefix = f"Y0S-{started_at.strftime('%H%M%S')}"
        completed_steps = 0
        completed_cycles = 0
        terminal_event: tuple[str, object] | None = None
        self._publish_event(
            "sequence_started",
            f"目标 {target_id}，固定从 Y0 出发，轮流打开 S1/S2 的 X0~X23。",
        )

        try:
            self._expect(
                self._request_one(target_id, f"{request_prefix}-RST0", "RESET"),
                "OK RESET",
            )
            while not self._cancel.is_set():
                cycle_number = completed_cycles + 1
                cycle_complete = True
                for point_index, point in enumerate(self._points, start=1):
                    if self._cancel.is_set():
                        cycle_complete = False
                        break
                    sequence = completed_steps + 1
                    request_base = f"{request_prefix}-{sequence:04d}"
                    on_command = point.command(True)
                    off_command = point.command(False)
                    body_error: BaseException | None = None
                    try:
                        self._expect(
                            self._request_one(
                                target_id, f"{request_base}-ON", on_command
                            ),
                            f"OK {on_command}",
                        )
                        self._publish_event(
                            "sequence_progress",
                            {
                                "cycle": cycle_number,
                                "index": point_index,
                                "total": len(self._points),
                                "point": point.route_label,
                            },
                        )
                        if self._cancel.wait(self._step_seconds):
                            cycle_complete = False
                    except BaseException as error:
                        body_error = error
                    finally:
                        try:
                            self._expect(
                                self._request_one(
                                    target_id, f"{request_base}-OFF", off_command
                                ),
                                f"OK {off_command}",
                            )
                        except BaseException as off_error:
                            if body_error is not None:
                                raise RuntimeError(
                                    f"{point.label} 测试失败（{body_error}）；"
                                    f"断开也失败（{off_error}）"
                                ) from off_error
                            raise
                    if body_error is not None:
                        raise body_error
                    completed_steps += 1
                    if self._cancel.is_set():
                        cycle_complete = point_index == len(self._points)
                        break
                if cycle_complete:
                    completed_cycles += 1

            terminal_event = (
                "sequence_stopped",
                {
                    "target_id": target_id,
                    "completed_steps": completed_steps,
                    "completed_cycles": completed_cycles,
                },
            )
        except Exception as error:
            terminal_event = ("sequence_error", f"Y0顺序测试失败：{error}")
        finally:
            try:
                reset_payload = self._request_one(
                    target_id, f"{request_prefix}-RST1", "RESET"
                )
                if reset_payload != "OK RESET":
                    self._publish_event(
                        "sequence_reset_warning",
                        f"Y0顺序测试结束复位失败：{reset_payload}",
                    )
            except Exception as error:
                self._publish_event(
                    "sequence_reset_warning",
                    f"Y0顺序测试结束复位异常：{error}",
                )
            with self._lock:
                self._pending.clear()
                self._thread = None
            if terminal_event is not None:
                self._publish_event(*terminal_event)

    @staticmethod
    def _expect(payload: str, expected: str) -> None:
        """Reject a response that does not exactly acknowledge the command."""
        if payload != expected:
            raise RuntimeError(f"期望 {expected}，实际 {payload}")

    def _request_one(self, target_id: str, request_id: str, command: str) -> str:
        """Send one GUI-routed command and wait for its matching RESULT payload."""
        response_queue: queue.Queue[str] = queue.Queue(maxsize=1)
        with self._lock:
            self._pending[request_id] = (target_id, response_queue)
        try:
            self._publish_event(
                "sent", f"GUI -> {target_id} {request_id} {command}"
            )
            acknowledgement = self._send_request(target_id, request_id, command)
            self._publish_event("received", acknowledgement)
            if not acknowledgement.startswith("OK FORWARDED "):
                return acknowledgement
            try:
                return response_queue.get(timeout=self._response_timeout_seconds)
            except queue.Empty:
                return f"ERR SEQUENCE RESULT_TIMEOUT request_id={request_id}"
        finally:
            with self._lock:
                self._pending.pop(request_id, None)
