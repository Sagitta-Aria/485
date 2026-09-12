"""Measure both directions of every 48-port pair and export an XLSX.

The GUI owns TCP routing.  This module owns the long-running pair schedule,
waits one second after each successful CONNECT, records the next raw MEASURE
response without calibration or statistical processing.  Each unordered pair
is scheduled twice, once with each port as the positive and negative end, so
the workbook contains independently measured A->B and B->A cells.  If the
first measurement is above 20 ohm, it keeps the same CONNECT path and takes
three additional MEASURE readings, averaging those three values.  It always
resets the matrix before publishing a terminal event.
"""

from __future__ import annotations

from cable_tester.paths import PROJECT_ROOT, PAIRWISE_REPORTS

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
DEFAULT_SETTLE_SECONDS = 1.0
DEFAULT_RESPONSE_TIMEOUT_SECONDS = 5.0
RECHECK_THRESHOLD_OHM = 20.0
RECHECK_MEASUREMENT_COUNT = 3
REPORT_ROOT = PAIRWISE_REPORTS
WORKBOOK_BUILDER = (
    PROJECT_ROOT
    / "report_tools"
    / "pairwise_resistance_workbook.mjs"
)
MEASURE_OK_PATTERN = re.compile(
    r"^OK MEASURE resistance=(?P<resistance>\d+(?:\.\d+)?) "
    r"raw=(?P<raw>\d+) range=(?P<range>\d+)$"
)

SendRequest = Callable[[str, str, str], str]
PublishEvent = Callable[[str, object], None]
ReportWriter = Callable[["PairwiseSession", Path], Path]


@dataclass(frozen=True)
class MatrixPort:
    """Identify one of the existing S1/S2 X0..X23 connector ports."""

    bank: str
    x: int

    def __post_init__(self) -> None:
        if self.bank not in {"S1", "S2"} or not 0 <= self.x < PORTS_PER_BANK:
            raise ValueError(f"invalid matrix port: {self.bank} X{self.x}")

    @property
    def label(self) -> str:
        """Return the stable label used by GUI progress and workbook axes."""
        return f"{self.bank}_X{self.x}"


@dataclass(frozen=True)
class PairSpec:
    """Describe one directed port pair and its firmware CONNECT command."""

    first: MatrixPort
    second: MatrixPort

    @property
    def command(self) -> str:
        """Return a four-wire CONNECT command for this pair."""
        return (
            f"CONNECT {self.first.bank} {self.first.x} "
            f"{self.second.bank} {self.second.x}"
        )

    @property
    def label(self) -> str:
        """Return a compact pair label for progress messages."""
        return f"{self.first.label} - {self.second.label}"


@dataclass(frozen=True)
class PairResult:
    """Store one directed resistance result and every raw response used to obtain it."""

    first_port: str
    second_port: str
    resistance_ohm: float | None
    raw_value: int | None
    range_code: int | None
    status: str
    connect_response: str
    measure_response: str
    measured_at: str
    additional_measure_responses: tuple[str, ...] = ()
    measurement_count: int = 1
    averaged_recheck: bool = False
    initial_classification: str = "normal"


@dataclass
class PairwiseSession:
    """Hold the complete or partial raw dataset passed to the workbook writer."""

    target_id: str
    started_at: datetime
    finished_at: datetime
    settle_seconds: float
    ports: tuple[MatrixPort, ...]
    total_pairs: int
    results: list[PairResult] = field(default_factory=list)
    cancelled: bool = False

    @property
    def successful_pairs(self) -> int:
        """Count results containing one successfully decoded resistance value."""
        return sum(result.resistance_ohm is not None for result in self.results)


def build_port_order() -> tuple[MatrixPort, ...]:
    """Return S1_X0..S1_X23 followed by S2_X0..S2_X23."""
    return tuple(
        MatrixPort(bank, x)
        for bank in ("S1", "S2")
        for x in range(PORTS_PER_BANK)
    )


def build_pair_plan(
    ports: Sequence[MatrixPort] | None = None,
) -> tuple[PairSpec, ...]:
    """Build both directions of every unordered pair in adjacent order."""
    ordered_ports = tuple(ports or build_port_order())
    plan: list[PairSpec] = []
    for first_index in range(len(ordered_ports)):
        for second_index in range(first_index + 1, len(ordered_ports)):
            first = ordered_ports[first_index]
            second = ordered_ports[second_index]
            plan.extend((PairSpec(first, second), PairSpec(second, first)))
    return tuple(plan)


def decode_measurement(
    spec: PairSpec, connect_response: str, measure_response: str
) -> PairResult:
    """Decode only OK MEASURE; preserve every other response as an error row."""
    match = MEASURE_OK_PATTERN.fullmatch(measure_response.strip())
    measured_at = datetime.now().astimezone().isoformat(timespec="seconds")
    if match is None:
        return PairResult(
            spec.first.label,
            spec.second.label,
            None,
            None,
            None,
            "测量错误",
            connect_response,
            measure_response,
            measured_at,
            initial_classification="error",
        )
    resistance = float(match.group("resistance"))
    return PairResult(
        spec.first.label,
        spec.second.label,
        resistance,
        int(match.group("raw")),
        int(match.group("range")),
        "成功",
        connect_response,
        measure_response,
        measured_at,
        initial_classification=(
            "over20" if resistance > RECHECK_THRESHOLD_OHM else "normal"
        ),
    )


def parse_measurement(payload: str) -> tuple[float, int, int] | None:
    """Decode one firmware MEASURE response into resistance, raw value, range."""
    match = MEASURE_OK_PATTERN.fullmatch(payload.strip())
    if match is None:
        return None
    return (
        float(match.group("resistance")),
        int(match.group("raw")),
        int(match.group("range")),
    )


def decode_rechecked_measurement(
    spec: PairSpec,
    connect_response: str,
    first_response: str,
    additional_responses: Sequence[str],
) -> PairResult:
    """Average three successful rechecks while preserving all raw responses."""
    first = parse_measurement(first_response)
    rechecks = [parse_measurement(response) for response in additional_responses]
    measured_at = datetime.now().astimezone().isoformat(timespec="seconds")
    if len(rechecks) != RECHECK_MEASUREMENT_COUNT or any(
        item is None for item in rechecks
    ):
        initial = decode_measurement(spec, connect_response, first_response)
        return PairResult(
            initial.first_port,
            initial.second_port,
            None,
            initial.raw_value,
            initial.range_code,
            "复测错误",
            connect_response,
            first_response,
            measured_at,
            tuple(additional_responses),
            1 + len(additional_responses),
            True,
            initial.initial_classification,
        )
    resistance = sum(item[0] for item in rechecks if item is not None) / RECHECK_MEASUREMENT_COUNT
    initial_classification = (
        "error"
        if first is None
        else "over20" if first[0] > RECHECK_THRESHOLD_OHM else "normal"
    )
    status = (
        "初测错误，3次复测平均"
        if initial_classification == "error"
        else "初测超20Ω，3次复测平均"
        if initial_classification == "over20"
        else "成功（3次复测平均）"
    )
    return PairResult(
        spec.first.label,
        spec.second.label,
        resistance,
        first[1] if first is not None else None,
        first[2] if first is not None else None,
        status,
        connect_response,
        first_response,
        measured_at,
        tuple(additional_responses),
        1 + len(additional_responses),
        True,
        initial_classification,
    )


def connection_error_result(spec: PairSpec, payload: str) -> PairResult:
    """Record a failed CONNECT without inventing a measurement value."""
    return PairResult(
        spec.first.label,
        spec.second.label,
        None,
        None,
        None,
        "连接错误",
        payload,
        "",
        datetime.now().astimezone().isoformat(timespec="seconds"),
        initial_classification="error",
    )


class PairwiseResistanceController:
    """Run all pair measurements while correlating asynchronous RESULT frames."""

    def __init__(
        self,
        send_request: SendRequest,
        publish_event: PublishEvent,
        *,
        report_root: Path = REPORT_ROOT,
        settle_seconds: float = DEFAULT_SETTLE_SECONDS,
        response_timeout_seconds: float = DEFAULT_RESPONSE_TIMEOUT_SECONDS,
        pairs: Sequence[PairSpec] | None = None,
        report_writer: ReportWriter | None = None,
    ) -> None:
        if settle_seconds < 0.0:
            raise ValueError("settle_seconds must not be negative")
        self._send_request = send_request
        self._publish_event = publish_event
        self._report_root = Path(report_root)
        self._settle_seconds = settle_seconds
        self._response_timeout_seconds = response_timeout_seconds
        self._ports = build_port_order()
        self._pairs = tuple(pairs or build_pair_plan(self._ports))
        self._report_writer = report_writer or write_report_workbook
        self._uses_default_report_writer = report_writer is None
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[str, queue.Queue[str]]] = {}
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()

    @property
    def running(self) -> bool:
        """Return whether this controller currently owns the measurement path."""
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self, target_id: str) -> bool:
        """Start one pairwise test and reject a second overlapping run."""
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
                name="cable-pairwise-resistance-test",
            )
            self._thread = thread
            thread.start()
        return True

    def cancel(self) -> None:
        """Request a cooperative stop; completed data is still exported."""
        self._cancel.set()

    def feed_result(self, target_id: str, request_id: str, payload: str) -> bool:
        """Deliver a correlated ESP RESULT to the waiting test worker."""
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
        request_prefix = f"PRT-{started_at.strftime('%H%M%S')}"
        results: list[PairResult] = []
        cancelled = False
        terminal_error: str | None = None
        terminal_event: tuple[str, object] | None = None
        self._publish_event(
            "pairwise_started",
            f"目标 {target_id}，共 {len(self._pairs)} 个方向；每个端口对正反各测一次，"
            f"每次闭合后稳定 {self._settle_seconds:.1f} 秒再读取。",
        )

        try:
            for index, spec in enumerate(self._pairs, start=1):
                if self._cancel.is_set():
                    cancelled = True
                    break
                self._publish_event(
                    "pairwise_progress",
                    {
                        "index": index,
                        "total": len(self._pairs),
                        "pair": spec.label,
                    },
                )
                request_base = f"{request_prefix}-{index:04d}"
                connect_response = self._request_one(
                    target_id, f"{request_base}-C", spec.command
                )
                if connect_response != f"OK {spec.command}":
                    results.append(connection_error_result(spec, connect_response))
                    continue
                if self._cancel.wait(self._settle_seconds):
                    cancelled = True
                    break
                measure_response = self._request_one(
                    target_id, f"{request_base}-M", "MEASURE"
                )
                initial_result = decode_measurement(
                    spec, connect_response, measure_response
                )
                should_recheck = (
                    initial_result.resistance_ohm is None
                    or initial_result.resistance_ohm > RECHECK_THRESHOLD_OHM
                )
                if should_recheck:
                    additional_responses: list[str] = []
                    for repeat_index in range(1, RECHECK_MEASUREMENT_COUNT + 1):
                        if self._cancel.is_set():
                            cancelled = True
                            break
                        additional_responses.append(
                            self._request_one(
                                target_id,
                                f"{request_base}-R{repeat_index}",
                                "MEASURE",
                            )
                        )
                    if cancelled:
                        results.append(
                            PairResult(
                                initial_result.first_port,
                                initial_result.second_port,
                                None,
                                initial_result.raw_value,
                                initial_result.range_code,
                                "复测取消",
                                connect_response,
                                measure_response,
                                datetime.now()
                                .astimezone()
                                .isoformat(timespec="seconds"),
                                tuple(additional_responses),
                                1 + len(additional_responses),
                                True,
                                initial_result.initial_classification,
                            )
                        )
                        break
                    results.append(
                        decode_rechecked_measurement(
                            spec,
                            connect_response,
                            measure_response,
                            additional_responses,
                        )
                    )
                else:
                    results.append(initial_result)
        except Exception as error:
            terminal_error = f"全引脚阻值测试失败：{error}"
        finally:
            try:
                reset_response = self._request_one(
                    target_id, f"{request_prefix}-RESET", "RESET"
                )
                if reset_response != "OK RESET":
                    self._publish_event(
                        "pairwise_reset_warning",
                        f"全引脚阻值测试结束复位失败：{reset_response}",
                    )
            except Exception as error:
                self._publish_event(
                    "pairwise_reset_warning",
                    f"全引脚阻值测试结束复位异常：{error}",
                )

            if terminal_error is None:
                session = PairwiseSession(
                    target_id=target_id,
                    started_at=started_at,
                    finished_at=datetime.now().astimezone(),
                    settle_seconds=self._settle_seconds,
                    ports=self._ports,
                    total_pairs=len(self._pairs),
                    results=results,
                    cancelled=cancelled,
                )
                try:
                    workbook_path = self._report_writer(session, self._report_root)
                except Exception as error:
                    terminal_error = f"全引脚阻值表生成失败：{error}"
                else:
                    event_type = "pairwise_stopped" if cancelled else "pairwise_complete"
                    terminal_event = (
                        event_type,
                        {
                            "target_id": target_id,
                            "completed": len(results),
                            "successful": session.successful_pairs,
                            "total": len(self._pairs),
                            "xlsx": str(workbook_path),
                        },
                    )

            with self._lock:
                self._pending.clear()
                self._thread = None
            if terminal_error is not None:
                self._publish_event("pairwise_error", terminal_error)
            elif terminal_event is not None:
                self._publish_event(*terminal_event)

    def _request_one(self, target_id: str, request_id: str, command: str) -> str:
        """Send one routed command and wait for its matching RESULT payload."""
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
                return f"ERR PAIRWISE RESULT_TIMEOUT request_id={request_id}"
        finally:
            with self._lock:
                self._pending.pop(request_id, None)


def ensure_workbook_runtime() -> tuple[Path, Path]:
    """Locate the bundled Node/artifact-tool runtime and ensure local resolution."""
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
        raise RuntimeError("未找到上位机生成 XLSX 所需的表格运行环境")
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


def _next_workbook_path(
    report_root: Path, target_id: str, now: datetime
) -> Path:
    """Allocate one monthly XLSX path without overwriting an earlier run."""
    month_directory = Path(report_root) / now.strftime("%Y-%m")
    month_directory.mkdir(parents=True, exist_ok=True)
    date_text = now.strftime("%Y%m%d")
    for sequence in range(1, 1000):
        path = month_directory / (
            f"全引脚两两阻值_{target_id}_{date_text}_{sequence:03d}.xlsx"
        )
        if not path.exists():
            return path
    raise RuntimeError("当天的全引脚阻值报告编号已用尽")


def _session_payload(session: PairwiseSession) -> dict[str, object]:
    """Convert a session into the JSON contract consumed by the XLSX builder."""
    return {
        "target_id": session.target_id,
        "started_at": session.started_at.isoformat(),
        "finished_at": session.finished_at.isoformat(),
        "settle_seconds": session.settle_seconds,
        "total_pairs": session.total_pairs,
        "completed_pairs": len(session.results),
        "successful_pairs": session.successful_pairs,
        "cancelled": session.cancelled,
        "ports": [port.label for port in session.ports],
        "results": [asdict(result) for result in session.results],
    }


def write_report_workbook(session: PairwiseSession, report_root: Path) -> Path:
    """Build the matrix and raw-record XLSX using the bundled artifact tool."""
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
            prefix="pairwise_",
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
            raise RuntimeError(detail or "表格生成器未输出 XLSX")
        return output_path
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
