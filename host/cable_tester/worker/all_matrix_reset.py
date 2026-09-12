"""Reset online Wi-Fi matrices and every supported address on each master's bus.

Broadcast reaches slaves without Wi-Fi. Subsequent unicast RESETs distinguish
confirmed execution from delivery only; unresponsive addresses remain unknown.
This controller uses existing firmware commands and must own the GUI hardware
interlock for its whole run. It never changes topology journals or sessions.
"""

import queue
import threading
import uuid
from collections.abc import Callable


class AllMatrixResetController:
    """Perform one reset sweep while correlating actual device RESULT frames."""

    def __init__(self, send_request: Callable[[str, str, str], str],
                 publish_event: Callable[[str, object], None], *, response_timeout_seconds: float = 5.0):
        self._send = send_request
        self._publish = publish_event
        self._timeout = response_timeout_seconds
        self._lock = threading.Lock()
        self._pending: dict[tuple[str, str], queue.Queue[str]] = {}
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()

    @property
    def running(self) -> bool:
        """Keep ownership until the complete sweep has finished."""
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self, masters: tuple[str, ...], wifi_slaves: tuple[str, ...]) -> bool:
        """Capture online targets so mode/selection changes cannot redirect a reset."""
        if not masters and not wifi_slaves:
            raise ValueError("当前没有在线设备")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._cancel.clear()
            self._thread = threading.Thread(target=self._run, args=(tuple(masters), tuple(wifi_slaves)),
                                            daemon=True, name="all-matrix-reset")
            self._thread.start()
        return True

    def cancel(self) -> None:
        """Stop after the in-flight command; completed resets remain effective."""
        self._cancel.set()

    def feed_result(self, target: str, request: str, payload: str) -> bool:
        """Accept only the first result with matching device and request IDs."""
        with self._lock:
            inbox = self._pending.get((target, request))
        if inbox is None:
            return False
        try:
            inbox.put_nowait(payload)
        except queue.Full:
            return False
        return True

    def _request(self, target: str, request: str, command: str) -> str:
        """Wait for hardware completion, not merely OK FORWARDED."""
        inbox: queue.Queue[str] = queue.Queue(maxsize=1)
        with self._lock:
            self._pending[(target, request)] = inbox
        try:
            self._publish("sent", f"GUI -> {target} {request} {command}")
            ack = self._send(target, request, command)
            self._publish("received", ack)
            if ack != f"OK FORWARDED {target} {request}":
                return ack
            try:
                return inbox.get(timeout=self._timeout)
            except queue.Empty:
                return "ERR RESET RESULT_TIMEOUT"
        except Exception as error:
            return f"ERR RESET {error}"
        finally:
            with self._lock:
                self._pending.pop((target, request), None)

    def _run(self, masters: tuple[str, ...], wifi_slaves: tuple[str, ...]) -> None:
        """Broadcast on each bus, then confirm all 1..10 addresses individually."""
        commands = []
        for master in masters:
            commands.append((master, master, "RESET", False))
            commands.append((master, f"{master}/全部从机", "BUS broadcast RESET", True))
            for index in range(1, 11):
                commands.append((master, f"{master}-slave{index}", f"BUS slave{index} RESET", False))
        commands.extend((slave, slave, "RESET", False) for slave in wifi_slaves)
        prefix = f"AR-{uuid.uuid4().hex[:12]}"
        result: dict[str, object] = {"confirmed": [], "broadcast": [], "unconfirmed": [], "cancelled": False}
        try:
            for index, (transport, label, command, broadcast) in enumerate(commands):
                if self._cancel.is_set():
                    result["cancelled"] = True
                    result["unconfirmed"].extend((item[1], "已取消，未发送") for item in commands[index:])
                    break
                reply = self._request(transport, f"{prefix}-{index}", command)
                expected = "OK BUS_SENT broadcast RESET" if broadcast else "OK RESET"
                if reply == expected:
                    result["broadcast" if broadcast else "confirmed"].append(label)
                else:
                    result["unconfirmed"].append((label, reply))
                self._publish("all_reset_progress", {"target": label, "reply": reply,
                                                     "done": index + 1, "total": len(commands)})
        finally:
            with self._lock:
                self._thread = None
            self._publish("all_reset_complete", result)
