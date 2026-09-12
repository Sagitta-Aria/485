"""GUI background workflow for auxiliary-loop resistance-error testing.

The GUI owns TCP routing. This module compares one zero-ohm Kelvin target with
and without each S1_Xn/S2_Xn auxiliary loop, correlates RESULT frames, ranks
the candidates, and writes JSON/CSV reports.
"""

from __future__ import annotations

from cable_tester.paths import AUXILIARY_REPORTS

import csv
import json
import math
import queue
import random
import re
import statistics
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol, Sequence


DEFAULT_TARGET_RESISTANCE_OHM = 0.0
DEFAULT_SCREEN_SAMPLES = 5
DEFAULT_VERIFY_SAMPLES = 10
DEFAULT_FINALIST_COUNT = 3
DEFAULT_SETTLE_SECONDS = 2.0
DEFAULT_SAMPLE_INTERVAL_SECONDS = 0.2
DEFAULT_RESPONSE_TIMEOUT_SECONDS = 5.0
MIN_SUCCESS_RATIO = 0.8
REPORT_ROOT = AUXILIARY_REPORTS
DEFAULT_CANDIDATES = tuple(range(1, 24))

MEASURE_OK_PATTERN = re.compile(
    r"^OK MEASURE resistance=(?P<resistance>\d+(?:\.\d+)?) "
    r"raw=(?P<raw>\d+) range=(?P<range>\d+)$"
)


class CommandTransport(Protocol):
    """Synchronous command interface used by the experiment and test doubles."""

    def request(self, command: str) -> str:
        """Send one hardware command and return its correlated RESULT payload."""


@dataclass(frozen=True)
class TargetPair:
    """Identify the positive and negative ports of the measured Kelvin path."""

    positive_bank: str = "S1"
    positive_x: int = 0
    negative_bank: str = "S2"
    negative_x: int = 0

    def __post_init__(self) -> None:
        for bank in (self.positive_bank, self.negative_bank):
            if bank not in {"S1", "S2"}:
                raise ValueError(f"invalid bank: {bank}")
        for x in (self.positive_x, self.negative_x):
            if not 0 <= x <= 23:
                raise ValueError(f"X channel outside 0..23: {x}")
        if (
            self.positive_bank == self.negative_bank
            and self.positive_x == self.negative_x
        ):
            raise ValueError("positive and negative ports must be different")

    @property
    def command(self) -> str:
        """Return the firmware CONNECT command for this pair."""
        return (
            f"CONNECT {self.positive_bank} {self.positive_x} "
            f"{self.negative_bank} {self.negative_x}"
        )

    @property
    def label(self) -> str:
        """Return a compact human-readable target label."""
        return (
            f"{self.positive_bank}_X{self.positive_x}-"
            f"{self.negative_bank}_X{self.negative_x}"
        )

    @property
    def occupied_x(self) -> set[int]:
        """Return X indices already used by either side of the target path."""
        return {self.positive_x, self.negative_x}


@dataclass(frozen=True)
class MeasurementReading:
    """Hold one successfully decoded raw XD31H measurement."""

    resistance_ohm: float
    raw_value: int
    range_code: int
    payload: str
    received_at: str


@dataclass
class ConditionResult:
    """Store all readings and errors for one baseline or assisted condition."""

    phase: str
    candidate_x: int
    condition: str
    requested_samples: int
    readings: list[MeasurementReading] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self, target_ohm: float) -> dict[str, float | int | None]:
        """Return robust descriptive statistics relative to the known target."""
        values = [reading.resistance_ohm for reading in self.readings]
        return summarize_values(
            values,
            target_ohm,
            requested_samples=self.requested_samples,
            error_count=len(self.errors),
        )

    def to_dict(self, target_ohm: float) -> dict[str, object]:
        """Return a JSON-ready representation including readings and statistics."""
        return {
            "phase": self.phase,
            "candidate_x": self.candidate_x,
            "condition": self.condition,
            "requested_samples": self.requested_samples,
            "summary": self.summary(target_ohm),
            "readings": [asdict(reading) for reading in self.readings],
            "errors": list(self.errors),
        }


@dataclass
class CandidateComparison:
    """Pair a nearby baseline run with one assisted-loop run."""

    phase: str
    candidate_x: int
    baseline: ConditionResult
    assisted: ConditionResult

    def to_dict(self, target_ohm: float) -> dict[str, object]:
        """Return measurements plus the paired improvement for report output."""
        baseline_summary = self.baseline.summary(target_ohm)
        assisted_summary = self.assisted.summary(target_ohm)
        baseline_error = baseline_summary["absolute_median_error_ohm"]
        assisted_error = assisted_summary["absolute_median_error_ohm"]
        improvement: float | None = None
        improvement_percent: float | None = None
        if isinstance(baseline_error, float) and isinstance(assisted_error, float):
            improvement = baseline_error - assisted_error
            if baseline_error > 0.0:
                improvement_percent = improvement / baseline_error * 100.0
        return {
            "phase": self.phase,
            "candidate_x": self.candidate_x,
            "auxiliary_loop": f"S1_X{self.candidate_x}-S2_X{self.candidate_x}",
            "baseline": self.baseline.to_dict(target_ohm),
            "assisted": self.assisted.to_dict(target_ohm),
            "paired_improvement_ohm": improvement,
            "paired_improvement_percent": improvement_percent,
        }


def parse_measurement_payload(payload: str) -> MeasurementReading | None:
    """Decode a successful firmware MEASURE payload without hiding failures."""
    match = MEASURE_OK_PATTERN.fullmatch(payload.strip())
    if match is None:
        return None
    return MeasurementReading(
        resistance_ohm=float(match.group("resistance")),
        raw_value=int(match.group("raw")),
        range_code=int(match.group("range")),
        payload=payload,
        received_at=datetime.now().astimezone().isoformat(),
    )


def summarize_values(
    values: Sequence[float],
    target_ohm: float,
    *,
    requested_samples: int | None = None,
    error_count: int = 0,
) -> dict[str, float | int | None]:
    """Calculate median error and robust spread for one measurement condition."""
    requested = len(values) if requested_samples is None else requested_samples
    if not values:
        return {
            "requested_samples": requested,
            "successful_samples": 0,
            "failed_samples": error_count,
            "median_ohm": None,
            "mean_ohm": None,
            "stdev_ohm": None,
            "mad_ohm": None,
            "minimum_ohm": None,
            "maximum_ohm": None,
            "span_ohm": None,
            "signed_median_error_ohm": None,
            "absolute_median_error_ohm": None,
        }

    median = float(statistics.median(values))
    mean = float(statistics.fmean(values))
    stdev = float(statistics.stdev(values)) if len(values) > 1 else 0.0
    mad = float(statistics.median(abs(value - median) for value in values))
    minimum = float(min(values))
    maximum = float(max(values))
    signed_error = median - target_ohm
    return {
        "requested_samples": requested,
        "successful_samples": len(values),
        "failed_samples": error_count,
        "median_ohm": median,
        "mean_ohm": mean,
        "stdev_ohm": stdev,
        "mad_ohm": mad,
        "minimum_ohm": minimum,
        "maximum_ohm": maximum,
        "span_ohm": maximum - minimum,
        "signed_median_error_ohm": signed_error,
        "absolute_median_error_ohm": abs(signed_error),
    }


def auxiliary_loop_commands(candidate_x: int) -> tuple[str, ...]:
    """Build the four SWITCH commands for S1_Xn positive and S2_Xn negative."""
    if not 0 <= candidate_x <= 23:
        raise ValueError(f"candidate X outside 0..23: {candidate_x}")
    return (
        f"SWITCH S1 {candidate_x} Y2 ON",
        f"SWITCH S1 {candidate_x} Y3 ON",
        f"SWITCH S2 {candidate_x} Y1 ON",
        f"SWITCH S2 {candidate_x} Y0 ON",
    )


def parse_candidates(text: str) -> list[int]:
    """Parse comma-separated X values and inclusive ranges such as 1-5,8,10."""
    candidates: list[int] = []
    for raw_part in text.split(","):
        part = raw_part.strip()
        if not part:
            raise ValueError("candidate list contains an empty item")
        if "-" in part:
            bounds = part.split("-")
            if len(bounds) != 2:
                raise ValueError(f"invalid candidate range: {part}")
            start, end = (int(value) for value in bounds)
            if start > end:
                raise ValueError(f"candidate range is reversed: {part}")
            values = range(start, end + 1)
        else:
            values = (int(part),)
        for value in values:
            if not 0 <= value <= 23:
                raise ValueError(f"candidate X outside 0..23: {value}")
            if value not in candidates:
                candidates.append(value)
    if not candidates:
        raise ValueError("at least one candidate X is required")
    return candidates


def _expect_payload(payload: str, expected: str, operation: str) -> None:
    """Raise immediately when a matrix configuration command was not accepted."""
    if payload != expected:
        raise RuntimeError(f"{operation} failed: {payload}")


def run_condition(
    transport: CommandTransport,
    target_pair: TargetPair,
    *,
    phase: str,
    candidate_x: int,
    assisted: bool,
    sample_count: int,
    settle_seconds: float,
    sample_interval_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
) -> ConditionResult:
    """Configure one condition, sample XD31H, and always return to RESET state."""
    condition = "assisted" if assisted else "baseline"
    result = ConditionResult(phase, candidate_x, condition, sample_count)
    body_error: BaseException | None = None
    try:
        connect_payload = transport.request(target_pair.command)
        _expect_payload(connect_payload, f"OK {target_pair.command}", "CONNECT")

        if assisted:
            for command in auxiliary_loop_commands(candidate_x):
                payload = transport.request(command)
                _expect_payload(payload, f"OK {command}", command)

        sleep(settle_seconds)
        for sample_index in range(sample_count):
            try:
                payload = transport.request("MEASURE")
                reading = parse_measurement_payload(payload)
                if reading is None:
                    result.errors.append(payload)
                else:
                    result.readings.append(reading)
            except InterruptedError:
                raise
            except Exception as error:
                result.errors.append(f"MEASURE exception: {error}")
            if sample_index + 1 < sample_count:
                sleep(sample_interval_seconds)
    except BaseException as error:
        body_error = error
        raise
    finally:
        try:
            reset_payload = transport.request("RESET")
            _expect_payload(reset_payload, "OK RESET", "RESET")
        except BaseException as reset_error:
            if body_error is not None:
                raise RuntimeError(
                    f"condition failed ({body_error}); RESET also failed ({reset_error})"
                ) from reset_error
            raise
    return result


def run_comparison(
    transport: CommandTransport,
    target_pair: TargetPair,
    *,
    phase: str,
    candidate_x: int,
    sample_count: int,
    settle_seconds: float,
    sample_interval_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
) -> CandidateComparison:
    """Run a nearby baseline immediately before one assisted candidate."""
    baseline = run_condition(
        transport,
        target_pair,
        phase=phase,
        candidate_x=candidate_x,
        assisted=False,
        sample_count=sample_count,
        settle_seconds=settle_seconds,
        sample_interval_seconds=sample_interval_seconds,
        sleep=sleep,
    )
    assisted = run_condition(
        transport,
        target_pair,
        phase=phase,
        candidate_x=candidate_x,
        assisted=True,
        sample_count=sample_count,
        settle_seconds=settle_seconds,
        sample_interval_seconds=sample_interval_seconds,
        sleep=sleep,
    )
    return CandidateComparison(phase, candidate_x, baseline, assisted)


def rank_comparisons(
    comparisons: Sequence[CandidateComparison], target_ohm: float
) -> list[dict[str, object]]:
    """Rank sufficiently complete candidates by error, MAD, then failures."""
    ranked: list[dict[str, object]] = []
    for comparison in comparisons:
        payload = comparison.to_dict(target_ohm)
        baseline = payload["baseline"]
        assisted = payload["assisted"]
        assert isinstance(baseline, dict) and isinstance(assisted, dict)
        baseline_summary = baseline["summary"]
        assisted_summary = assisted["summary"]
        assert isinstance(baseline_summary, dict)
        assert isinstance(assisted_summary, dict)
        requested = int(assisted_summary["requested_samples"])
        minimum_success = max(1, math.ceil(requested * MIN_SUCCESS_RATIO))
        baseline_success = int(baseline_summary["successful_samples"])
        assisted_success = int(assisted_summary["successful_samples"])
        eligible = (
            baseline_success >= minimum_success
            and assisted_success >= minimum_success
            and assisted_summary["absolute_median_error_ohm"] is not None
        )
        payload["eligible"] = eligible
        payload["minimum_successful_samples"] = minimum_success
        ranked.append(payload)

    def sort_key(item: dict[str, object]) -> tuple[float, float, int, int]:
        assisted = item["assisted"]
        assert isinstance(assisted, dict)
        summary = assisted["summary"]
        assert isinstance(summary, dict)
        absolute_error = summary["absolute_median_error_ohm"]
        mad = summary["mad_ohm"]
        failed = int(summary["failed_samples"])
        return (
            float(absolute_error) if item["eligible"] else math.inf,
            float(mad) if mad is not None else math.inf,
            failed,
            int(item["candidate_x"]),
        )

    ranked.sort(key=sort_key)
    for index, item in enumerate(ranked, start=1):
        item["rank"] = index if item["eligible"] else None
    return ranked


def improvement_is_confirmed(ranked_item: dict[str, object]) -> bool:
    """Require paired improvement to exceed the larger observed MAD."""
    improvement = ranked_item.get("paired_improvement_ohm")
    if not ranked_item.get("eligible") or not isinstance(improvement, float):
        return False
    baseline = ranked_item["baseline"]
    assisted = ranked_item["assisted"]
    assert isinstance(baseline, dict) and isinstance(assisted, dict)
    baseline_summary = baseline["summary"]
    assisted_summary = assisted["summary"]
    assert isinstance(baseline_summary, dict)
    assert isinstance(assisted_summary, dict)
    observed_noise = max(
        float(baseline_summary["mad_ohm"] or 0.0),
        float(assisted_summary["mad_ohm"] or 0.0),
    )
    return improvement > observed_noise


def run_experiment(
    transport: CommandTransport,
    target_pair: TargetPair,
    candidates: Sequence[int],
    *,
    target_ohm: float,
    screen_samples: int,
    verify_samples: int,
    finalist_count: int,
    settle_seconds: float,
    sample_interval_seconds: float,
    seed: int,
    progress: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Run randomized screening followed by a focused finalist verification."""
    order = list(candidates)
    random.Random(seed).shuffle(order)
    screening: list[CandidateComparison] = []
    total = len(order)
    for index, candidate_x in enumerate(order, start=1):
        progress(
            f"[初筛 {index}/{total}] 辅助回路 S1_X{candidate_x}-S2_X{candidate_x}"
        )
        comparison = run_comparison(
            transport,
            target_pair,
            phase="screening",
            candidate_x=candidate_x,
            sample_count=screen_samples,
            settle_seconds=settle_seconds,
            sample_interval_seconds=sample_interval_seconds,
            sleep=sleep,
        )
        screening.append(comparison)
        item = comparison.to_dict(target_ohm)
        progress(_comparison_progress_text(item))

    screening_ranking = rank_comparisons(screening, target_ohm)
    eligible = [item for item in screening_ranking if item["eligible"]]
    finalist_x = [
        int(item["candidate_x"])
        for item in eligible[: min(finalist_count, len(eligible))]
    ]
    if not finalist_x:
        raise RuntimeError("no candidate produced enough valid screening samples")

    verification: list[CandidateComparison] = []
    for index, candidate_x in enumerate(finalist_x, start=1):
        progress(
            f"[复测 {index}/{len(finalist_x)}] 辅助回路 "
            f"S1_X{candidate_x}-S2_X{candidate_x}"
        )
        comparison = run_comparison(
            transport,
            target_pair,
            phase="verification",
            candidate_x=candidate_x,
            sample_count=verify_samples,
            settle_seconds=settle_seconds,
            sample_interval_seconds=sample_interval_seconds,
            sleep=sleep,
        )
        verification.append(comparison)
        progress(_comparison_progress_text(comparison.to_dict(target_ohm)))

    verification_ranking = rank_comparisons(verification, target_ohm)
    verified_eligible = [item for item in verification_ranking if item["eligible"]]
    if not verified_eligible:
        raise RuntimeError("no finalist produced enough valid verification samples")
    best = dict(verified_eligible[0])
    best["improvement_confirmed"] = improvement_is_confirmed(best)
    return {
        "candidate_order": order,
        "screening_ranking": screening_ranking,
        "finalist_x": finalist_x,
        "verification_ranking": verification_ranking,
        "best": best,
    }


class _ControllerTransport:
    """Adapt correlated GUI requests to the synchronous experiment interface."""

    def __init__(
        self,
        controller: "AuxiliaryLoopTestController",
        target_id: str,
        request_prefix: str,
    ) -> None:
        self._controller = controller
        self._target_id = target_id
        self._request_prefix = request_prefix
        self._sequence = 0

    def request(self, command: str) -> str:
        """Send one command unless cancellation is pending; RESET always remains allowed."""
        if self._controller.cancelled and command != "RESET":
            raise InterruptedError("辅助回路测试已取消")
        self._sequence += 1
        request_id = f"{self._request_prefix}-{self._sequence:04d}"
        return self._controller.request_one(
            self._target_id, request_id, command
        )


class AuxiliaryLoopTestController:
    """Run the auxiliary-loop scan while the GUI owns TCP routing."""

    def __init__(
        self,
        send_request: Callable[[str, str, str], str],
        publish_event: Callable[[str, object], None],
        *,
        report_root: Path = REPORT_ROOT,
        target_pair: TargetPair = TargetPair(),
        candidates: Sequence[int] = DEFAULT_CANDIDATES,
        target_ohm: float = DEFAULT_TARGET_RESISTANCE_OHM,
        screen_samples: int = DEFAULT_SCREEN_SAMPLES,
        verify_samples: int = DEFAULT_VERIFY_SAMPLES,
        finalist_count: int = DEFAULT_FINALIST_COUNT,
        settle_seconds: float = DEFAULT_SETTLE_SECONDS,
        sample_interval_seconds: float = DEFAULT_SAMPLE_INTERVAL_SECONDS,
        response_timeout_seconds: float = DEFAULT_RESPONSE_TIMEOUT_SECONDS,
    ) -> None:
        self._send_request = send_request
        self._publish_event = publish_event
        self._report_root = Path(report_root)
        self._target_pair = target_pair
        self._candidates = tuple(candidates)
        self._target_ohm = target_ohm
        self._screen_samples = screen_samples
        self._verify_samples = verify_samples
        self._finalist_count = finalist_count
        self._settle_seconds = settle_seconds
        self._sample_interval_seconds = sample_interval_seconds
        self._response_timeout_seconds = response_timeout_seconds
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[str, queue.Queue[str]]] = {}
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()

    @property
    def running(self) -> bool:
        """Return whether a worker currently owns the matrix measurement path."""
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def cancelled(self) -> bool:
        """Return whether cooperative cancellation has been requested."""
        return self._cancel.is_set()

    def start(self, target_id: str) -> bool:
        """Start one GUI-owned test and reject overlapping starts."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._cancel.clear()
            thread = threading.Thread(
                target=self._run,
                args=(target_id,),
                daemon=True,
                name="cable-auxiliary-loop-test",
            )
            self._thread = thread
            thread.start()
        return True

    def cancel(self) -> None:
        """Request a cooperative stop; the worker still performs final RESET."""
        self._cancel.set()

    def feed_result(self, target_id: str, request_id: str, payload: str) -> bool:
        """Deliver one correlated ESP RESULT to the waiting test worker."""
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

    def request_one(self, target_id: str, request_id: str, command: str) -> str:
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
                return f"ERR AUX RESULT_TIMEOUT request_id={request_id}"
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def _publish(self, event_type: str, message: object) -> None:
        self._publish_event(event_type, message)

    def _cancelable_sleep(self, seconds: float) -> None:
        if self._cancel.wait(seconds):
            raise InterruptedError("辅助回路测试已取消")

    def _run(self, target_id: str) -> None:
        started_at = datetime.now().astimezone()
        seed = time.time_ns() & 0xFFFFFFFF
        request_prefix = f"ALT-{started_at.strftime('%H%M%S')}"
        transport = _ControllerTransport(
            self, target_id, request_prefix
        )
        terminal_event: tuple[str, object] | None = None
        self._publish(
            "aux_test_started",
            f"目标 {target_id}，固定测量 {self._target_pair.label}="
            f"{self._target_ohm:.3f} ohm，"
            f"扫描 {len(self._candidates)} 条辅助回路。",
        )
        try:
            outcome = run_experiment(
                transport,
                self._target_pair,
                self._candidates,
                target_ohm=self._target_ohm,
                screen_samples=self._screen_samples,
                verify_samples=self._verify_samples,
                finalist_count=self._finalist_count,
                settle_seconds=self._settle_seconds,
                sample_interval_seconds=self._sample_interval_seconds,
                seed=seed,
                progress=lambda message: self._publish(
                    "aux_test_progress", message
                ),
                sleep=self._cancelable_sleep,
            )
            finished_at = datetime.now().astimezone()
            report: dict[str, object] = {
                "target_id": target_id,
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "target_pair": asdict(self._target_pair),
                "target_pair_label": self._target_pair.label,
                "target_resistance_ohm": self._target_ohm,
                "candidate_definition": (
                    "S1_Xn to Y2/Y3 and S2_Xn to Y1/Y0, in parallel with target"
                ),
                "candidates": list(self._candidates),
                "seed": seed,
                "screen_samples": self._screen_samples,
                "verify_samples": self._verify_samples,
                "finalist_count": self._finalist_count,
                "settle_seconds": self._settle_seconds,
                "sample_interval_seconds": self._sample_interval_seconds,
                "minimum_success_ratio": MIN_SUCCESS_RATIO,
                **outcome,
            }
            json_path, csv_path = write_report_files(report, self._report_root)
            best = outcome["best"]
            assert isinstance(best, dict)
            assisted = best["assisted"]
            baseline = best["baseline"]
            assert isinstance(assisted, dict) and isinstance(baseline, dict)
            assisted_summary = assisted["summary"]
            baseline_summary = baseline["summary"]
            assert isinstance(assisted_summary, dict)
            assert isinstance(baseline_summary, dict)
            terminal_event = (
                "aux_test_complete",
                {
                    "target_id": target_id,
                    "candidate_x": best["candidate_x"],
                    "baseline_median_ohm": baseline_summary["median_ohm"],
                    "assisted_median_ohm": assisted_summary["median_ohm"],
                    "improvement_ohm": best["paired_improvement_ohm"],
                    "improvement_confirmed": best["improvement_confirmed"],
                    "json": str(json_path),
                    "csv": str(csv_path),
                },
            )
        except InterruptedError as error:
            terminal_event = ("aux_test_error", str(error))
        except Exception as error:
            terminal_event = ("aux_test_error", f"辅助回路测试失败：{error}")
        finally:
            reset_id = f"{request_prefix}-RESET"
            try:
                reset_payload = self.request_one(target_id, reset_id, "RESET")
                if reset_payload != "OK RESET":
                    self._publish(
                        "aux_test_reset_warning",
                        f"辅助回路测试结束复位失败：{reset_payload}",
                    )
            except Exception as error:
                self._publish(
                    "aux_test_reset_warning",
                    f"辅助回路测试结束复位异常：{error}",
                )
            with self._lock:
                self._pending.clear()
                self._thread = None
            if terminal_event is not None:
                self._publish(*terminal_event)


def _comparison_progress_text(item: dict[str, object]) -> str:
    """Format one concise paired baseline/assisted progress line."""
    baseline = item["baseline"]
    assisted = item["assisted"]
    assert isinstance(baseline, dict) and isinstance(assisted, dict)
    baseline_summary = baseline["summary"]
    assisted_summary = assisted["summary"]
    assert isinstance(baseline_summary, dict)
    assert isinstance(assisted_summary, dict)
    return (
        f"  基线中位数={_format_ohm(baseline_summary['median_ohm'])}，"
        f"闭合后={_format_ohm(assisted_summary['median_ohm'])}，"
        f"误差改善={_format_signed_ohm(item['paired_improvement_ohm'])}"
    )


def _format_ohm(value: object) -> str:
    return "-" if value is None else f"{float(value):.3f} Ω"


def _format_signed_ohm(value: object) -> str:
    return "-" if value is None else f"{float(value):+.3f} Ω"


def _next_report_paths(
    report_root: Path, target_id: str, now: datetime
) -> tuple[str, Path, Path]:
    """Allocate matching JSON/CSV paths without overwriting an earlier run."""
    month_directory = report_root / now.strftime("%Y-%m")
    month_directory.mkdir(parents=True, exist_ok=True)
    date_text = now.strftime("%Y%m%d")
    for sequence in range(1, 1000):
        report_id = f"{date_text}-{sequence:03d}"
        stem = f"辅助回路误差测试_{target_id}_{date_text}_{sequence:03d}"
        json_path = month_directory / f"{stem}.json"
        csv_path = month_directory / f"{stem}.csv"
        if not json_path.exists() and not csv_path.exists():
            return report_id, json_path, csv_path
    raise RuntimeError("report sequence exhausted for today")


def write_report_files(
    report: dict[str, object], report_root: Path = REPORT_ROOT
) -> tuple[Path, Path]:
    """Write one complete machine-readable report and one flat comparison table."""
    now = datetime.now().astimezone()
    target_id = str(report["target_id"])
    report_id, json_path, csv_path = _next_report_paths(report_root, target_id, now)
    report["schema_version"] = 1
    report["report_id"] = report_id
    report["written_at"] = now.isoformat()
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    rows: list[dict[str, object]] = []
    for phase_key in ("screening_ranking", "verification_ranking"):
        for item in list(report[phase_key]):
            assert isinstance(item, dict)
            for condition_name in ("baseline", "assisted"):
                condition = item[condition_name]
                assert isinstance(condition, dict)
                summary = condition["summary"]
                assert isinstance(summary, dict)
                readings = condition["readings"]
                assert isinstance(readings, list)
                errors = condition["errors"]
                assert isinstance(errors, list)
                rows.append(
                    {
                        "phase": item["phase"],
                        "rank": item["rank"],
                        "candidate_x": item["candidate_x"],
                        "auxiliary_loop": item["auxiliary_loop"],
                        "condition": condition_name,
                        "requested_samples": summary["requested_samples"],
                        "successful_samples": summary["successful_samples"],
                        "failed_samples": summary["failed_samples"],
                        "median_ohm": summary["median_ohm"],
                        "mad_ohm": summary["mad_ohm"],
                        "absolute_median_error_ohm": summary[
                            "absolute_median_error_ohm"
                        ],
                        "paired_improvement_ohm": item[
                            "paired_improvement_ohm"
                        ],
                        "paired_improvement_percent": item[
                            "paired_improvement_percent"
                        ],
                        "values_ohm": json.dumps(
                            [reading["resistance_ohm"] for reading in readings]
                        ),
                        "errors": json.dumps(errors, ensure_ascii=False),
                    }
                )
    with csv_path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return json_path, csv_path
