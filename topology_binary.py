"""Search every branch with repeated probes supplied by the scan controller.

Group exclusions assume a stable OR response. Complement checks and exhaustive
fallback detect many violations, but cannot prove an instrument never lies.
"""

from __future__ import annotations

from collections.abc import Callable


class BinaryRowSearch:
    """Search one source against all destinations, retaining incomplete/conflicting evidence."""

    def __init__(self, count: int, probe: Callable[[int, int], int | None]) -> None:
        if not 1 <= count <= 240:
            raise ValueError("destination count must be in 1..240")
        self.count = count
        self.probe = probe
        self.decisions: dict[int, int | None] = {}
        self.points: dict[int, int | None] = {}
        self.conflicts: list[dict[str, object]] = []
        self.complete = False

    def _point(self, port: int) -> None:
        """Only a stable isolated probe can add a confirmed connection."""
        bit = self.probe(port, port + 1)
        previous = self.points.get(port)
        if previous is not None and bit is not None and previous != bit:
            self.conflicts.append({"first": port, "end": port + 1, "reason": "POINT_CHANGED"})
        self.points[port] = self.decisions[port] = bit

    def _exhaustive(self, first: int, end: int) -> None:
        """Recheck every unresolved/negative leaf when group evidence contradicts it."""
        for port in range(first, end):
            if self.points.get(port) != 1:
                self._point(port)

    def _visit(self, first: int, end: int) -> None:
        """Visit both children of a positive or uncertain group; never stop at one hit."""
        if end - first == 1:
            self._point(first)
            return
        bit = self.probe(first, end)
        if bit == 0:
            self.decisions.update((port, 0) for port in range(first, end))
            return
        if bit is None:
            self.conflicts.append({"first": first, "end": end, "reason": "UNSTABLE_GROUP"})
        middle = (first + end) // 2
        self._visit(first, middle)
        self._visit(middle, end)
        if bit == 1 and not any(self.decisions.get(port) == 1 for port in range(first, end)):
            self.conflicts.append({"first": first, "end": end, "reason": "GROUP_POINT_CONFLICT"})
            self._exhaustive(first, end)

    def run(self) -> None:
        """Search, then independently audit the entire complement of confirmed targets."""
        self._visit(0, self.count)
        first = 0
        while first < self.count:
            if self.points.get(first) == 1:
                first += 1
                continue
            end = first + 1
            while end < self.count and self.points.get(end) != 1:
                end += 1
            bit = self.probe(first, end)
            if bit != 0:
                self.conflicts.append({"first": first, "end": end, "reason": "COMPLEMENT_NOT_CLEAR"})
                self._exhaustive(first, end)
            first = end
        self.complete = True

    @property
    def targets(self) -> tuple[int, ...]:
        return tuple(port for port in range(self.count) if self.points.get(port) == 1)

    @property
    def status(self) -> str:
        if not self.complete or any(self.decisions.get(port) is None for port in range(self.count)):
            return "UNKNOWN"
        if self.conflicts:
            return "INCONSISTENT"
        return "SHORT" if len(self.targets) > 1 else "UNIQUE" if self.targets else "NO_CONTINUITY"
