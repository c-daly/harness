"""Session verification and authorized repair utilities.

Quick/full integrity reports over one session's log and blobs, plus an
authorization token to run a bounded, explicit repair (torn-tail only for now).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field

from harness.blobs import BlobRef
from harness.events import Envelope
from harness.log import SessionLock, SessionLockedError, TornLogError, _scan
from harness.types import SessionId


@dataclass(frozen=True)
class Finding:
    severity: Literal["info", "warn", "error"]
    message: str
    # Optional correlated identifiers for human debugging
    rel_path: str | None = None
    seq: int | None = None
    call_id: str | None = None
    remediation: str | None = None


class IntegrityReport(BaseModel):
    model_config = ConfigDict(frozen=True)
    session_id: SessionId
    findings: tuple[Finding, ...] = ()  # stable ordering in construction
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @staticmethod
    def _digest_payload(session_id: SessionId, findings: Iterable[Finding], *, log_bytes_sha256: str) -> str:
        # Normalize to a stable, sorted JSON representation without nondeterminism.
        # Ephemeral INFO-level findings (e.g., lock presence) are excluded from the digest.
        stable_findings = [dataclasses.asdict(f) for f in findings if f.severity != "info"]
        payload = {
            "session_id": str(session_id),
            "findings": stable_findings,
            "log": {"sha256": log_bytes_sha256},
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(canonical).hexdigest()

    @classmethod
    def build(cls, session_id: SessionId, findings: list[Finding], *, log_bytes_sha256: str) -> "IntegrityReport":
        digest = cls._digest_payload(session_id, findings, log_bytes_sha256=log_bytes_sha256)
        # freeze Finding list as a tuple for report stability
        return cls(session_id=session_id, findings=tuple(findings), report_sha256=digest)


def _session_dir(base: Path, session_id: SessionId) -> Path:
    return Path(base) / "sessions" / str(session_id)


def _list_blob_paths(base: Path, session_id: SessionId) -> set[str]:
    root = _session_dir(base, session_id) / "blobs"
    if not root.exists():
        return set()
    entries: set[str] = set()
    for p in root.iterdir():
        if p.is_file():
            entries.add(p.name)
    return entries


def _iter_blob_refs(envelopes: list[Envelope]) -> Iterable[tuple[int, BlobRef]]:
    # Conservative search: scan event dicts for BlobRef-shaped mappings.
    from harness.blobs import BlobRef as _BlobRef

    for env in envelopes:
        event_dict = env.event.model_dump()
        stack = [event_dict]
        while stack:
            obj = stack.pop()
            if isinstance(obj, dict):
                # Possible blob
                if set(obj.keys()) >= {"sha256", "size"}:
                    try:
                        ref = _BlobRef.model_validate(obj)
                        yield env.seq, ref
                    except Exception:
                        pass
                for v in obj.values():
                    stack.append(v)
            elif isinstance(obj, (list, tuple)):
                stack.extend(obj)
    return


def verify_session(base: Path, session_id: SessionId, mode: Literal["quick", "full"]) -> IntegrityReport:
    """Scan one session for structural and storage integrity problems.

    - sequence monotonicity and envelope parse is validated by the reader
    - intent facts are not paired here (timeline logic lives elsewhere)
    - quick: presence/size/type of blobs referenced from the log
    - full: also hash every blob to detect same-size corruption
    - lock state is reported as an informational finding
    """
    # Session log lives at base/sessions/<id>.jsonl
    path = Path(base) / "sessions" / f"{session_id}.jsonl"
    raw = path.read_bytes()
    envelopes, offset = _scan(raw, session_id)
    findings: list[Finding] = []

    # Lock state marker at base/sessions/<id>.lock
    lock_path = Path(base) / "sessions" / f"{session_id}.lock"
    if lock_path.exists():
        findings.append(
            Finding(
                severity="info",
                message="session is locked",
                rel_path=str(Path("sessions") / str(session_id) / lock_path.name),
            )
        )

    # Torn tail
    if offset is not None:
        findings.append(
            Finding(
                severity="error",
                message=f"torn tail at byte {offset}",
                rel_path=str(Path("sessions") / str(session_id) / f"{session_id}.jsonl"),
                remediation="authorize torn-tail repair and retry",
            )
        )

    # Blob checks
    for seq, ref in _iter_blob_refs(envelopes):
        blob_path = Path("sessions") / str(session_id) / "blobs" / ref.sha256
        abs_path = _session_dir(base, session_id) / "blobs" / ref.sha256
        try:
            st = abs_path.stat()
        except FileNotFoundError:
            findings.append(
                Finding(
                    severity="error",
                    message="missing blob",
                    rel_path=str(blob_path),
                    seq=seq,
                    remediation="restore from backup or discard the referencing event",
                )
            )
            continue
        if not abs_path.is_file() or st.st_size != ref.size:
            findings.append(
                Finding(
                    severity="error",
                    message="blob size/type mismatch",
                    rel_path=str(blob_path),
                    seq=seq,
                    remediation="remove and re-ingest, or restore the correct object",
                )
            )
            continue
        if mode == "quick":
            findings.append(
                Finding(
                    severity="info",
                    message="blob present",
                    rel_path=str(blob_path),
                    seq=seq,
                )
            )
        elif mode == "full":
            import hashlib as _hl

            with open(abs_path, "rb") as fh:
                data = fh.read()
            digest = _hl.sha256(data).hexdigest()
            if digest != ref.sha256:
                findings.append(
                    Finding(
                        severity="error",
                        message="blob digest mismatch",
                        rel_path=str(blob_path),
                        seq=seq,
                        remediation="remove and re-ingest, or restore the correct object",
                    )
                )

    log_sha256 = hashlib.sha256(raw).hexdigest()
    return IntegrityReport.build(session_id, findings, log_bytes_sha256=log_sha256)


class RepairAuthorization(BaseModel):
    """One bounded repair operation token the CLI can pass to the kernel.

    The token binds to session and a specific integrity snapshot. Frozen; safe to
    persist for a short time if the operator wants to confirm before proceeding.
    """

    model_config = ConfigDict(frozen=True)
    session_id: SessionId
    operation: Literal["torn-tail"]
    integrity_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


def repair_session(base: Path, session_id: SessionId, authorization: RepairAuthorization) -> None:
    """Apply a bounded, explicit repair after confirming the integrity snapshot.

    - reacquire the session lock (refuse a held session)
    - recompute the integrity report; refuse if the hash or session mismatches
    - perform only the requested repair
    """
    if authorization.session_id != session_id:
        raise ValueError("authorization applies to a different session")
    if authorization.operation != "torn-tail":
        raise ValueError("unsupported repair operation")

    # refuse live ownership
    try:
        lock = SessionLock(base, session_id)
    except SessionLockedError as exc:
        raise TornLogError(f"{session_id}: repair refused — session lock held") from exc
    with lock:
        # recompute
        report = verify_session(base, session_id, mode="quick")
        if report.report_sha256 != authorization.integrity_report_sha256:
            raise ValueError("stale integrity report: re-verify before repairing")
        # perform torn-tail repair by delegating to the log reader
        from harness.log import read_session

        read_session(base, session_id, repair=True, _lock=lock)


def main(argv: list[str] | None = None) -> None:
    """Minimal sessions admin CLI stub to expose --help and wiring.

    The full lifecycle operations are implemented incrementally; this entry point
    ensures `harness sessions --help` works and provides top-level usage.
    """
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="harness sessions", description="Manage Harness session logs")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("list", help="List known sessions")
    show = sub.add_parser("show", help="Show a session summary")
    show.add_argument("session_id")
    verify = sub.add_parser("verify", help="Verify a session's integrity")
    verify.add_argument("session_id")
    verify.add_argument("--full", action="store_true", help="Perform full verification (hash blobs)")
    repair = sub.add_parser("repair", help="Repair a session (authorized)")
    repair.add_argument("session_id")
    export = sub.add_parser("export", help="Export a session as a tar archive")
    export.add_argument("session_id")
    sub.add_parser("trash", help="Move a session into trash (disabled while locked)")
    sub.add_parser("restore", help="Restore a trashed session")
    sub.add_parser("purge", help="Permanently delete a trashed session")

    # if just --help, argparse will handle it; otherwise, print minimal guidance
    args = parser.parse_args(argv or sys.argv[1:])
    if not args.cmd:
        parser.print_help()
        return

    # Placeholder no-op actions until full implementation lands
    if args.cmd in {"list", "show", "verify", "repair", "export", "trash", "restore", "purge"}:
        # Exits successfully for operator --help smoke; real actions will be implemented later
        return
