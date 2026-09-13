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
from harness.events import ToolCallProposed, ToolCallCompleted, ToolCallCancelled, ToolCallAborted
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

    # Intent pairing: warn about dangling tool-call intents with remediation guidance.
    intents: dict[str, int] = {}
    settled: set[str] = set()
    for env in envelopes:
        ev = env.event
        if isinstance(ev, ToolCallProposed):
            intents[str(ev.call_id)] = env.seq
        elif isinstance(ev, (ToolCallCompleted, ToolCallCancelled, ToolCallAborted)):
            settled.add(str(ev.call_id))
    for call_id, seq in intents.items():
        if call_id not in settled:
            findings.append(
                Finding(
                    severity="warn",
                    message="dangling tool-call intent",
                    rel_path=str(Path("sessions") / str(session_id) / f"{session_id}.jsonl"),
                    seq=seq,
                    call_id=call_id,
                    remediation="resume the session to reconcile, or abort the call explicitly",
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
    """Sessions lifecycle CLI.

    Interface note: accepts either
      harness sessions <base> <cmd> [args...]  (preferred in tests)
    or
      harness sessions <cmd> [args...]         (uses default base)
    """
    import argparse
    import sys
    import os
    from pathlib import Path as _P

    argv = list(argv or sys.argv[1:])

    # Pre-parse for flexible base-dir placement
    base = _P.home() / ".local/share/harness"
    # Extract a --base-dir flag anywhere
    if "--base-dir" in argv:
        i = argv.index("--base-dir")
        if i + 1 >= len(argv):
            raise SystemExit("--base-dir requires a path")
        base = _P(argv[i + 1])
        del argv[i : i + 2]
    commands = {"list", "show", "verify", "repair", "export", "trash", "restore", "purge"}
    # Treat a leading non-command as a base path
    if argv and not argv[0].startswith("-") and argv[0] not in commands:
        base = _P(argv.pop(0))
    # Or accept `sessions <cmd> <base> ...` ordering
    if argv and argv[0] in commands and len(argv) >= 2:
        nxt = argv[1]
        if not nxt.startswith("-") and (nxt.startswith("/") or nxt.startswith(".") or "/" in nxt):
            base = _P(argv.pop(1))

    parser = argparse.ArgumentParser(prog="harness sessions", description="Manage Harness session logs")
    # Do not require a subcommand: default to 'list' when missing for a friendly entrypoint.
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("list", help="List known sessions")
    show = sub.add_parser("show", help="Show a session summary")
    show.add_argument("session_id")
    verify = sub.add_parser("verify", help="Verify a session's integrity")
    verify.add_argument("session_id")
    mode = verify.add_mutually_exclusive_group()
    mode.add_argument("--quick", action="store_true", help="Shallow checks (default)")
    mode.add_argument("--full", action="store_true", help="Hash blobs to detect same-size corruption")
    repair = sub.add_parser("repair", help="Repair a session (authorized)")
    repair.add_argument("session_id")
    export = sub.add_parser("export", help="Export a session as a tar archive")
    export.add_argument("session_id")
    export.add_argument("--output", help="Output tar path")
    trash = sub.add_parser("trash", help="Move a session into trash (disabled while locked)")
    trash.add_argument("session_id")
    restore = sub.add_parser("restore", help="Restore a trashed session")
    restore.add_argument("session_id")
    purge = sub.add_parser("purge", help="Permanently delete a trashed session")
    purge.add_argument("session_id")
    # Accept either a positional confirmation id or a --confirm flag for compatibility with tests.
    purge.add_argument("confirm_id", nargs="?")
    purge.add_argument("--confirm", dest="confirm_id")

    args = parser.parse_args(argv)

    # Default command
    if not getattr(args, "cmd", None):
        args.cmd = "list"

    if args.cmd == "list":
        # One id per line; simple, greppable output for tests and operators.
        for s in __import__("harness.sessions").sessions.list_sessions(base):  # type: ignore[attr-defined]
            print(str(s.session_id))
        return
    if args.cmd == "show":
        from harness.sessions import list_sessions

        for s in list_sessions(base):
            if str(s.session_id) == args.session_id:
                # Present as minimal JSON-like lines for now
                import json as _json

                out = {
                    "session_id": str(s.session_id),
                    "mtime": s.mtime,
                    "event_count": s.event_count,
                    "first_prompt": s.first_prompt,
                    "last_model": s.last_model,
                    "locked": s.locked,
                    "error": s.error,
                }
                print(_json.dumps(out))
                return
        raise SystemExit("unknown session id")
    if args.cmd == "verify":
        mode = "full" if getattr(args, "full", False) else "quick"
        rep = verify_session(base, SessionId(args.session_id), mode=mode)  # type: ignore[arg-type]
        import json as _json

        print(
            _json.dumps(
                {
                    "session_id": str(rep.session_id),
                    "report_sha256": rep.report_sha256,
                    "findings": [dataclasses.asdict(f) for f in rep.findings],
                }
            )
        )
        return
    if args.cmd == "export":
        # refuse a locked session (presence of lock marker)
        lock = base / "sessions" / f"{args.session_id}.lock"
        if lock.exists():
            raise SystemExit("session is locked")
        # Build a deterministic tar archive with fixed metadata
        import io
        import json as _json
        import tarfile

        sid = SessionId(args.session_id)  # type: ignore[arg-type]
        sess_dir = base / "sessions"
        log_path = sess_dir / f"{sid}.jsonl"
        blobs_dir = base / "sessions" / str(sid) / "blobs"
        out_path = Path(args.output) if getattr(args, "output", None) else (base / f"{sid}.tar")
        out_path.parent.mkdir(parents=True, exist_ok=True)

        def _info(name: str, size: int = 0, mode: int = 0o600, type=tarfile.REGTYPE):
            ti = tarfile.TarInfo(name)
            ti.size = size
            ti.mode = mode
            ti.mtime = 0
            ti.uid = 0
            ti.gid = 0
            ti.uname = ""
            ti.gname = ""
            ti.type = type
            return ti

        with tarfile.open(out_path, mode="w", format=tarfile.USTAR_FORMAT) as tf:
            # directory entries
            tf.addfile(_info("sessions", 0, 0o700, type=tarfile.DIRTYPE))
            tf.addfile(_info(f"sessions/{sid}", 0, 0o700, type=tarfile.DIRTYPE))
            # log first to satisfy lexicographic order ('.jsonl' sorts before '/blobs')
            data = log_path.read_bytes()
            tf.addfile(_info(f"sessions/{sid}.jsonl", len(data)), io.BytesIO(data))
            # then the blobs directory and blob files in sorted order
            tf.addfile(_info(f"sessions/{sid}/blobs", 0, 0o700, type=tarfile.DIRTYPE))
            if blobs_dir.is_dir():
                names = sorted([p.name for p in blobs_dir.iterdir() if p.is_file()])
                for name in names:
                    b = (blobs_dir / name).read_bytes()
                    tf.addfile(_info(f"sessions/{sid}/blobs/{name}", len(b)), io.BytesIO(b))
            # integrity manifest last ("manifest" sorts after "blobs")
            rep = verify_session(base, sid, mode="quick")
            import hashlib as _hl

            log_sha256 = _hl.sha256(data).hexdigest()
            manifest = _json.dumps(
                {
                    "session_id": str(rep.session_id),
                    "report_sha256": rep.report_sha256,
                    "log_sha256": log_sha256,
                    "findings": [dataclasses.asdict(f) for f in rep.findings],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            tf.addfile(_info(f"sessions/{sid}/manifest.json", len(manifest)), io.BytesIO(manifest))
        return
    if args.cmd == "trash":
        sid = SessionId(args.session_id)  # type: ignore[arg-type]
        if (base / "sessions" / f"{sid}.lock").exists():
            raise SystemExit("session is locked")
        trash = base / "trash"
        trash.mkdir(parents=True, exist_ok=True)
        # Force 0700 on the trash directory (Unix-only; ignore errors on other platforms)
        try:
            os.chmod(trash, 0o700)
        except Exception:
            pass
        dst_root = trash / str(sid)
        if dst_root.exists():
            raise SystemExit("already trashed")
        dst_root.mkdir()
        # move log and per-session dir if present
        log = base / "sessions" / f"{sid}.jsonl"
        if log.exists():
            log.rename(dst_root / f"{sid}.jsonl")
        sess = base / "sessions" / str(sid)
        if sess.exists():
            sess.rename(dst_root / str(sid))
        return
    if args.cmd == "restore":
        sid = SessionId(args.session_id)  # type: ignore[arg-type]
        if (base / "sessions" / f"{sid}.lock").exists():
            raise SystemExit("session is locked")
        src_root = base / "trash" / str(sid)
        if not src_root.exists():
            raise SystemExit("not in trash")
        (base / "sessions").mkdir(parents=True, exist_ok=True)
        # move back
        log = src_root / f"{sid}.jsonl"
        if log.exists():
            log.rename(base / "sessions" / f"{sid}.jsonl")
        sess = src_root / str(sid)
        if sess.exists():
            sess.rename(base / "sessions" / str(sid))
        # remove empty dir
        try:
            src_root.rmdir()
        except OSError:
            pass
        return
    if args.cmd == "purge":
        sid = SessionId(args.session_id)  # type: ignore[arg-type]
        if args.confirm_id != str(sid):
            raise SystemExit("confirmation id mismatch")
        if (base / "sessions" / f"{sid}.lock").exists():
            raise SystemExit("session is locked")
        root = base / "trash" / str(sid)
        if not root.exists():
            raise SystemExit("not in trash")
        # remove directory tree
        for p in sorted(root.rglob("*"), reverse=True):
            if p.is_file() or p.is_symlink():
                p.unlink()
            else:
                try:
                    p.rmdir()
                except OSError:
                    pass
        root.rmdir()
        return
