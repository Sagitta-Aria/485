"""Durably accept verified topology records before acknowledging device storage."""

from __future__ import annotations

import json
import re
import sqlite3
import zlib
from dataclasses import dataclass
from pathlib import Path


class TransferCorruption(ValueError):
    """A damaged frame can be requested again without advancing the durable cursor."""


class SequenceGap(ValueError):
    """A missing sequence requires fetching from the last committed cursor."""


class ReplayConflict(ValueError):
    """The device reused an accepted sequence for different data; stop the session."""


@dataclass(frozen=True)
class TransferRecord:
    session: int
    sequence: int
    job: int
    crc: str
    payload: str

    @classmethod
    def parse(cls, frame: str, session: int) -> "TransferRecord":
        """Validate framing and the CRC over the exact ASCII record identity/payload."""
        fields = frame.split(maxsplit=5)
        if len(fields) != 6 or fields[0] != "TOPO_DATA":
            raise TransferCorruption("invalid TOPO_DATA frame")
        if not all(re.fullmatch(r"[0-9]+", value) for value in fields[1:4]):
            raise TransferCorruption("invalid TOPO_DATA coordinates")
        sid, sequence, job = map(int, fields[1:4])
        if sid != session or sequence < 1 or job < 1:
            raise ReplayConflict("TOPO_DATA belongs to another session or invalid sequence/job")
        if not re.fullmatch(r"[0-9a-fA-F]{8}", fields[4]):
            raise TransferCorruption("invalid TOPO_DATA checksum")
        try:
            raw = f"{sid} {sequence} {job} {fields[5]}".encode("ascii")
        except UnicodeEncodeError as error:
            raise TransferCorruption("TOPO_DATA payload is not ASCII") from error
        crc = f"{zlib.crc32(raw):08x}"
        if crc != fields[4].lower():
            raise TransferCorruption(f"TOPO_DATA CRC mismatch at sequence {sequence}")
        return cls(sid, sequence, job, crc, fields[5])


class TopologyTransferStore:
    """One worker-owned SQLite journal; only committed contiguous data may be ACKed."""

    @staticmethod
    def read_configuration(path: Path, session: int) -> dict | None:
        """Read the original cleanup scope without creating or changing a saved journal."""
        database = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            identity = database.execute("SELECT value FROM metadata WHERE name='session'").fetchone()
            if identity is None or identity[0] != str(session):
                raise ReplayConflict("SQLite journal belongs to another scan session")
            row = database.execute("SELECT value FROM metadata WHERE name='configuration'").fetchone()
            configuration = json.loads(row[0]) if row else None
            if configuration is not None and not isinstance(configuration, dict):
                raise ReplayConflict("invalid previous-session configuration")
            return configuration
        finally:
            database.close()

    def __init__(self, path: Path, session: int, configuration: dict | None = None, *, acknowledged_prefix: int = 0) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.session = session
        self._db = sqlite3.connect(self.path)
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
            self._db.execute("CREATE TABLE IF NOT EXISTS records (sequence INTEGER PRIMARY KEY, job INTEGER NOT NULL, crc TEXT NOT NULL, payload TEXT NOT NULL)")
            self._db.execute("CREATE TABLE IF NOT EXISTS jobs (job INTEGER PRIMARY KEY, command TEXT NOT NULL)")
            previous = self._db.execute("SELECT value FROM metadata WHERE name='session'").fetchone()
            if previous is not None and previous[0] != str(session):
                raise ReplayConflict("SQLite journal belongs to another scan session")
            self._db.execute("INSERT OR IGNORE INTO metadata VALUES ('session', ?)", (str(session),))
            self._db.execute("INSERT OR IGNORE INTO metadata VALUES ('acknowledged_prefix', ?)", (str(acknowledged_prefix),))
            if configuration is not None:
                self._db.execute("INSERT OR IGNORE INTO metadata VALUES ('configuration', ?)", (json.dumps(configuration, ensure_ascii=False),))
            self._db.commit()
            self.acknowledged_prefix = int(self._db.execute("SELECT value FROM metadata WHERE name='acknowledged_prefix'").fetchone()[0])
            row = self._db.execute("SELECT COUNT(*), COALESCE(MAX(sequence), 0), MIN(sequence) FROM records").fetchone()
            if self.acknowledged_prefix < 0 or (row[0] and (row[0] != row[1] - self.acknowledged_prefix or row[2] != self.acknowledged_prefix + 1)):
                raise ReplayConflict("SQLite journal has a noncontiguous sequence")
            self.cursor = int(row[1]) if row[0] else self.acknowledged_prefix
        except Exception:
            self._db.close()
            raise

    def record_job(self, job: int, command: str) -> None:
        """Persist the exact idempotent job command before it can reach the device."""
        previous = self._db.execute("SELECT command FROM jobs WHERE job=?", (job,)).fetchone()
        if previous is not None and previous[0] != command:
            raise ReplayConflict("job ID reused for a different command")
        with self._db:
            self._db.execute("INSERT OR IGNORE INTO jobs VALUES (?, ?)", (job, command))

    def accept(self, record: TransferRecord) -> bool:
        """Commit a new contiguous record, or verify an exact duplicate without reapplying it."""
        if record.session != self.session:
            raise ReplayConflict("record session differs from durable journal")
        if record.sequence <= self.cursor:
            previous = self._db.execute("SELECT job, crc, payload FROM records WHERE sequence=?", (record.sequence,)).fetchone()
            if previous != (record.job, record.crc, record.payload):
                raise ReplayConflict(f"conflicting replay at sequence {record.sequence}")
            return False
        if record.sequence != self.cursor + 1:
            raise SequenceGap(f"expected sequence {self.cursor + 1}, received {record.sequence}")
        with self._db:
            self._db.execute("INSERT INTO records VALUES (?, ?, ?, ?)", (record.sequence, record.job, record.crc, record.payload))
        self.cursor = record.sequence
        return True

    def acknowledgement(self) -> tuple[int, str] | None:
        """Return only the highest durable sequence and its checksum for cumulative ACK."""
        if not self.cursor:
            return None
        row = self._db.execute("SELECT crc FROM records WHERE sequence=?", (self.cursor,)).fetchone()
        return (self.cursor, row[0]) if row is not None else None

    def export(self) -> dict:
        """Describe recovered raw records separately from the new scan's decoded rows."""
        configuration = self._db.execute("SELECT value FROM metadata WHERE name='configuration'").fetchone()
        return {
            "schema": 1, "session_id": self.session, "durable_sequence": self.cursor,
            "acknowledged_prefix_without_local_records": self.acknowledged_prefix,
            "configuration": json.loads(configuration[0]) if configuration else None,
            "records": [{"sequence": sequence, "job": job, "crc": crc, "payload": payload}
                        for sequence, job, crc, payload in self._db.execute("SELECT sequence, job, crc, payload FROM records ORDER BY sequence")],
        }

    def close(self) -> None:
        """Release SQLite after report creation; committed data remains recoverable on disk."""
        self._db.close()
