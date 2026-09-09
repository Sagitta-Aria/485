"""Plan and execute scalable two-ended Kelvin continuity scans.

Constant-weight group tests recover reachable endpoint sets, not the physical
location of shorts inside a cable. Their OR model assumes stable contacts and
an instrument capable of classifying grouped paths; thresholds need bench
validation before these results can be used as a production acceptance test.
Only masters communicate with the PC. Slave addresses are local to each side.
"""

from __future__ import annotations

import csv
import itertools
import json
import math
import queue
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence

from topology_transfer import ReplayConflict, SequenceGap, TopologyTransferStore, TransferCorruption, TransferRecord
from topology_binary import BinaryRowSearch


PORTS_PER_MODULE = 24
MAX_MODULES = 10
MAX_BINARY_REPEATS = 10
REPORT_ROOT = Path(__file__).resolve().parent / "reports" / "topology"
MAX_TRANSPORT_EVENTS = 20000
SendRequest = Callable[[str, str, str], str]
PublishEvent = Callable[[str, object], None]
MEASUREMENT_PATTERN = re.compile(
    r"OK MEASURE resistance=(\d+(?:\.\d+)?) raw=(\d+) range=(\d+)"
)


@dataclass(frozen=True, order=True)
class TopologyPort:
    """Address one adjacent current/voltage X pair on a slave, within one side."""

    module_id: int
    local_port: int

    def __post_init__(self) -> None:
        if type(self.module_id) is not int or not 0 <= self.module_id < MAX_MODULES:
            raise ValueError("module_id must be in 0..9")
        if type(self.local_port) is not int or not 0 <= self.local_port < PORTS_PER_MODULE:
            raise ValueError("local_port must be in 0..23")

    @property
    def global_port(self) -> int:
        return self.module_id * PORTS_PER_MODULE + self.local_port

    @property
    def label(self) -> str:
        return f"slave{self.module_id + 1}-G{self.local_port + 1}"

    @property
    def bank(self) -> str:
        return "S1" if self.local_port < 12 else "S2"

    @property
    def current_x(self) -> int:
        return (self.local_port % 12) * 2

    @property
    def voltage_x(self) -> int:
        return self.current_x + 1


def build_ports(module_ids: Sequence[int]) -> tuple[TopologyPort, ...]:
    """Keep configured module ordering and reject duplicate addresses."""
    ids = tuple(module_ids)
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("module_ids must be nonempty and unique")
    return tuple(TopologyPort(module, port) for module in ids for port in range(24))


@dataclass(frozen=True)
class Codebook:
    """Associate equal-weight bit patterns with distinct destination ports."""

    ports: tuple[TopologyPort, ...]
    codes: tuple[int, ...]
    rounds: int
    weight: int

    @classmethod
    def create(cls, ports: Sequence[TopologyPort]) -> "Codebook":
        ordered = tuple(ports)
        if not ordered or len(set(ordered)) != len(ordered):
            raise ValueError("destination ports must be nonempty and unique")
        rounds = 1
        while math.comb(rounds, rounds // 2) < len(ordered):
            rounds += 1
        weight = next(w for w in range(1, rounds + 1) if math.comb(rounds, w) >= len(ordered))
        codes = tuple(
            sum(1 << bit for bit in bits)
            for bits in itertools.islice(itertools.combinations(range(rounds), weight), len(ordered))
        )
        return cls(ordered, codes, rounds, weight)

    def masks_for_round(self, round_index: int) -> dict[int, int]:
        """Return every module's 24-bit mask, including all-zero masks."""
        if not 0 <= round_index < self.rounds:
            raise ValueError("round_index outside codebook")
        masks = {port.module_id: 0 for port in self.ports}
        for port, code in zip(self.ports, self.codes):
            if code & (1 << round_index):
                masks[port.module_id] |= 1 << port.local_port
        return masks


@dataclass(frozen=True)
class Measurement:
    """Preserve raw instrument evidence alongside a three-state decision."""

    raw: str
    bit: int | None
    reason: str
    resistance_ohm: float | None = None
    raw_value: int | None = None
    range_code: int | None = None


def classify_measurement(
    payload: str, on_threshold_ohm: float = 50.0, off_threshold_ohm: float = 100.0
) -> Measurement:
    """Treat overrange as no measured continuity, and protocol faults as unknown."""
    if not (math.isfinite(on_threshold_ohm) and math.isfinite(off_threshold_ohm)):
        raise ValueError("thresholds must be finite")
    if not 0 <= on_threshold_ohm < off_threshold_ohm:
        raise ValueError("thresholds must satisfy 0 <= on < off")
    raw = payload.strip()
    match = MEASUREMENT_PATTERN.fullmatch(raw)
    if match:
        resistance = float(match[1])
        raw_value, range_code = int(match[2]), int(match[3])
        if not math.isfinite(resistance) or range_code not in range(3) or not 0 <= raw_value <= 65535:
            return Measurement(raw, None, "INVALID_NUMERIC_READING")
        bit = 1 if resistance <= on_threshold_ohm else (0 if resistance >= off_threshold_ohm else None)
        reason = "CONTINUITY" if bit == 1 else ("HIGH_RESISTANCE" if bit == 0 else "THRESHOLD_UNCERTAIN")
        return Measurement(raw, bit, reason, resistance, raw_value, range_code)
    if re.fullmatch(r"ERR MEASURE (?:OVERRANGE|OL)(?: .*)?", raw):
        return Measurement(raw, 0, "NO_CONTINUITY_OR_OVERRANGE")
    return Measurement(raw, None, "MEASUREMENT_ERROR")


def confirm_binary_readings(readings: Sequence[Measurement]) -> Measurement:
    """Require agreeing decisions; preserve single-read diagnostics and a real median sample."""
    if not 1 <= len(readings) <= MAX_BINARY_REPEATS:
        raise ValueError(f"binary confirmation requires 1..{MAX_BINARY_REPEATS} readings")
    if len(readings) == 1:
        return readings[0]
    if readings[0].bit is None or any(reading.bit != readings[0].bit for reading in readings):
        return replace(readings[-1], bit=None, reason="UNSTABLE_READING")
    return sorted(readings, key=lambda reading: reading.resistance_ohm if reading.resistance_ohm is not None else float("inf"))[len(readings) // 2]


@dataclass(frozen=True)
class RowDecode:
    """Describe observed endpoint reachability; no physical short location is inferred."""

    status: str
    targets: tuple[TopologyPort, ...]
    candidates: tuple[TopologyPort, ...]
    signature: str


def decode_signature(codebook: Codebook, bits: Sequence[int | None]) -> RowDecode:
    """Decode exact codewords or derive a conservative set for point follow-ups."""
    if len(bits) != codebook.rounds or any(bit not in (0, 1, None) for bit in bits):
        raise ValueError("one 0/1/None value is required per round")
    signature = "".join("?" if bit is None else str(bit) for bit in bits)
    ones = sum(1 << index for index, bit in enumerate(bits) if bit == 1)
    zeros = sum(1 << index for index, bit in enumerate(bits) if bit == 0)
    candidates = tuple(port for port, code in zip(codebook.ports, codebook.codes) if not code & zeros)
    if None in bits:
        return RowDecode("UNKNOWN", (), candidates, signature)
    if ones == 0:
        return RowDecode("NO_CONTINUITY", (), (), signature)
    if ones in codebook.codes:
        port = codebook.ports[codebook.codes.index(ones)]
        return RowDecode("UNIQUE", (port,), (port,), signature)
    union = 0
    for port, code in zip(codebook.ports, codebook.codes):
        if port in candidates:
            union |= code
    status = "SHORT_CANDIDATES" if union == ones else "INCONSISTENT"
    return RowDecode(status, (), candidates, signature)


def resolve_row(
    codebook: Codebook,
    bits: Sequence[int | None],
    point_bits: Mapping[TopologyPort, int | None],
) -> RowDecode:
    """Require all follow-ups before reporting an exact target set.

Unknown or inconsistent group scans require a full row of point readings.
Valid short signatures need only the candidate subset implied by zero rounds.
Point results must agree with every known group bit or the row stays suspect.
"""
    initial = decode_signature(codebook, bits)
    if initial.status in {"UNIQUE", "NO_CONTINUITY"} and not point_bits:
        return initial
    required = codebook.ports if initial.status in {"UNKNOWN", "INCONSISTENT"} else initial.candidates
    if any(port not in codebook.ports or bit not in (0, 1, None) for port, bit in point_bits.items()):
        raise ValueError("point reading outside codebook or invalid bit")
    targets = tuple(port for port in required if point_bits.get(port) == 1)
    if any(port not in point_bits or point_bits[port] is None for port in required):
        return RowDecode("UNKNOWN", targets, required, initial.signature)
    union = 0
    for port, code in zip(codebook.ports, codebook.codes):
        if port in targets:
            union |= code
    if any(bit is not None and bit != ((union >> index) & 1) for index, bit in enumerate(bits)):
        return RowDecode("INCONSISTENT", targets, required, initial.signature)
    status = "SHORT" if len(targets) > 1 else ("UNIQUE" if targets else "NO_CONTINUITY")
    return RowDecode(status, targets, required, initial.signature)


def assess_rows(
    rows: Mapping[TopologyPort, RowDecode],
    expected_mapping: Mapping[TopologyPort, TopologyPort] | None = None,
) -> dict[TopologyPort, tuple[str, ...]]:
    """Flag unexpected mappings and repeated reachable destinations across rows."""
    owners: dict[TopologyPort, list[TopologyPort]] = {}
    for source, row in rows.items():
        for target in row.targets:
            owners.setdefault(target, []).append(source)
    findings: dict[TopologyPort, tuple[str, ...]] = {}
    for source, row in rows.items():
        flags = []
        if any(len(owners[target]) > 1 for target in row.targets):
            flags.append("DUPLICATE_TARGET")
        if expected_mapping is not None and source in expected_mapping and row.status == "UNIQUE":
            if row.targets != (expected_mapping[source],):
                flags.append("MISWIRE")
        findings[source] = tuple(flags)
    return findings


def resolve_point_recheck(codebook: Codebook, bits: Sequence[int | None],
                          point_bits: Mapping[TopologyPort, int | None]) -> RowDecode:
    """Use full-row point evidence without discarding contradictions with the original code."""
    targets = tuple(port for port in codebook.ports if point_bits.get(port) == 1)
    pending = tuple(port for port in codebook.ports if point_bits.get(port) is None)
    signature = "".join("?" if bit is None else str(bit) for bit in bits)
    if pending:
        return RowDecode("UNKNOWN", targets, pending, signature)
    union = 0
    for port, code in zip(codebook.ports, codebook.codes):
        if port in targets:
            union |= code
    if any(bit is not None and bit != ((union >> index) & 1) for index, bit in enumerate(bits)):
        return RowDecode("INCONSISTENT", targets, (), signature)
    status = "SHORT" if len(targets) > 1 else "UNIQUE" if targets else "NO_CONTINUITY"
    return RowDecode(status, targets, (), signature)


@dataclass(frozen=True)
class _RecheckSnapshot:
    """Keep the last saved coded scan in memory for an explicitly requested point-only run."""

    session_id: int
    parameters: dict[str, object]
    report_path: str
    code_samples: dict[tuple[int, int], Measurement]
    point_samples: dict[tuple[int, int], Measurement]
    coded_targets: dict[TopologyPort, tuple[TopologyPort, ...]]
    verified_sources: frozenset[TopologyPort]
    pending: dict[TopologyPort, tuple[TopologyPort, ...]]


class _Cancelled(Exception):
    """Unwind the worker into the shared cleanup and partial-report path."""


class TopologyScanController:
    """Load remote masks and consume autonomous master scan result streams.

The router callback must return its normal FORWARDED acknowledgement and feed
every matching RESULT frame back through feed_result. The controller owns
both masters until terminal cleanup finishes; GUI manual actions must honor
running. Each connected side currently uses contiguous slave addresses.
"""

    def __init__(
        self,
        send_request: SendRequest,
        publish_event: PublishEvent,
        *,
        report_root: Path = REPORT_ROOT,
        response_timeout_seconds: float = 30.0,
        stream_timeout_seconds: float = 90.0,
    ) -> None:
        if any(not math.isfinite(value) or value <= 0 for value in (response_timeout_seconds, stream_timeout_seconds)):
            raise ValueError("response timeouts must be positive")
        self._send_request = send_request
        self._publish_event = publish_event
        self._report_root = Path(report_root)
        self._response_timeout = response_timeout_seconds
        self._stream_timeout = stream_timeout_seconds
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[str, queue.Queue[str]]] = {}
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._sequence = 0
        self._masters: tuple[str, ...] = ()
        self._offline: set[str] = set()
        self._disconnected: set[str] = set()
        self._connection_times: dict[str, float] = {}
        self._connection_generations: dict[str, int] = {}
        self._connection_error: str | None = None
        self._transport_events: deque[dict[str, object]] = deque(maxlen=MAX_TRANSPORT_EVENTS)
        self._transport_events_dropped = 0
        self._started_monotonic = 0.0
        self._reliable = False
        self._transfer_mode = "unnegotiated"
        self._reliable_store: TopologyTransferStore | None = None
        self._reliable_job = 0
        self._reliable_recovery = threading.Event()
        self._reliable_rebooted = False
        self._recovered_sessions: list[dict[str, object]] = []
        self._last_recheck: _RecheckSnapshot | None = None

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(
        self,
        left_master: str,
        right_master: str,
        left_modules: int = 7,
        right_modules: int = 7,
        *,
        on_threshold_ohm: float = 50.0,
        off_threshold_ohm: float = 100.0,
        settle_seconds: float = 1.0,
        expected_mapping: Mapping[TopologyPort, TopologyPort] | None = None,
        scan_method: str = "coded",
        binary_repeats: int = 1,
        recheck_session_id: int | None = None,
    ) -> bool:
        """Validate an isolated two-master run before launching its worker."""
        masters = (str(left_master).strip(), str(right_master).strip())
        if any(not re.fullmatch(r"[A-Za-z0-9_.:-]{1,31}", value) for value in masters):
            raise ValueError("master IDs must be nonempty ASCII routing identifiers")
        if masters[0] == masters[1]:
            raise ValueError("left and right masters must be different devices")
        if scan_method not in {"coded", "binary"}:
            raise ValueError("scan_method must be coded or binary")
        if type(binary_repeats) is not int or not 1 <= binary_repeats <= MAX_BINARY_REPEATS:
            raise ValueError(f"binary_repeats must be an integer in 1..{MAX_BINARY_REPEATS}")
        if any(type(count) is not int or not 1 <= count <= MAX_MODULES for count in (left_modules, right_modules)):
            raise ValueError("each side needs 1..10 slave modules")
        if not math.isfinite(settle_seconds) or not 0 <= settle_seconds <= 5:
            raise ValueError("settle_seconds must be in 0..5")
        classify_measurement("", on_threshold_ohm, off_threshold_ohm)
        left_ports = build_ports(range(left_modules))
        codebook = Codebook.create(build_ports(range(right_modules)))
        expected = None if expected_mapping is None else dict(expected_mapping)
        if expected is not None and any(source not in left_ports or target not in codebook.ports for source, target in expected.items()):
            raise ValueError("expected mapping refers to an unconfigured port")
        parameters = dict(left_master=masters[0], right_master=masters[1],
                          left_modules=left_modules, right_modules=right_modules,
                          on_threshold_ohm=on_threshold_ohm, off_threshold_ohm=off_threshold_ohm,
                          settle_seconds=round(settle_seconds * 1000) / 1000,
                          expected_mapping=expected, scan_method=scan_method, binary_repeats=binary_repeats)
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            recheck = None
            if recheck_session_id is not None:
                recheck = self._last_recheck
                if type(recheck_session_id) is not int or recheck is None or recheck.session_id != recheck_session_id:
                    raise ValueError("补测对应的扫描已失效，请重新扫描")
                if parameters != recheck.parameters:
                    raise ValueError("补测必须使用原扫描的主机、端口范围和测量参数")
                if not recheck.pending:
                    raise ValueError("当前没有待补测端口")
            self._last_recheck = None
            self._cancel.clear()
            self._masters = masters
            self._offline.clear()
            self._disconnected.clear()
            self._connection_times.clear()
            self._connection_generations.clear()
            self._connection_error = None
            self._transport_events.clear()
            self._transport_events_dropped = 0
            self._started_monotonic = time.monotonic()
            self._reliable = False
            self._transfer_mode = "unnegotiated"
            self._reliable_store = None
            self._reliable_job = 0
            self._reliable_recovery.clear()
            self._reliable_rebooted = False
            self._recovered_sessions.clear()
            thread = threading.Thread(
                target=self._run,
                args=(*masters, left_modules, right_modules, left_ports, codebook, on_threshold_ohm, off_threshold_ohm, round(settle_seconds * 1000), expected, scan_method, binary_repeats, recheck),
                name="cable-topology-scan",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        return True

    def cancel(self) -> None:
        """Request abort; keep ownership until masters have been reset."""
        self._cancel.set()

    def feed_transport(self, event: Mapping[str, object]) -> None:
        """Record this scan's router evidence without depending on Tk polling.

        Reliable sessions pause on TCP replacement; legacy sessions become
        invalid. Debug-slave links do not own the scan.
        """
        with self._lock:
            if not self._masters or not any(
                event.get(key) in self._masters for key in ("peer_id", "source_id", "target_id")
            ):
                return
            observed = float(event.get("monotonic", time.monotonic()))
            entry = dict(event)
            entry.setdefault("timestamp", datetime.now().astimezone().isoformat())
            entry["elapsed_ms"] = round((observed - self._started_monotonic) * 1000, 3)
            if len(self._transport_events) == MAX_TRANSPORT_EVENTS:
                self._transport_events_dropped += 1
            self._transport_events.append(entry)
            peer = str(event.get("peer_id", ""))
            kind = event.get("event")
            if peer not in self._masters or kind not in {"connected", "disconnected"}:
                return
            if kind == "disconnected":
                self._disconnected.add(peer)
                self._connection_generations[peer] = self._connection_generations.get(peer, 0) + 1
                if self._reliable:
                    self._reliable_recovery.set()
                elif self._connection_error is None:
                    reason = event.get("reason", "unknown")
                    self._connection_error = f"{peer} CONNECTION_LOST reason={reason}; scan session invalidated"
            # Registration and old-handler teardown can publish on different threads.
            if observed >= self._connection_times.get(peer, float("-inf")):
                self._connection_times[peer] = observed
                if kind == "connected":
                    self._offline.discard(peer)
                else:
                    self._offline.add(peer)

    def _check_connection(self, target: str, *, cleanup: bool = False) -> None:
        """Stop normal work on a broken session; cleanup may use a reconnected peer."""
        with self._lock:
            failure = self._connection_error
            offline = target in self._offline
        if not cleanup and failure:
            raise ConnectionError(failure)
        if offline:
            raise ConnectionError(f"{target} CONNECTION_LOST; device is offline")

    def feed_result(self, target_id: str, request_id: str, payload: str) -> bool:
        """Queue acknowledgements and samples, rejecting foreign or late frames."""
        with self._lock:
            pending = self._pending.get(request_id)
        if pending is None or target_id != pending[0]:
            return False
        pending[1].put_nowait(payload)
        return True

    def _open_request(self, target: str, sid: int, command: str) -> tuple[str, queue.Queue[str]]:
        with self._lock:
            self._sequence += 1
            request_id = f"TS-{sid:08x}-{self._sequence}"
            inbox: queue.Queue[str] = queue.Queue()
            self._pending[request_id] = (target, inbox)
        try:
            self._publish_event("sent", f"GUI -> {target} {request_id} {command}")
            acknowledgement = self._send_request(target, request_id, command)
            self._publish_event("received", acknowledgement)
            if not acknowledgement.startswith("OK FORWARDED "):
                raise RuntimeError(acknowledgement)
        except Exception:
            self._close_request(request_id)
            raise
        return request_id, inbox

    def _close_request(self, request_id: str) -> None:
        with self._lock:
            self._pending.pop(request_id, None)

    def _request_one(
        self, target: str, sid: int, command: str, *, allow_cancel: bool = True,
        deadline: float | None = None,
    ) -> str:
        if allow_cancel and self._cancel.is_set():
            raise _Cancelled()
        self._check_connection(target, cleanup=not allow_cancel)
        with self._lock:
            generation = self._connection_generations.get(target, 0)
        request_id, inbox = self._open_request(target, sid, command)
        deadline = min(deadline, time.monotonic() + self._response_timeout) if deadline is not None else time.monotonic() + self._response_timeout
        try:
            while True:
                if allow_cancel and self._cancel.is_set():
                    raise _Cancelled()
                self._check_connection(target, cleanup=not allow_cancel)
                with self._lock:
                    replaced = generation != self._connection_generations.get(target, 0)
                if replaced:
                    raise ConnectionError(f"{target} CONNECTION_LOST during {command.split()[0]}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"{target} {command.split()[0]} RESULT_TIMEOUT")
                try:
                    return inbox.get(timeout=min(0.1, remaining))
                except queue.Empty:
                    pass
        finally:
            self._close_request(request_id)

    def _expect(self, target: str, sid: int, command: str, expected: str, *, allow_cancel: bool = True) -> str:
        payload = (self._reliable_rpc(target, sid, command) if self._reliable and allow_cancel
                   else self._request_one(target, sid, command, allow_cancel=allow_cancel))
        if payload != expected:
            raise RuntimeError(f"{target}: expected {expected}; received {payload}")
        return payload

    @staticmethod
    def _retryable_transfer_error(error: Exception) -> bool:
        """Retry transport interruption, while preserving device/protocol errors as failures."""
        return isinstance(error, (ConnectionError, TimeoutError)) or str(error).startswith((
            "ERR DELIVERY_FAILED ", "ERR TARGET_NOT_CONNECTED ", "ERR SERVER_NOT_RUNNING"
        ))

    def _resume_reliable(self, sid: int) -> None:
        """Keep the scan owned during TCP recovery; restore the receiver before the source."""
        self._publish_event("topology_paused", {"reason": "NETWORK"})
        while True:
            if self._cancel.is_set():
                raise _Cancelled()
            with self._lock:
                offline = bool(self._offline)
                generations = dict(self._connection_generations)
            if offline:
                time.sleep(0.1)
                continue
            try:
                if self._reliable_job:
                    for master in reversed(self._masters):
                        reply = self._request_one(master, sid, f"TOPO_RESUME {sid}")
                        if master == self._masters[0] and "REBOOT_REQUIRES_NEW_SCAN" in reply:
                            self._reliable_rebooted = True
                            continue
                        if reply != "OK TOPO_RESUME":
                            raise RuntimeError(f"{master}: {reply}")
                with self._lock:
                    if self._offline or generations != self._connection_generations:
                        continue
                    self._reliable_recovery.clear()
                self._publish_event("topology_resumed", {})
                return
            except Exception as error:
                if not self._retryable_transfer_error(error):
                    raise
                time.sleep(0.1)

    def _reliable_rpc(self, target: str, sid: int, command: str) -> str:
        """Retry the identical idempotent command after a lost response or TCP replacement."""
        while True:
            if self._cancel.is_set():
                raise _Cancelled()
            if self._reliable_recovery.is_set():
                self._resume_reliable(sid)
            if self._reliable_rebooted and command.startswith(("TOPO_RUN2 ", "TOPO_POINT2 ", "TOPO_RANGE2 ")):
                return f"OK {command.split()[0]}"
            try:
                return self._request_one(target, sid, command)
            except Exception as error:
                if not self._retryable_transfer_error(error):
                    raise
                self._reliable_recovery.set()

    def _restore_previous_module_count(self, master: str, sid: int, path: Path) -> None:
        """Restore reboot-lost cleanup scope from the old journal, never the new scan.

        DISCOVER only probes capabilities and selects a module count. A live
        job/plan rejects it as BUSY and keeps its own authoritative cleanup scope.
        Missing old configuration leaves the firmware's existing scope intact.
        """
        configuration = TopologyTransferStore.read_configuration(path, sid)
        if configuration is None:
            return
        sides = [side for side in ("left", "right") if configuration.get(f"{side}_master") == master]
        if len(sides) != 1:
            raise ReplayConflict(f"{master} is not uniquely identified in previous session {sid}")
        count = configuration.get(f"{sides[0]}_modules")
        if type(count) is not int or not 1 <= count <= MAX_MODULES:
            raise ReplayConflict(f"{master} previous session {sid} has invalid module count")
        reply = self._request_one(master, sid, f"TOPO_DISCOVER {count}")
        if reply == "ERR TOPO_DISCOVER BUSY":
            return
        match = re.fullmatch(r"OK TOPO_DISCOVER count=(\d+) online=([0-9a-fA-F]{8})", reply)
        if match is None or int(match[1]) != count or int(match[2], 16) != (1 << count) - 1:
            raise RuntimeError(f"{master} previous session {sid} missing configured slaves: {reply}")

    def _recover_previous_cache(self, master: str, capabilities: Mapping[str, str]) -> None:
        """Salvage an older device session to its own durable files before creating a new one."""
        for field in ("cache_session", "cache_ack", "cache_next", "plan_session"):
            if field in capabilities and not capabilities[field].isdigit():
                raise ReplayConflict(f"invalid {field} capability")
        sid = int(capabilities.get("cache_session", "0"))
        if sid:
            acknowledged = int(capabilities.get("cache_ack", "0"))
            next_sequence = int(capabilities.get("cache_next", "1"))
            if acknowledged >= next_sequence:
                raise ReplayConflict("invalid previous-session cache cursor")
            sessions = self._report_root / "sessions"
            candidates = sorted(sessions.glob(f"*_{sid:08x}.sqlite3"), key=lambda path: path.stat().st_mtime, reverse=True)
            path = candidates[0] if candidates else sessions / f"recovered_{sid:08x}.sqlite3"
            if candidates:
                self._restore_previous_module_count(master, sid, path)
            reset = None
            if next_sequence == acknowledged + 1:
                reset = self._request_one(master, sid, f"TOPO_RESET {sid}")
            if reset != "OK TOPO_RESET":
                self._publish_event("topology_salvaging", {"master": master, "session_id": sid})
                self._expect(master, sid, f"TOPO_ABORT {sid}", "OK TOPO_ABORT")
                store = TopologyTransferStore(path, sid, acknowledged_prefix=acknowledged)
                previous_store = self._reliable_store
                self._reliable_store = store
                artifact = {"session_id": sid, "master": master, "sqlite": str(path)}
                self._recovered_sessions.append(artifact)
                deadline = time.monotonic() + self._response_timeout
                corruptions = 0
                try:
                    if store.cursor < acknowledged:
                        raise ReplayConflict("device acknowledgement exceeds the previous host journal")
                    while True:
                        frames, manifest = self._fetch_reliable(master, sid, deadline=deadline)
                        damaged = False
                        for frame in frames:
                            try:
                                store.accept(TransferRecord.parse(frame, sid))
                            except (TransferCorruption, SequenceGap):
                                damaged = True
                                break
                        ack = store.acknowledgement()
                        if ack is not None:
                            sequence, crc = ack
                            self._expect(master, sid, f"TOPO_ACK {sid} {sequence} {crc}", "OK TOPO_ACK")
                        if damaged:
                            corruptions += 1
                            if corruptions >= 3:
                                raise TransferCorruption("previous cache remains damaged after three fetches")
                            continue
                        corruptions = 0
                        if store.cursor + 1 >= int(manifest["next"]) and manifest["state"] in {"IDLE", "DONE", "FAILED", "STOPPED", "RECOVERED"}:
                            break
                        if not frames:
                            time.sleep(0.1)
                    recovered_json = path.with_suffix(".json")
                    with recovered_json.open("w", encoding="utf-8") as output:
                        json.dump(store.export(), output, ensure_ascii=False, indent=2)
                    artifact.update(json=str(recovered_json), durable_sequence=store.cursor)
                    self._expect(master, sid, f"TOPO_RESET {sid}", "OK TOPO_RESET")
                finally:
                    store.close()
                    self._reliable_store = previous_store
        plan = int(capabilities.get("plan_session", "0"))
        if plan and plan != sid:
            self._expect(master, plan, f"TOPO_RESET {plan}", "OK TOPO_RESET")

    def _fetch_reliable(self, master: str, sid: int, *, allow_cancel: bool = True, deadline: float | None = None) -> tuple[list[str], dict[str, str]]:
        """Collect one bounded batch and its manifest; device records are not ACKed here."""
        store = self._reliable_store
        assert store is not None
        self._check_connection(master, cleanup=not allow_cancel)
        with self._lock:
            generation = self._connection_generations.get(master, 0)
        request_id, inbox = self._open_request(master, sid, f"TOPO_FETCH {sid} {store.cursor + 1} 16")
        until = time.monotonic() + self._response_timeout
        if deadline is not None:
            until = min(until, deadline)
        frames: list[str] = []
        try:
            while True:
                if allow_cancel and self._cancel.is_set():
                    raise _Cancelled()
                if inbox.empty():
                    self._check_connection(master, cleanup=not allow_cancel)
                    with self._lock:
                        if generation != self._connection_generations.get(master, 0):
                            raise ConnectionError(f"{master} connection changed during TOPO_FETCH")
                remaining = until - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"{master} TOPO_FETCH RESULT_TIMEOUT")
                try:
                    payload = inbox.get(timeout=min(0.1, remaining))
                except queue.Empty:
                    continue
                if payload.startswith("TOPO_DATA "):
                    if len(frames) >= 16:
                        raise ReplayConflict("TOPO_FETCH exceeded requested batch size")
                    frames.append(payload)
                    continue
                fields = payload.split()
                if fields[:2] != ["OK", "TOPO_FETCH"]:
                    raise RuntimeError(f"{master}: {payload}")
                manifest: dict[str, str] = {}
                for field in fields[2:]:
                    key, separator, value = field.partition("=")
                    if not separator or not value or key in manifest:
                        raise ReplayConflict("malformed TOPO_FETCH manifest")
                    manifest[key] = value
                required = {"session", "first", "next", "ack", "used", "job", "state", "reason", "high", "low"}
                if not required.issubset(manifest) or any(not manifest[key].isdigit() for key in required - {"state", "reason"}):
                    raise ReplayConflict("incomplete TOPO_FETCH manifest")
                if int(manifest["session"]) != sid or manifest["high"] != "70" or manifest["low"] != "50":
                    raise ReplayConflict("TOPO_FETCH session or cache thresholds differ from negotiation")
                if manifest["state"] not in {"IDLE", "RUNNING", "PAUSED", "DONE", "FAILED", "STOPPED", "RECOVERED"} or manifest["reason"] not in {"NONE", "NETWORK", "CACHE", "CONTROL", "STORAGE", "TASK"}:
                    raise ReplayConflict("unknown TOPO_FETCH state or pause reason")
                if not 0 <= int(manifest["ack"]) < int(manifest["first"]) <= int(manifest["next"]) or not 0 <= int(manifest["used"]) <= 100:
                    raise ReplayConflict("invalid TOPO_FETCH cache cursor")
                if int(manifest["ack"]) > store.cursor or int(manifest["first"]) > store.cursor + 1:
                    raise ReplayConflict("device discarded records without a durable host acknowledgement")
                return frames, manifest
        finally:
            self._close_request(request_id)

    def _stream_reliable(
        self, left_master: str, sid: int, command: str, expected_samples: int,
        on_sample: Callable[[str], None], on_frame: Callable[[str], None],
    ) -> None:
        """Fetch, durably commit, and ACK each job, resuming only its existing identity."""
        store = self._reliable_store
        assert store is not None
        if self._cancel.is_set():
            raise _Cancelled()
        self._reliable_job += 1
        job = self._reliable_job
        parts = command.split(maxsplit=2)
        operation = parts[0] + "2"
        reliable_command = f"{operation} {sid} {job} {parts[2]}"
        store.record_job(job, reliable_command)
        samples = 0
        terminal: str | None = None
        corruptions = 0
        aborting = False
        abort_deadline: float | None = None
        try:
            try:
                reply = self._reliable_rpc(left_master, sid, reliable_command)
                if reply != f"OK {operation}":
                    raise RuntimeError(f"{left_master}: {reply}")
            except _Cancelled:
                pass
            while True:
                if self._cancel.is_set() and not aborting:
                    aborting = True
                    abort_deadline = time.monotonic() + self._response_timeout
                    self._expect(left_master, sid, f"TOPO_ABORT {sid}", "OK TOPO_ABORT", allow_cancel=False)
                if self._reliable_recovery.is_set() and not aborting:
                    try:
                        self._resume_reliable(sid)
                    except _Cancelled:
                        continue
                try:
                    frames, manifest = self._fetch_reliable(left_master, sid, allow_cancel=not aborting, deadline=abort_deadline)
                except _Cancelled:
                    continue
                except Exception as error:
                    if not aborting and self._retryable_transfer_error(error):
                        self._reliable_recovery.set()
                        continue
                    raise
                damaged = False
                for frame in frames:
                    try:
                        record = TransferRecord.parse(frame, sid)
                        if record.sequence <= store.cursor:
                            store.accept(record)
                            continue
                        if record.job != job:
                            raise ReplayConflict(f"unexpected job {record.job}; active job is {job}")
                        if record.sequence != store.cursor + 1:
                            raise SequenceGap("TOPO_FETCH omitted a sequence")
                        tokens = record.payload.split(maxsplit=2)
                        if len(tokens) < 2 or tokens[1] != str(sid):
                            raise ReplayConflict("cached payload belongs to another session")
                        prefix = {"TOPO_POINT2": "TOPO_POINT_SAMPLE", "TOPO_RANGE2": "TOPO_RANGE_SAMPLE"}.get(operation, "TOPO_SAMPLE")
                        if tokens[0] == prefix:
                            if terminal is not None or samples >= expected_samples:
                                raise ReplayConflict("unexpected cached sample after job completion")
                            on_sample(record.payload)
                            samples += 1
                        elif tokens[0] in {"TOPO_DONE", "TOPO_STOPPED", "TOPO_FAILED"}:
                            if terminal is not None:
                                raise ReplayConflict("multiple terminals for one cached job")
                            if tokens[0] == "TOPO_DONE" and (len(tokens) != 3 or tokens[2] != str(samples) or samples != expected_samples):
                                raise ReplayConflict("cached job completed with missing measurements")
                            terminal = record.payload
                        else:
                            raise ReplayConflict(f"unexpected cached payload: {record.payload}")
                        store.accept(record)
                        on_frame(record.payload)
                    except (TransferCorruption, SequenceGap):
                        damaged = True
                        break
                acknowledgement = store.acknowledgement()
                if acknowledgement is not None:
                    sequence, crc = acknowledgement
                    ack_command = f"TOPO_ACK {sid} {sequence} {crc}"
                    try:
                        reply = (self._request_one(left_master, sid, ack_command, allow_cancel=False, deadline=abort_deadline)
                                 if aborting else self._reliable_rpc(left_master, sid, ack_command))
                    except _Cancelled:
                        continue
                    if reply != "OK TOPO_ACK":
                        raise RuntimeError(f"{left_master}: {reply}")
                if damaged:
                    corruptions += 1
                    if corruptions >= 3:
                        raise TransferCorruption("cached record remains damaged or missing after three fetches")
                    continue
                corruptions = 0
                if (aborting and int(manifest["job"]) < job
                        and manifest["state"] in {"IDLE", "DONE", "FAILED", "STOPPED"}
                        and store.cursor + 1 >= int(manifest["next"])):
                    raise _Cancelled()
                if manifest["state"] == "RECOVERED" or self._reliable_rebooted:
                    if store.cursor + 1 < int(manifest["next"]):
                        continue
                    raise RuntimeError("SOURCE_REBOOTED: cached measurements recovered; start a new scan")
                if terminal is not None:
                    if aborting:
                        raise _Cancelled()
                    if terminal.startswith("TOPO_STOPPED "):
                        raise RuntimeError(f"master stopped unexpectedly: {terminal}")
                    if terminal.startswith("TOPO_FAILED "):
                        raise RuntimeError(terminal)
                    return
                if manifest["state"] in {"DONE", "FAILED", "STOPPED"} and store.cursor + 1 >= int(manifest["next"]):
                    raise ReplayConflict(f"device {manifest['state']} reason={manifest['reason']} has no durable terminal record")
                if manifest["state"] == "PAUSED":
                    self._publish_event("topology_paused", {"reason": manifest["reason"], "used": int(manifest["used"])})
                    if manifest["reason"] != "CACHE" or int(manifest["used"]) < 50:
                        self._reliable_recovery.set()
                elif frames:
                    self._publish_event("topology_resumed", {"used": int(manifest["used"])})
                if not frames:
                    time.sleep(0.1)
        except Exception:
            # A storage/protocol failure must leave unacknowledged flash data intact.
            if not aborting and terminal is None:
                try:
                    self._expect(left_master, sid, f"TOPO_ABORT {sid}", "OK TOPO_ABORT", allow_cancel=False)
                except Exception:
                    pass
            raise

    def _reset_master(self, master: str, sid: int) -> None:
        """Allow bounded reconnection and firmware cleanup before requiring RESET acknowledgement."""
        deadline = time.monotonic() + self._response_timeout
        while True:
            with self._lock:
                recovering = self._connection_error is not None
                offline = master in self._offline
            if not offline:
                try:
                    reply = self._request_one(master, sid, f"TOPO_RESET {sid}", allow_cancel=False, deadline=deadline)
                except (ConnectionError, RuntimeError) as failure:
                    if not isinstance(failure, ConnectionError) and not str(failure).startswith((
                        f"ERR DELIVERY_FAILED {master} ", f"ERR TARGET_NOT_CONNECTED {master} "
                    )):
                        raise
                    reply = "CONNECTION_LOST"
                    recovering = True
                if reply == "OK TOPO_RESET":
                    return
                waiting_for_worker = self._reliable and reply == "ERR TOPO_RESET BUSY_USE_ABORT"
                if not (recovering or waiting_for_worker) or reply not in {
                    "CONNECTION_LOST", "ERR TOPO_RESET BUSY_USE_ABORT", "ERR TOPO_RESET RECOVERY_PENDING"
                }:
                    raise RuntimeError(f"{master}: expected OK TOPO_RESET; received {reply}")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{master} RESET_UNCONFIRMED after connection recovery")
            time.sleep(min(0.05, max(0.001, deadline - time.monotonic())))

    def _stream(
        self,
        left_master: str,
        sid: int,
        command: str,
        expected_samples: int,
        on_sample: Callable[[str], None],
        on_frame: Callable[[str], None],
    ) -> None:
        """Keep the request registered through DONE, including every sample."""
        if self._reliable:
            self._stream_reliable(left_master, sid, command, expected_samples, on_sample, on_frame)
            return
        if self._cancel.is_set():
            raise _Cancelled()
        self._check_connection(left_master)
        request_id, inbox = self._open_request(left_master, sid, command)
        deadline = time.monotonic() + self._stream_timeout
        samples = 0
        abort_sent = False
        terminal_received = False
        try:
            while True:
                # Consume samples received before the disconnect before ending the run.
                if inbox.empty():
                    self._check_connection(left_master)
                if self._cancel.is_set() and not abort_sent:
                    self._expect(left_master, sid, f"TOPO_ABORT {sid}", "OK TOPO_ABORT", allow_cancel=False)
                    abort_sent = True
                    deadline = time.monotonic() + self._response_timeout
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"{left_master} TOPO stream timeout")
                try:
                    payload = inbox.get(timeout=min(0.1, remaining))
                except queue.Empty:
                    continue
                on_frame(payload)
                deadline = time.monotonic() + (self._response_timeout if abort_sent else self._stream_timeout)
                if payload == f"OK {command.split()[0]}":
                    continue
                tokens = payload.split(maxsplit=2)
                if len(tokens) < 2 or tokens[1] != str(sid):
                    raise RuntimeError(f"unexpected topology stream frame: {payload}")
                if tokens[0] == "TOPO_DONE":
                    terminal_received = True
                    if abort_sent:
                        raise _Cancelled()
                    if len(tokens) != 3 or tokens[2] != str(samples) or samples != expected_samples:
                        raise RuntimeError(f"incomplete topology stream: {payload}, observed={samples}, expected={expected_samples}")
                    self._check_connection(left_master)
                    return
                if tokens[0] == "TOPO_STOPPED":
                    terminal_received = True
                    if self._cancel.is_set():
                        raise _Cancelled()
                    raise RuntimeError(f"master stopped unexpectedly: {payload}")
                if tokens[0] == "TOPO_FAILED":
                    terminal_received = True
                    raise RuntimeError(payload)
                expected_prefix = "TOPO_POINT_SAMPLE" if command.startswith("TOPO_POINT ") else "TOPO_SAMPLE"
                if tokens[0] != expected_prefix or samples >= expected_samples:
                    raise RuntimeError(f"unexpected topology sample: {payload}")
                if abort_sent:
                    continue
                on_sample(payload)
                samples += 1
        except Exception as failure:
            with self._lock:
                source_disconnected = left_master in self._disconnected
            # A reconnected source discarded its worker and cannot emit its old terminal frame.
            if not terminal_received and not source_disconnected:
                try:
                    if not abort_sent:
                        self._expect(left_master, sid, f"TOPO_ABORT {sid}", "OK TOPO_ABORT", allow_cancel=False)
                    abort_deadline = time.monotonic() + self._response_timeout
                    while time.monotonic() < abort_deadline:
                        with self._lock:
                            source_disconnected = left_master in self._disconnected
                        if source_disconnected:
                            break
                        try:
                            payload = inbox.get(timeout=min(0.1, max(0.001, abort_deadline - time.monotonic())))
                        except queue.Empty:
                            continue
                        on_frame(payload)
                        parts = payload.split(maxsplit=2)
                        if len(parts) >= 2 and parts[1] == str(sid) and parts[0] in {"TOPO_DONE", "TOPO_STOPPED", "TOPO_FAILED"}:
                            terminal_received = True
                            break
                    if not terminal_received and not source_disconnected:
                        raise TimeoutError("master did not confirm abort completion")
                except Exception as abort_error:
                    with self._lock:
                        source_disconnected = left_master in self._disconnected
                    if not source_disconnected:
                        raise RuntimeError(f"{failure}; abort failed: {abort_error}") from failure
            raise
        finally:
            self._close_request(request_id)

    def _run(
        self,
        left_master: str,
        right_master: str,
        left_modules: int,
        right_modules: int,
        left_ports: tuple[TopologyPort, ...],
        codebook: Codebook,
        on_threshold: float,
        off_threshold: float,
        settle_ms: int,
        expected_mapping: Mapping[TopologyPort, TopologyPort] | None,
        scan_method: str = "coded",
        binary_repeats: int = 1,
        recheck: _RecheckSnapshot | None = None,
    ) -> None:
        started_at = datetime.now().astimezone()
        sid = uuid.uuid4().int & 0xFFFFFFFF or 1
        total = len(left_ports) * codebook.rounds if scan_method == "coded" else 0
        code_samples = dict(recheck.code_samples) if recheck else {}
        point_samples = dict(recheck.point_samples) if recheck else {}
        coded_targets = dict(recheck.coded_targets) if recheck else {}
        verified_sources = set(recheck.verified_sources) if recheck else set()
        recheck_samples: dict[tuple[int, int], Measurement] = {}
        if recheck:
            total = sum(len(targets) for targets in recheck.pending.values())
            verified_sources.update(recheck.pending)
            # Old readings cannot stand in for measurements requested in this new pass.
            for source, targets in recheck.pending.items():
                for target in targets:
                    point_samples.pop((source.global_port, target.global_port), None)
        binary_searches: dict[TopologyPort, BinaryRowSearch] = {}
        binary_samples: list[dict[str, object]] = []
        raw_frames: list[str] = []
        discovery: dict[str, dict[str, object]] = {}
        reliable_capabilities: list[bool] = []
        owned: list[str] = []
        cleanup_errors: list[str] = []
        error: str | None = None
        cancelled = False
        self._publish_event("topology_started", {
            "left_master": left_master, "right_master": right_master,
            "left_ports": len(left_ports), "right_ports": len(codebook.ports),
            "rounds": codebook.rounds if scan_method == "coded" else 0, "total": total,
            "scan_method": scan_method,
            "binary_confirmation_repeats": binary_repeats if scan_method == "binary" else None,
            "recheck": recheck is not None,
        })

        def decode_source(source: TopologyPort) -> RowDecode:
            if scan_method == "binary":
                search = binary_searches.get(source)
                if search is None:
                    return RowDecode("UNKNOWN", (), codebook.ports, "")
                return RowDecode(search.status, tuple(codebook.ports[index] for index in search.targets),
                                 tuple(port for index, port in enumerate(codebook.ports)
                                       if not search.complete or search.decisions.get(index) is None), "")
            bits = [code_samples[(source.global_port, index)].bit if (source.global_port, index) in code_samples else None for index in range(codebook.rounds)]
            point_bits = {target: point_samples[(source.global_port, target.global_port)].bit for target in codebook.ports if (source.global_port, target.global_port) in point_samples}
            if source in verified_sources:
                return resolve_point_recheck(codebook, bits, point_bits)
            return resolve_row(codebook, bits, point_bits) if point_bits else decode_signature(codebook, bits)

        def row_payload(source: TopologyPort, flags: Sequence[str] = ()) -> dict[str, object]:
            result = _row_payload(source, decode_source(source), flags, point_samples=point_samples, coded_targets=coded_targets.get(source, ()))
            if scan_method == "binary":
                search = binary_searches.get(source)
                result.update(scan_method=scan_method, binary_complete=bool(search and search.complete),
                              binary_conflicts=list(search.conflicts) if search else [])
            if source in verified_sources:
                measured = sum((source.global_port, target.global_port) in point_samples for target in codebook.ports)
                result.update(point_recheck_complete=not decode_source(source).candidates,
                              point_recheck_measured=measured, point_recheck_total=len(codebook.ports))
            return result

        def binary_probe(source: TopologyPort, first: int, end: int) -> int | None:
            """Collect the selected number of durable readings for each group or point probe."""
            nonlocal total
            self._publish_event("topology_row", row_payload(source))
            readings: list[Measurement] = []
            single = end - first == 1
            total += binary_repeats
            for attempt in range(binary_repeats):
                def received(payload: str) -> None:
                    fields = payload.split(maxsplit=4 if single else 5)
                    coordinates = [str(source.global_port), str(first)] + ([] if single else [str(end)])
                    if len(fields) != (5 if single else 6) or fields[2:-1] != coordinates:
                        raise ReplayConflict(f"unexpected binary coordinates: {payload}")
                    reading = classify_measurement(fields[-1], on_threshold, off_threshold)
                    readings.append(reading)
                    binary_samples.append({"source": source.global_port, "first": first, "end": end,
                                           "attempt": attempt + 1, "job_id": self._reliable_job,
                                           "kind": "point" if single else "group", **asdict(reading)})
                    self._publish_event("topology_progress", {"phase": "binary", "completed": len(binary_samples),
                        "total": total, "source": source.label,
                        "target": codebook.ports[first].label if single else f"{codebook.ports[first].label} .. {codebook.ports[end - 1].label}"})
                command = (f"TOPO_POINT {sid} {right_master} {left_modules} {source.global_port} {first} {settle_ms}"
                           if single else f"TOPO_RANGE {sid} {right_master} {left_modules} {source.global_port} {first} {end} {settle_ms}")
                self._stream(left_master, sid, command, 1, received, raw_frames.append)
            confirmed = confirm_binary_readings(readings)
            if single:
                point_samples[(source.global_port, first)] = confirmed
            return confirmed.bit

        def read_point(source: TopologyPort, target: TopologyPort) -> None:
            """Acquire one isolated pair; the same evidence verifies topology and resistance."""
            if (source.global_port, target.global_port) in point_samples:
                return

            def point_received(payload: str) -> None:
                fields = payload.split(maxsplit=4)
                if len(fields) != 5 or fields[2:4] != [str(source.global_port), str(target.global_port)]:
                    raise RuntimeError(f"unexpected point coordinates: {payload}")
                key = (source.global_port, target.global_port)
                if key in point_samples:
                    raise RuntimeError(f"duplicate point sample: {payload}")
                point_samples[key] = classify_measurement(fields[4], on_threshold, off_threshold)
                if recheck:
                    recheck_samples[key] = point_samples[key]

            command = f"TOPO_POINT {sid} {right_master} {left_modules} {source.global_port} {target.global_port} {settle_ms}"
            self._stream(left_master, sid, command, 1, point_received, raw_frames.append)

        def sample_received(payload: str) -> None:
            fields = payload.split(maxsplit=4)
            if len(fields) != 5:
                raise RuntimeError(f"malformed sample: {payload}")
            source_index, round_index = int(fields[2]), int(fields[3])
            if not 0 <= source_index < len(left_ports) or not 0 <= round_index < codebook.rounds:
                raise RuntimeError(f"sample coordinates outside scan plan: {payload}")
            if (source_index, round_index) in code_samples:
                raise RuntimeError(f"duplicate sample: {payload}")
            measurement = classify_measurement(fields[4], on_threshold, off_threshold)
            code_samples[(source_index, round_index)] = measurement
            source = left_ports[source_index]
            self._publish_event("topology_progress", {"phase": "coded", "completed": len(code_samples), "total": total, "source": source.label, "round": round_index})
            if all((source_index, index) in code_samples for index in range(codebook.rounds)):
                coded_targets[source] = decode_source(source).targets
                self._publish_event("topology_row", row_payload(source))

        try:
            if recheck:
                inherited_rows = {source: decode_source(source) for source in left_ports}
                inherited_flags = assess_rows(inherited_rows, expected_mapping)
                for source in left_ports:
                    self._publish_event("topology_row", row_payload(source, inherited_flags[source]))
            for side, master, count in (("left", left_master, left_modules), ("right", right_master, right_modules)):
                info = self._request_one(master, sid, "TOPO_INFO")
                fields = info.split()
                capabilities: dict[str, str] = {}
                valid = fields[:2] == ["OK", "TOPO_INFO"]
                for field in fields[2:]:
                    key, separator, value = field.partition("=")
                    if not separator or not key or not value or key in capabilities:
                        valid = False
                    capabilities[key] = value
                if not valid or capabilities.get("role") != "MASTER" or capabilities.get("bus") != "1":
                    raise RuntimeError(f"{master} is not a bus-ready master: {info}")
                if not capabilities.get("capacity", "").isdigit() or not capabilities.get("configured", "").isdigit():
                    raise RuntimeError(f"{master} returned invalid module capabilities: {info}")
                required_route = "1" if side == "left" else "0"
                if capabilities.get("route") != required_route:
                    raise RuntimeError(f"{master} {side} fixed Kelvin route must be {required_route}: {info}")
                if count > int(capabilities["capacity"]):
                    raise RuntimeError(f"{master} capacity is {capabilities['capacity']}, requested {count}")
                if scan_method == "binary" and (capabilities.get("binary") != "1" or capabilities.get("reliable") != "1"):
                    raise RuntimeError(f"{master} BINARY_UNSUPPORTED: 二分扫描需要两台 Master 都升级到支持区间测量的固件")
                reliable_capabilities.append(capabilities.get("reliable") == "1")
                if capabilities.get("reliable") == "1":
                    if capabilities.get("cache_ready", "1") != "1":
                        self._transfer_mode = "durable_cache_unavailable"
                        detail = capabilities.get("cache_error", "UNKNOWN")
                        raise RuntimeError(f"{master} CACHE_UNAVAILABLE: reliable flash cache initialization failed ({detail})")
                    self._recover_previous_cache(master, capabilities)
                discovered = self._request_one(master, sid, f"TOPO_DISCOVER {count}")
                match = re.fullmatch(r"OK TOPO_DISCOVER count=(\d+) online=([0-9a-fA-F]{8})", discovered)
                if match is None or int(match[1]) != count or int(match[2], 16) != (1 << count) - 1:
                    raise RuntimeError(f"{master} missing configured slaves: {discovered}")
                discovery[side] = {"id": master, "module_count": count, "online": int(match[2], 16)}
                self._publish_event("topology_discovered", {"side": side, "master": master, "count": count, "online": int(match[2], 16)})
            self._publish_event("topology_discovery", discovery)
            self._reliable = all(reliable_capabilities)
            self._transfer_mode = "durable_cached" if self._reliable else "legacy"
            self._publish_event("topology_transfer_mode", {
                "mode": self._transfer_mode,
                "reason": "both_masters_reliable" if self._reliable else "firmware_capability_missing",
            })
            if self._reliable:
                self._reliable_store = TopologyTransferStore(
                    self._report_root / "sessions" / f"session_{started_at:%Y%m%d_%H%M%S}_{sid:08x}.sqlite3",
                    sid,
                    {"left_master": left_master, "right_master": right_master,
                     "left_modules": left_modules, "right_modules": right_modules,
                     "settle_ms": settle_ms, "on_threshold_ohm": on_threshold,
                     "off_threshold_ohm": off_threshold,
                     "scan_method": scan_method,
                     "binary_confirmation_repeats": binary_repeats if scan_method == "binary" else None,
                     "recheck_parent_session": recheck.session_id if recheck else None,
                     "recheck_pairs": [[source.global_port, target.global_port]
                                       for source, targets in recheck.pending.items() for target in targets] if recheck else [],
                     "codebook": [{"port": port.label, "code": code} for port, code in zip(codebook.ports, codebook.codes)]},
                )
            owned.extend((left_master, right_master))
            for master in owned:
                self._expect(master, sid, f"TOPO_RESET {sid}", "OK TOPO_RESET")
            if self._reliable:
                self._expect(left_master, sid, f"TOPO_OPEN {sid}", "OK TOPO_OPEN")
            begin = "TOPO_BEGIN2" if self._reliable else "TOPO_BEGIN"
            self._expect(right_master, sid, f"{begin} {sid} {right_modules} {codebook.rounds}", f"OK {begin}")
            for round_index in range(codebook.rounds):
                for module, mask in codebook.masks_for_round(round_index).items():
                    self._expect(right_master, sid, f"TOPO_MASK {sid} {round_index} {module} {mask:06x}", "OK TOPO_MASK")
            self._expect(right_master, sid, f"TOPO_SEAL {sid}", "OK TOPO_SEAL")
            if recheck:
                for source, targets in recheck.pending.items():
                    for target in targets:
                        if self._cancel.is_set():
                            raise _Cancelled()
                        read_point(source, target)
                        self._publish_event("topology_progress", {"phase": "recheck", "completed": len(recheck_samples),
                            "total": total, "source": source.label, "target": target.label})
                        self._publish_event("topology_row", row_payload(source))
            elif scan_method == "binary":
                for source in left_ports:
                    search = BinaryRowSearch(len(codebook.ports), lambda first, end: binary_probe(source, first, end))
                    binary_searches[source] = search
                    search.run()
                    self._publish_event("topology_row", row_payload(source))
            else:
                self._stream(left_master, sid, f"TOPO_RUN {sid} {right_master} {left_modules} {codebook.rounds} {settle_ms}", total, sample_received, raw_frames.append)
                followups: list[tuple[TopologyPort, tuple[TopologyPort, ...], str]] = []
                for source in left_ports:
                    row = decode_source(source)
                    if row.status == "NO_CONTINUITY":
                        continue
                    candidates = codebook.ports if row.status in {"UNKNOWN", "INCONSISTENT"} else row.candidates
                    phase = "resistance" if row.status == "UNIQUE" else "point"
                    followups.append((source, candidates, phase))
                # Fault follow-ups already provide single-pair readings for resolved links.
                followups.sort(key=lambda item: item[2] == "resistance")
                total += sum(len(candidates) for _, candidates, _ in followups)
                for source, candidates, phase in followups:
                    self._publish_event("topology_progress", {"phase": phase, "completed": len(code_samples) + len(point_samples), "total": total, "source": source.label, "round": -1})
                    for target in candidates:
                        if self._cancel.is_set():
                            raise _Cancelled()
                        read_point(source, target)
                        self._publish_event("topology_progress", {"phase": phase, "completed": len(code_samples) + len(point_samples), "total": total, "source": source.label, "target": target.label, "round": -1})
                        self._publish_event("topology_row", row_payload(source))
        except _Cancelled:
            cancelled = True
        except Exception as failure:
            error = str(failure)
        finally:
            with self._lock:
                recovering = self._connection_error is not None
            if recovering:
                self._publish_event("topology_recovering", {"error": error})
            for master in owned:
                try:
                    self._reset_master(master, sid)
                except Exception as failure:
                    cleanup_errors.append(f"{master}: {failure}")
            with self._lock:
                connection_error = self._connection_error
                transport_events = list(self._transport_events)
                transport_events_dropped = self._transport_events_dropped
                self._masters = ()
            if connection_error and error is None:
                error = connection_error
            if cleanup_errors and error is None:
                error = "matrix cleanup failed; " + "; ".join(cleanup_errors)
            rows = {source: decode_source(source) for source in left_ports}
            findings = assess_rows(rows, expected_mapping)
            row_payloads = [row_payload(source, findings[source]) for source in rows]
            pending_recheck = {}
            if scan_method == "coded":
                for source, row in rows.items():
                    if source in verified_sources:
                        pending = row.candidates
                    else:
                        pending = codebook.ports if row.status in {"UNKNOWN", "INCONSISTENT", "SHORT_CANDIDATES"} else ()
                    if pending:
                        pending_recheck[source] = pending
            recheck_info = ({"parent_session_id": recheck.session_id, "parent_report": recheck.report_path,
                             "planned_pairs": total, "completed_pairs": len(recheck_samples), "samples_per_pair": 1}
                            if recheck else None)
            report = {
                "schema": 2, "session_id": sid, "started_at": started_at.isoformat(),
                "finished_at": datetime.now().astimezone().isoformat(),
                "left_master": left_master, "right_master": right_master,
                "left_modules": left_modules, "right_modules": right_modules,
                "discovery": discovery, "rounds": codebook.rounds if scan_method == "coded" else 0, "weight": codebook.weight if scan_method == "coded" else 0,
                "scan_method": scan_method, "binary_samples": binary_samples,
                "binary_confirmation_repeats": binary_repeats if scan_method == "binary" else None,
                "on_threshold_ohm": on_threshold, "off_threshold_ohm": off_threshold,
                "settle_ms": settle_ms, "cancelled": cancelled, "error": error,
                "cleanup_errors": cleanup_errors, "rows": row_payloads,
                "connection_error": connection_error,
                "transport_events": transport_events,
                "transport_events_dropped": transport_events_dropped,
                "transfer_mode": self._transfer_mode,
                "durable_session": str(self._reliable_store.path) if self._reliable_store else None,
                "durable_sequence": self._reliable_store.cursor if self._reliable_store else 0,
                "recovered_sessions": list(self._recovered_sessions),
                "expected_mapping": None if expected_mapping is None else [{"source": source.label, "destination": target.label} for source, target in expected_mapping.items()],
                "codebook": [{"port": port.label, "code": code} for port, code in zip(codebook.ports, codebook.codes)] if scan_method == "coded" else [],
                "code_samples": [{"source": source, "round": round_index, **asdict(value)} for (source, round_index), value in code_samples.items()],
                "point_samples": [{"source": source, "destination": target, **asdict(value)} for (source, target), value in point_samples.items()],
                "raw_frames": raw_frames,
                "resistance_basis": "single_point_uncalibrated",
                "recheck": recheck_info,
                "recheck_samples": [{"source": source, "destination": target, **asdict(value)} for (source, target), value in recheck_samples.items()],
                "point_rechecked_sources": sorted(source.global_port for source in verified_sources),
                "pending_recheck_count": sum(len(targets) for targets in pending_recheck.values()),
                "limitations": "Reachability under stable low-resistance OR model; no-continuity also includes overrange or contact failure. Hidden short placement and isolated same-side shorts are not resolved. Connection resistances are uncalibrated single-selected-pair XD31H readings, not grouped readings or cable-only resistance. Shorted networks may yield equivalent resistance.",
            }
            output = {"json": "", "csv": ""}
            try:
                output = self._write_reports(report, left_ports, codebook, rows, code_samples, point_samples, sid)
            except Exception as failure:
                error = f"{error + '; ' if error else ''}report write failed: {failure}"
            snapshot = None
            parameters = dict(left_master=left_master, right_master=right_master,
                              left_modules=left_modules, right_modules=right_modules,
                              on_threshold_ohm=on_threshold, off_threshold_ohm=off_threshold,
                              settle_seconds=settle_ms / 1000, expected_mapping=expected_mapping,
                              scan_method=scan_method, binary_repeats=binary_repeats)
            if scan_method == "coded" and code_samples and output["json"] and pending_recheck:
                snapshot = _RecheckSnapshot(sid, parameters, output["json"], code_samples, point_samples,
                                           coded_targets, frozenset(verified_sources), pending_recheck)
            payload = {
                "completed": (len(recheck_samples) if recheck else sum(search.complete for search in binary_searches.values()) if scan_method == "binary" else
                              sum(all((source.global_port, index) in code_samples for index in range(codebook.rounds)) for source in left_ports)),
                "total": total if recheck else len(left_ports),
                "measurements": len(recheck_samples) if recheck else len(binary_samples) if scan_method == "binary" else len(code_samples) + len(point_samples),
                "scan_method": scan_method,
                "binary_confirmation_repeats": binary_repeats if scan_method == "binary" else None,
                "rows": row_payloads, "error": error, "cleanup_errors": cleanup_errors,
                "connection_error": connection_error,
                "transfer_mode": self._transfer_mode,
                "durable_session": str(self._reliable_store.path) if self._reliable_store else None,
                "recovered_sessions": list(self._recovered_sessions),
                "recheck": recheck_info,
                "pending_recheck_count": report["pending_recheck_count"] if snapshot else 0,
                "recheck_parameters": {**parameters, "expected_mapping": None if expected_mapping is None else dict(expected_mapping),
                                       "recheck_session_id": sid} if snapshot else None,
                **output,
            }
            if self._reliable_store is not None:
                self._reliable_store.close()
            with self._lock:
                self._pending.clear()
                self._last_recheck = snapshot
                self._thread = None
            event = "topology_error" if error else ("topology_stopped" if cancelled else "topology_complete")
            self._publish_event(event, payload)

    def _write_reports(
        self, report: dict[str, object], left_ports: Sequence[TopologyPort], codebook: Codebook,
        rows: Mapping[TopologyPort, RowDecode], code_samples: Mapping[tuple[int, int], Measurement],
        point_samples: Mapping[tuple[int, int], Measurement], sid: int,
    ) -> dict[str, str]:
        """Persist partial raw evidence and a matrix whose unknown cells stay '?'."""
        self._report_root.mkdir(parents=True, exist_ok=True)
        stem = f"topology_{datetime.now():%Y%m%d_%H%M%S}_{sid:08x}"
        json_path, csv_path = (self._report_root / f"{stem}.{extension}" for extension in ("json", "csv"))
        with json_path.open("x", encoding="utf-8") as destination:
            json.dump(report, destination, ensure_ascii=False, indent=2)
        with csv_path.open("x", encoding="utf-8-sig", newline="") as destination:
            writer = csv.writer(destination)
            flags = {item["source"]: item["flags"] for item in report["rows"]}
            writer.writerow(["source", "status", "flags", "signature", *[port.label for port in codebook.ports], "code_samples_raw", "point_samples_raw", *[f"{field}:{port.label}" for port in codebook.ports for field in ("resistance_ohm", "resistance_status")], "scan_method", "binary_samples_raw", "binary_confirmation_repeats"])
            for source in left_ports:
                row = rows[source]
                cells = []
                for target in codebook.ports:
                    if row.status in {"UNIQUE", "SHORT", "NO_CONTINUITY"}:
                        cells.append(int(target in row.targets))
                    else:
                        point = point_samples.get((source.global_port, target.global_port))
                        cells.append(point.bit if point is not None and point.bit is not None else "?")
                raw_code = ([code_samples[(source.global_port, index)].raw if (source.global_port, index) in code_samples else None for index in range(codebook.rounds)]
                            if report.get("scan_method", "coded") == "coded" else [])
                raw_point = {target.label: point_samples[(source.global_port, target.global_port)].raw for target in codebook.ports if (source.global_port, target.global_port) in point_samples}
                resistances = []
                for target in codebook.ports:
                    point = point_samples.get((source.global_port, target.global_port))
                    resistances.extend((
                        f"{point.resistance_ohm:.3f}" if point is not None and point.resistance_ohm is not None else "",
                        point.reason if point is not None else "NOT_MEASURED",
                    ))
                binary_raw = [item for item in report.get("binary_samples", []) if item["source"] == source.global_port]
                writer.writerow([source.label, row.status, "|".join(flags[source.label]), row.signature, *cells, json.dumps(raw_code), json.dumps(raw_point), *resistances, report.get("scan_method", "coded"), json.dumps(binary_raw), report.get("binary_confirmation_repeats")])
        return {"json": str(json_path), "csv": str(csv_path)}


def _row_payload(
    source: TopologyPort, row: RowDecode, flags: Sequence[str] = (), *,
    point_samples: Mapping[tuple[int, int], Measurement] | None = None,
    coded_targets: Sequence[TopologyPort] = (),
) -> dict[str, object]:
    """Keep each link's point evidence, including coded links whose verification failed."""
    samples = point_samples if point_samples is not None else {}
    resistances = []
    for target in sorted(set(row.targets) | set(coded_targets)):
        measurement = samples.get((source.global_port, target.global_port), Measurement("", None, "NOT_MEASURED"))
        resistances.append({"target": target.label, "target_global": target.global_port, **asdict(measurement)})
    return {
        "source": source.label, "source_global": source.global_port, "status": row.status,
        "targets": [port.label for port in row.targets],
        "candidates": [port.label for port in row.candidates],
        "signature": row.signature, "flags": list(flags),
        "connection_resistances": resistances,
    }
