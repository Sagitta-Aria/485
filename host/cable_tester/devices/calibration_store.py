"""Persist and apply one independent GUI calibration profile per ESP ID."""

from __future__ import annotations

from cable_tester.paths import CALIBRATION_PROFILES, CALIBRATION_REPORTS

import hashlib
import json
import math
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping


PORTS_PER_BANK = 24
PROFILE_SCHEMA_VERSION = 1
PROFILE_ROOT = CALIBRATION_PROFILES
REPORT_ROOT = CALIBRATION_REPORTS
SAFE_DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


class CalibrationProfileError(ValueError):
    """Report invalid, incomplete, or unreadable calibration profile data."""


@dataclass(frozen=True)
class DeviceCalibration:
    """Hold the active 48-port display calibration for one exact device ID."""

    target_id: str
    activated_at: str
    values_ohm: dict[str, float]
    profile_path: Path
    enabled: bool = True
    source_report_id: str | None = None
    source_report_path: str | None = None
    quality_warning_count: int = 0


@dataclass(frozen=True)
class CalibrationCandidate:
    """Describe one dated batch report that may be selected for one device."""

    target_id: str
    report_id: str
    finished_at: str
    values_ohm: dict[str, object]
    warning_count: int
    negative_count: int
    report_path: Path
    validation_error: str | None = None

    @property
    def usable(self) -> bool:
        """Return whether the report contains 48 finite numeric values."""
        return self.validation_error is None

    @property
    def display_name(self) -> str:
        """Return a compact selector label with date and quality state."""
        quality_parts = [
            f"警告{self.warning_count}项" if self.warning_count else "无警告"
        ]
        if self.negative_count:
            quality_parts.append(f"负值{self.negative_count}项")
        quality = "，".join(quality_parts)
        usability = "可手动应用" if self.usable else f"不可应用：{self.validation_error}"
        return f"{self.report_id} | {self.finished_at} | {quality} | {usability}"


@dataclass(frozen=True)
class CorrectedMeasurement:
    """Expose raw value and both independently applied endpoint corrections."""

    raw_ohm: float
    positive_port: str
    positive_offset_ohm: float
    negative_port: str
    negative_offset_ohm: float
    corrected_ohm: float


def expected_port_labels() -> set[str]:
    """Return the exact labels required for one complete two-bank profile."""
    return {
        f"{bank}_X{x}"
        for bank in ("S1", "S2")
        for x in range(PORTS_PER_BANK)
    }


def _validate_target_id(target_id: str) -> None:
    if (
        not target_id
        or len(target_id) > 31
        or any(character.isspace() for character in target_id)
    ):
        raise CalibrationProfileError(f"无效设备ID：{target_id!r}")


def _validate_values(
    values_ohm: Mapping[str, object], *, allow_negative: bool = False
) -> dict[str, float]:
    expected = expected_port_labels()
    if set(values_ohm) != expected:
        missing = len(expected - set(values_ohm))
        extra = len(set(values_ohm) - expected)
        raise CalibrationProfileError(
            f"校准端口不完整：缺少{missing}项，多出{extra}项"
        )

    validated: dict[str, float] = {}
    for label in sorted(expected):
        try:
            value = float(values_ohm[label])
        except (TypeError, ValueError) as error:
            raise CalibrationProfileError(f"{label}校准值不是数字") from error
        if not math.isfinite(value) or (value < 0.0 and not allow_negative):
            raise CalibrationProfileError(f"{label}校准值无效：{value}")
        validated[label] = value
    return validated


class CalibrationStore:
    """Read and atomically update ID-isolated active calibration profiles."""

    def __init__(
        self,
        root: Path = PROFILE_ROOT,
        report_root: Path = REPORT_ROOT,
    ) -> None:
        self._root = Path(root)
        self._report_root = Path(report_root)
        self._cache: dict[str, DeviceCalibration | None] = {}
        self._lock = threading.Lock()

    def profile_path(self, target_id: str) -> Path:
        """Return a traversal-safe, readable active-profile path for an ID."""
        _validate_target_id(target_id)
        if SAFE_DEVICE_ID_PATTERN.fullmatch(target_id):
            directory_name = target_id
        else:
            readable = re.sub(r"[^A-Za-z0-9_-]+", "_", target_id).strip("_")
            digest = hashlib.sha256(target_id.encode("utf-8")).hexdigest()[:8]
            directory_name = f"{readable or 'DEVICE'}_{digest}"
        return self._root / directory_name / "active.json"

    def activate(
        self,
        target_id: str,
        values_ohm: Mapping[str, object],
        *,
        source_report_id: str | None = None,
        source_report_path: str | None = None,
        quality_warning_count: int = 0,
        allow_negative: bool = False,
    ) -> Path:
        """Persist a complete profile for only target_id and make it active."""
        validated = _validate_values(values_ohm, allow_negative=allow_negative)
        profile_path = self.profile_path(target_id)
        activated_at = datetime.now().astimezone().isoformat(timespec="seconds")
        payload = {
            "schema_version": PROFILE_SCHEMA_VERSION,
            "target_id": target_id,
            "activated_at": activated_at,
            "application_scope": "GUI display only",
            "enabled": True,
            "source_report_id": source_report_id,
            "source_report_path": source_report_path,
            "quality_warning_count": quality_warning_count,
            "values_ohm": validated,
        }

        profile_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = profile_path.with_suffix(".tmp.json")
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary_path.replace(profile_path)
        with self._lock:
            self._cache[target_id] = DeviceCalibration(
                target_id=target_id,
                activated_at=activated_at,
                values_ohm=validated,
                profile_path=profile_path,
                enabled=True,
                source_report_id=source_report_id,
                source_report_path=source_report_path,
                quality_warning_count=quality_warning_count,
            )
        return profile_path

    def load_selection(self, target_id: str) -> DeviceCalibration | None:
        """Load the selected profile for target_id even when it is disabled."""
        _validate_target_id(target_id)
        with self._lock:
            if target_id in self._cache:
                return self._cache[target_id]

        profile_path = self.profile_path(target_id)
        if not profile_path.exists():
            with self._lock:
                self._cache[target_id] = None
            return None
        try:
            payload = json.loads(profile_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise CalibrationProfileError(
                f"无法读取{target_id}校准配置：{error}"
            ) from error
        if payload.get("schema_version") != PROFILE_SCHEMA_VERSION:
            raise CalibrationProfileError(f"{target_id}校准配置版本不支持")
        if payload.get("target_id") != target_id:
            raise CalibrationProfileError(
                f"校准配置ID不匹配：期望{target_id}，实际{payload.get('target_id')}"
            )
        values = payload.get("values_ohm")
        if not isinstance(values, dict):
            raise CalibrationProfileError(f"{target_id}校准配置缺少values_ohm")
        validated = _validate_values(values, allow_negative=True)
        calibration = DeviceCalibration(
            target_id=target_id,
            activated_at=str(payload.get("activated_at", "unknown")),
            values_ohm=validated,
            profile_path=profile_path,
            enabled=payload.get("enabled", True) is True,
            source_report_id=(
                str(payload["source_report_id"])
                if payload.get("source_report_id")
                else None
            ),
            source_report_path=(
                str(payload["source_report_path"])
                if payload.get("source_report_path")
                else None
            ),
            quality_warning_count=int(payload.get("quality_warning_count", 0)),
        )
        with self._lock:
            self._cache[target_id] = calibration
        return calibration

    def load(self, target_id: str) -> DeviceCalibration | None:
        """Load target_id's profile only when display correction is enabled."""
        calibration = self.load_selection(target_id)
        if calibration is None or not calibration.enabled:
            return None
        return calibration

    def set_enabled(self, target_id: str, enabled: bool) -> DeviceCalibration:
        """Persistently enable or disable the selected profile without deleting it."""
        calibration = self.load_selection(target_id)
        if calibration is None:
            raise CalibrationProfileError(f"{target_id}没有可启用的校准配置")
        activated_at = (
            datetime.now().astimezone().isoformat(timespec="seconds")
            if enabled
            else calibration.activated_at
        )
        payload = {
            "schema_version": PROFILE_SCHEMA_VERSION,
            "target_id": target_id,
            "activated_at": activated_at,
            "application_scope": "GUI display only",
            "enabled": bool(enabled),
            "source_report_id": calibration.source_report_id,
            "source_report_path": calibration.source_report_path,
            "quality_warning_count": calibration.quality_warning_count,
            "values_ohm": calibration.values_ohm,
        }
        temporary_path = calibration.profile_path.with_suffix(".tmp.json")
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary_path.replace(calibration.profile_path)
        updated = DeviceCalibration(
            target_id=target_id,
            activated_at=activated_at,
            values_ohm=calibration.values_ohm,
            profile_path=calibration.profile_path,
            enabled=bool(enabled),
            source_report_id=calibration.source_report_id,
            source_report_path=calibration.source_report_path,
            quality_warning_count=calibration.quality_warning_count,
        )
        with self._lock:
            self._cache[target_id] = updated
        return updated

    def list_candidates(self, target_id: str) -> list[CalibrationCandidate]:
        """Return newest-first batch-report candidates belonging to target_id."""
        _validate_target_id(target_id)
        candidates: list[CalibrationCandidate] = []
        if not self._report_root.exists():
            return candidates
        for report_path in self._report_root.rglob("*.json"):
            try:
                payload = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if payload.get("target_id") != target_id:
                continue
            raw_values = payload.get("estimates_ohm")
            values = raw_values if isinstance(raw_values, dict) else {}
            validation_error: str | None = None
            if payload.get("complete") is not True:
                validation_error = "未解出全部48路"
            else:
                try:
                    validated = _validate_values(values, allow_negative=True)
                except CalibrationProfileError as error:
                    validation_error = str(error)
                else:
                    values = validated
            warnings = payload.get("warnings")
            warning_count = len(warnings) if isinstance(warnings, list) else 0
            negative_count = sum(
                float(value) < 0.0
                for value in values.values()
                if isinstance(value, (int, float))
            )
            candidates.append(
                CalibrationCandidate(
                    target_id=target_id,
                    report_id=str(payload.get("report_id", report_path.stem)),
                    finished_at=str(payload.get("finished_at", "时间未知")),
                    values_ohm=values,
                    warning_count=warning_count,
                    negative_count=negative_count,
                    report_path=report_path.resolve(),
                    validation_error=validation_error,
                )
            )
        return sorted(
            candidates,
            key=lambda candidate: (candidate.finished_at, candidate.report_id),
            reverse=True,
        )

    def activate_candidate(
        self, target_id: str, candidate: CalibrationCandidate
    ) -> Path:
        """Select one validated report candidate and enable it for target_id."""
        _validate_target_id(target_id)
        if candidate.target_id != target_id:
            raise CalibrationProfileError(
                f"候选配置ID不匹配：期望{target_id}，实际{candidate.target_id}"
            )
        if not candidate.usable:
            raise CalibrationProfileError(
                f"{candidate.report_id}不可应用：{candidate.validation_error}"
            )
        try:
            relative_source = candidate.report_path.resolve().relative_to(
                self._report_root.resolve()
            )
        except ValueError as error:
            raise CalibrationProfileError("候选配置不在工程报告目录中") from error
        return self.activate(
            target_id,
            candidate.values_ohm,
            source_report_id=candidate.report_id,
            source_report_path=str(relative_source),
            quality_warning_count=candidate.warning_count,
            allow_negative=True,
        )

    def correct(
        self,
        target_id: str,
        positive_port: str,
        negative_port: str,
        raw_ohm: float,
    ) -> CorrectedMeasurement | None:
        """Subtract the two endpoint values from this device's raw reading."""
        calibration = self.load(target_id)
        if calibration is None:
            return None
        try:
            positive_offset = calibration.values_ohm[positive_port]
            negative_offset = calibration.values_ohm[negative_port]
        except KeyError as error:
            raise CalibrationProfileError(
                f"{target_id}校准配置缺少端口：{error.args[0]}"
            ) from error
        return CorrectedMeasurement(
            raw_ohm=raw_ohm,
            positive_port=positive_port,
            positive_offset_ohm=positive_offset,
            negative_port=negative_port,
            negative_offset_ohm=negative_offset,
            corrected_ohm=raw_ohm - positive_offset - negative_offset,
        )
