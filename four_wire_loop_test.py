"""Automate directed resistance measurements between 24 two-wire groups.

The two CH446X banks expose 48 X ports.  Adjacent ports within each bank form
12 two-wire groups, for 24 groups total.  Every ordered pair of distinct groups
is measured, so the schedule contains 24 * 23 = 552 directions.  For one
direction, the first X in a group is the current lead and the second X is the
voltage lead; the destination group is wired to the negative Kelvin leads.
"""

from __future__ import annotations

import json
import queue
import re
import subprocess
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence


PORTS_PER_BANK = 24
WIRES_PER_GROUP = 2
GROUPS_PER_BANK = PORTS_PER_BANK // WIRES_PER_GROUP
DEFAULT_SETTLE_SECONDS = 1.0
DEFAULT_RESPONSE_TIMEOUT_SECONDS = 5.0
RECHECK_MEASUREMENT_COUNT = 2
REPORT_ROOT = Path(__file__).resolve().parent / "reports" / "four_wire_loop"
WORKBOOK_BUILDER = (
    Path(__file__).resolve().parent
    / "report_tools"
    / "four_wire_loop_workbook.mjs"
)
MEASURE_OK_PATTERN = re.compile(
    r"^OK MEASURE resistance=(?P<resistance>\d+(?:\.\d+)?) "
    r"raw=(?P<raw>\d+) range=(?P<range>\d+)$"
)

SendRequest = Callable[[str, str, str], str]
PublishEvent = Callable[[str, object], None]
ReportWriter = Callable[["FourWireSession", Path], Path]


@dataclass(frozen=True)
class FourWireGroup:
    """Identify two adjacent X ports that make one logical cable group."""

    bank: str
    start_x: int

    def __post_init__(self) -> None:
        if self.bank not in {"S1", "S2"}:
            raise ValueError(f"invalid matrix bank: {self.bank}")
        if self.start_x < 0 or self.start_x + WIRES_PER_GROUP > PORTS_PER_BANK:
            raise ValueError(f"invalid two-wire group start: {self.bank} X{self.start_x}")
        if self.start_x % WIRES_PER_GROUP:
            raise ValueError("two-wire group must start on an even X index")

    @property
    def ports(self) -> tuple[str, str]:
        """Return the current-lead then voltage-lead port labels."""
        return (
            f"{self.bank}_X{self.start_x}",
            f"{self.bank}_X{self.start_x + 1}",
        )

    @property
    def label(self) -> str:
        """Return a compact group label for progress and workbook axes."""
        return f"{self.bank}_X{self.start_x}-X{self.start_x + 1}"

    @property
    def group_number(self) -> int:
        """Return the one-based group number within this bank."""
        return self.start_x // WIRES_PER_GROUP + 1


@dataclass(frozen=True)
class DirectedLoopSpec:
    """Describe one ordered source-to-destination four-wire measurement."""

    source: FourWireGroup
    destination: FourWireGroup

    def __post_init__(self) -> None:
        if self.source == self.destination:
            raise ValueError("a directed test must use two different groups")

    @property
    def label(self) -> str:
        """Return the explicit direction shown in progress and raw records."""
        return f"{self.source.label} -> {self.destination.label}"

    @property
    def switch_commands(self) -> tuple[str, ...]:
        """Return safe Kelvin closure order for this direction.

        The source group's first X is I+, its second X is V+; the destination
        group's first X is I-, its second X is V-.  Voltage leads close before
        current leads, matching the hardware guidance in the project notes.
        """
        return (
            f"SWITCH {self.source.bank} {self.source.start_x + 1} Y2 ON",
            f"SWITCH {self.destination.bank} {self.destination.start_x + 1} Y1 ON",
            f"SWITCH {self.destination.bank} {self.destination.start_x} Y0 ON",
            f"SWITCH {self.source.bank} {self.source.start_x} Y3 ON",
        )


@dataclass(frozen=True)
class FourWireResult:
    """Store one directed result and every raw response used to obtain it."""

    source_group: str
    destination_group: str
    source_ports: tuple[str, str]
    destination_ports: tuple[str, str]
    resistance_ohm: float | None
    raw_value: int | None
    range_code: int | None
    status: str
    switch_responses: tuple[str, ...]
    first_measure_response: str
    additional_measure_responses: tuple[str, ...]
    measured_at: str
    measurement_count: int
    averaged_recheck: bool
    initial_classification: str
    recheck_values_ohm: tuple[float | None, ...] = ()

    @property
    def label(self) -> str:
        """Return the explicit source-to-destination result label."""
        return f"{self.source_group} -> {self.destination_group}"


@dataclass
class FourWireSession:
    """Hold complete or partial directed results for the workbook writer."""

    target_id: str
    groups: tuple[FourWireGroup, ...]
    started_at: datetime
    finished_at: datetime
    settle_seconds: float
    total_directions: int
    results: list[FourWireResult] = field(default_factory=list)
    cancelled: bool = False

    @property
    def successful_results(self) -> int:
        """Count directed results with a final numeric resistance."""
        return sum(result.resistance_ohm is not None for result in self.results)

    @property
    def rechecked_results(self) -> int:
        """Count directions whose first measurement was an error."""
        return sum(result.averaged_recheck for result in self.results)

    @property
    def error_results(self) -> int:
        """Count directions without a final numeric resistance."""
        return sum(result.resistance_ohm is None for result in self.results)


def build_group_plan() -> tuple[FourWireGroup, ...]:
    """Return 12 adjacent groups for S1 followed by 12 groups for S2."""
    return tuple(
        FourWireGroup(bank, start_x)
        for bank in ("S1", "S2")
        for start_x in range(0, PORTS_PER_BANK, WIRES_PER_GROUP)
    )


def build_directed_plan(
    groups: Sequence[FourWireGroup] | None = None,
) -> tuple[DirectedLoopSpec, ...]:
    """Build every ordered pair of distinct groups, including both directions."""
    ordered_groups = tuple(groups or build_group_plan())
    return tuple(
        DirectedLoopSpec(source, destination)
        for source in ordered_groups
        for destination in ordered_groups
        if source != destination
    )


def parse_measurement(payload: str) -> tuple[float, int, int] | None:
    """Decode one valid firmware MEASURE response."""
    match = MEASURE_OK_PATTERN.fullmatch(payload.strip())
    if match is None:
        return None
    return (
        float(match.group("resistance")),
        int(match.group("raw")),
        int(match.group("range")),
    )


def build_result(
    spec: DirectedLoopSpec,
    switch_responses: Sequence[str],
    first_response: str,
    additional_responses: Sequence[str] = (),
) -> FourWireResult:
    """Classify one direction and apply the two-follow-up averaging rule."""
    first = parse_measurement(first_response)
    rechecks = tuple(parse_measurement(response) for response in additional_responses)
    measured_at = datetime.now().astimezone().isoformat(timespec="seconds")

    common = {
        "source_group": spec.source.label,
        "destination_group": spec.destination.label,
        "source_ports": spec.source.ports,
        "destination_ports": spec.destination.ports,
        "switch_responses": tuple(switch_responses),
        "first_measure_response": first_response,
        "additional_measure_responses": tuple(additional_responses),
        "measured_at": measured_at,
    }
    if first is not None:
        return FourWireResult(
            **common,
            resistance_ohm=first[0],
            raw_value=first[1],
            range_code=first[2],
            status="成功",
            measurement_count=1,
            averaged_recheck=False,
            initial_classification="normal",
        )

    if len(rechecks) == RECHECK_MEASUREMENT_COUNT and all(
        item is not None for item in rechecks
    ):
        valid_values = tuple(item[0] for item in rechecks if item is not None)
        return FourWireResult(
            **common,
            resistance_ohm=sum(valid_values) / RECHECK_MEASUREMENT_COUNT,
            raw_value=None,
            range_code=None,
            status="初测错误，两次复测平均",
            measurement_count=1 + len(additional_responses),
            averaged_recheck=True,
            initial_classification="error",
            recheck_values_ohm=valid_values,
        )

    return FourWireResult(
        **common,
        resistance_ohm=None,
        raw_value=None,
        range_code=None,
        status="复测错误",
        measurement_count=1 + len(additional_responses),
        averaged_recheck=True,
        initial_classification="error",
        recheck_values_ohm=tuple(
            item[0] if item is not None else None for item in rechecks
        ),
    )


def connection_error_result(
    spec: DirectedLoopSpec,
    switch_responses: Sequence[str],
    error_response: str,
) -> FourWireResult:
    """Record a failed SWITCH without fabricating a resistance value."""
    return FourWireResult(
        source_group=spec.source.label,
        destination_group=spec.destination.label,
        source_ports=spec.source.ports,
        destination_ports=spec.destination.ports,
        resistance_ohm=None,
        raw_value=None,
        range_code=None,
        status="连接错误",
        switch_responses=tuple(switch_responses),
        first_measure_response=error_response,
        additional_measure_responses=(),
        measured_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        measurement_count=0,
        averaged_recheck=False,
        initial_classification="error",
    )


class FourWireLoopTestController:
    """Run directed tests while exclusively owning the matrix path."""

    def __init__(
        self,
        send_request: SendRequest,
        publish_event: PublishEvent,
        *,
        report_root: Path = REPORT_ROOT,
        settle_seconds: float = DEFAULT_SETTLE_SECONDS,
        response_timeout_seconds: float = DEFAULT_RESPONSE_TIMEOUT_SECONDS,
        groups: Sequence[FourWireGroup] | None = None,
        specs: Sequence[DirectedLoopSpec] | None = None,
        report_writer: ReportWriter | None = None,
    ) -> None:
        if settle_seconds < 0.0:
            raise ValueError("settle_seconds must not be negative")
        self._send_request = send_request
        self._publish_event = publish_event
        self._groups = tuple(groups or build_group_plan())
        self._specs = tuple(specs or build_directed_plan(self._groups))
        self._report_root = Path(report_root)
        self._settle_seconds = settle_seconds
        self._response_timeout_seconds = response_timeout_seconds
        self._report_writer = report_writer or write_report_workbook
        self._uses_default_report_writer = report_writer is None
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[str, queue.Queue[str]]] = {}
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()

    @property
    def running(self) -> bool:
        """Return whether the directed test worker is currently active."""
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self, target_id: str) -> bool:
        """Start one directed run and reject overlapping runs."""
        if self._uses_default_report_writer:
            ensure_workbook_runtime()
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._cancel.clear()
            thread = threading.Thread(
                target=self._run,
                args=(target_id,),
                daemon=True,
                name="cable-four-wire-loop-test",
            )
            self._thread = thread
            thread.start()
        return True

    def cancel(self) -> None:
        """Request cooperative cancellation; the worker still performs RESET."""
        self._cancel.set()

    def feed_result(self, target_id: str, request_id: str, payload: str) -> bool:
        """Deliver one correlated ESP RESULT to the waiting worker."""
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
        request_prefix = f"FWL-{started_at.strftime('%H%M%S')}"
        results: list[FourWireResult] = []
        cancelled = False
        terminal_error: str | None = None
        terminal_event: tuple[str, object] | None = None

        self._publish_event(
            "four_wire_started",
            f"目标 {target_id}，24 个二线组，共 {len(self._specs)} 个有方向测试；"
            f"首测错误追加 {RECHECK_MEASUREMENT_COUNT} 次复测。",
        )

        try:
            initial_reset = self._request_one(
                target_id, f"{request_prefix}-RESET0", "RESET"
            )
            if initial_reset != "OK RESET":
                raise RuntimeError(f"开始前矩阵复位失败：{initial_reset}")

            for index, spec in enumerate(self._specs, start=1):
                if self._cancel.is_set():
                    cancelled = True
                    break
                self._publish_event(
                    "four_wire_progress",
                    {
                        "index": index,
                        "total": len(self._specs),
                        "pair": spec.label,
                        "source": spec.source.label,
                        "destination": spec.destination.label,
                        "phase": "connecting",
                    },
                )
                request_base = f"{request_prefix}-{index:03d}"
                switch_responses: list[str] = []
                switch_failed = False
                for switch_index, command in enumerate(spec.switch_commands):
                    response = self._request_one(
                        target_id,
                        f"{request_base}-S{switch_index}",
                        command,
                    )
                    switch_responses.append(response)
                    if response != f"OK {command}":
                        results.append(
                            connection_error_result(spec, switch_responses, response)
                        )
                        switch_failed = True
                        break
                    if self._cancel.is_set():
                        cancelled = True
                        break
                if switch_failed or cancelled:
                    if cancelled:
                        break
                    self._reset_after_direction(target_id, request_base)
                    continue

                if self._cancel.wait(self._settle_seconds):
                    cancelled = True
                    break
                self._publish_event(
                    "four_wire_progress",
                    {
                        "index": index,
                        "total": len(self._specs),
                        "pair": spec.label,
                        "source": spec.source.label,
                        "destination": spec.destination.label,
                        "phase": "measuring",
                    },
                )
                first_response = self._request_one(
                    target_id, f"{request_base}-M0", "MEASURE"
                )
                additional_responses: list[str] = []
                if parse_measurement(first_response) is None:
                    for retry_index in range(1, RECHECK_MEASUREMENT_COUNT + 1):
                        if self._cancel.is_set():
                            cancelled = True
                            break
                        additional_responses.append(
                            self._request_one(
                                target_id,
                                f"{request_base}-R{retry_index}",
                                "MEASURE",
                            )
                        )
                if cancelled:
                    break

                results.append(
                    build_result(
                        spec,
                        switch_responses,
                        first_response,
                        additional_responses,
                    )
                )
                self._reset_after_direction(target_id, request_base)

        except Exception as error:
            terminal_error = f"四线回路测试失败：{error}"
        finally:
            self._reset_after_direction(target_id, f"{request_prefix}-RESET-END")

            if terminal_error is None:
                session = FourWireSession(
                    target_id=target_id,
                    groups=self._groups,
                    started_at=started_at,
                    finished_at=datetime.now().astimezone(),
                    settle_seconds=self._settle_seconds,
                    total_directions=len(self._specs),
                    results=results,
                    cancelled=cancelled,
                )
                try:
                    workbook_path = self._report_writer(session, self._report_root)
                except Exception as error:
                    terminal_error = f"四线回路 Excel 生成失败：{error}"
                else:
                    event_type = "four_wire_stopped" if cancelled else "four_wire_complete"
                    terminal_event = (
                        event_type,
                        {
                            "target_id": target_id,
                            "completed": len(results),
                            "successful": session.successful_results,
                            "rechecked": session.rechecked_results,
                            "errors": session.error_results,
                            "total": len(self._specs),
                            "groups": len(self._groups),
                            "xlsx": str(workbook_path),
                        },
                    )

            with self._lock:
                self._pending.clear()
                self._thread = None
            if terminal_error is not None:
                self._publish_event("four_wire_error", terminal_error)
            elif terminal_event is not None:
                self._publish_event(*terminal_event)

    def _reset_after_direction(self, target_id: str, request_prefix: str) -> None:
        """Reset the matrix after one direction and publish failures as warnings."""
        try:
            reset_response = self._request_one(
                target_id, f"{request_prefix}-RESET", "RESET"
            )
            if reset_response != "OK RESET":
                self._publish_event(
                    "four_wire_reset_warning",
                    f"四线回路方向结束复位失败：{reset_response}",
                )
        except Exception as error:
            self._publish_event(
                "four_wire_reset_warning",
                f"四线回路方向结束复位异常：{error}",
            )

    def _request_one(self, target_id: str, request_id: str, command: str) -> str:
        """Send one routed command and wait for its matching RESULT payload."""
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
                return f"ERR FOUR_WIRE RESULT_TIMEOUT request_id={request_id}"
        finally:
            with self._lock:
                self._pending.pop(request_id, None)


def ensure_workbook_runtime() -> tuple[Path, Path]:
    """Locate the bundled Node/artifact-tool runtime and local workbook builder."""
    dependency_root = (
        Path.home()
        / ".cache"
        / "codex-runtimes"
        / "codex-primary-runtime"
        / "dependencies"
    )
    node = dependency_root / "node" / "bin" / "node.exe"
    node_modules = dependency_root / "node" / "node_modules"
    if not node.exists() or not (node_modules / "@oai" / "artifact-tool").exists():
        raise RuntimeError("未找到四线回路测试生成 XLSX 所需的表格运行环境")
    if not WORKBOOK_BUILDER.exists():
        raise RuntimeError(f"表格生成脚本不存在：{WORKBOOK_BUILDER}")

    local_modules = WORKBOOK_BUILDER.parent / "node_modules"
    if not local_modules.exists():
        completed = subprocess.run(
            [
                "cmd.exe",
                "/d",
                "/c",
                "mklink",
                "/J",
                str(local_modules),
                str(node_modules),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0 or not local_modules.exists():
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(f"无法准备表格运行环境：{detail}")
    return node, WORKBOOK_BUILDER


def _next_workbook_path(report_root: Path, target_id: str, now: datetime) -> Path:
    """Allocate a monthly workbook path without overwriting prior runs."""
    month_directory = Path(report_root) / now.strftime("%Y-%m")
    month_directory.mkdir(parents=True, exist_ok=True)
    date_text = now.strftime("%Y%m%d")
    for sequence in range(1, 1000):
        path = month_directory / (
            f"四线回路电阻_S1S2_{target_id}_{date_text}_{sequence:03d}.xlsx"
        )
        if not path.exists():
            return path
    raise RuntimeError("当天的四线回路报告编号已用尽")


def _session_payload(session: FourWireSession) -> dict[str, object]:
    """Convert session data into the JSON contract consumed by the XLSX builder."""
    return {
        "target_id": session.target_id,
        "started_at": session.started_at.isoformat(),
        "finished_at": session.finished_at.isoformat(),
        "settle_seconds": session.settle_seconds,
        "total_groups": len(session.groups),
        "total_directions": session.total_directions,
        "completed_directions": len(session.results),
        "cancelled": session.cancelled,
        "groups": [
            {"label": group.label, "ports": list(group.ports)}
            for group in session.groups
        ],
        "results": [asdict(result) for result in session.results],
    }


def write_report_workbook(session: FourWireSession, report_root: Path) -> Path:
    """Build the directed matrix and raw-record XLSX using the bundled Node tool."""
    node, builder = ensure_workbook_runtime()
    output_path = _next_workbook_path(
        Path(report_root), session.target_id, session.finished_at
    )
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".json",
            prefix="four_wire_",
            dir=output_path.parent,
            delete=False,
        ) as temporary:
            json.dump(_session_payload(session), temporary, ensure_ascii=False, indent=2)
            temporary_path = Path(temporary.name)
        completed = subprocess.run(
            [str(node), str(builder), str(temporary_path), str(output_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0 or not output_path.exists():
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(detail or "四线回路表格生成器未输出 XLSX")
        return output_path
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
