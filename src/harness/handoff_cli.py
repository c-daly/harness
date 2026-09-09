"""Operator-owned reconciliation and continuation; never registered as model tools."""

import argparse
import asyncio
from pathlib import Path
from types import SimpleNamespace

from harness.blobs import BlobStore
from harness.handoff import HandoffSpec, render_handoff
from harness.types import ModelId, SessionId


RECONCILE = "This handoff record was used. Use /handoff inspect, then record a new handoff before another attempt."


class HandoffAttemptError(RuntimeError):
    """The current command started an attempt; its effects require reconciliation."""
    def __init__(self, status, reason):
        self.status = status
        super().__init__(f"{reason}\n{RECONCILE}")


def failure_message(exc):
    from harness.improvement_cli import refusal_message
    status = exc.status if isinstance(exc, HandoffAttemptError) else "refused"
    return f"Handoff {status}: {refusal_message(exc)}"


async def perform(kernel, words, *, on_progress=None):
    if len(words) == 2 and words[0] == "record":
        from harness.semantic_cli import _read
        spec = HandoffSpec.model_validate_json(_read(Path(words[1]), 1024 * 1024))
        record = kernel.handoffs.record(spec)
        held = any(r.status == "uncertain" for r in spec.resolutions)
        return f"Handoff {record.id}: {'held: uncertain effects remain' if held else 'recorded; review before running'}."
    if len(words) == 2 and words[0] == "run":
        from harness.improvement_cli import refusal_message
        from harness.log import read_session
        before = read_session(kernel.session.base, kernel.session.id, repair=False)
        after_seq = before[-1].seq if before else 0
        try:
            result = await kernel.handoffs.run(words[1], on_progress=on_progress)
        except Exception as exc:
            later = [e.event for e in read_session(kernel.session.base, kernel.session.id, repair=False)
                     if e.seq > after_seq]
            started = next((e for e in later if e.type == "agent_run_started" and e.handoff_id == words[1]), None)
            if started is None:
                raise  # A preflight refusal did not consume this record.
            finished = next((e.result for e in later if e.type == "agent_run_finished" and e.result.run_id == started.run_id), None)
            status = finished.status if finished is not None else "interrupted"
            raise HandoffAttemptError(status, refusal_message(exc)) from exc
        return (f"Handoff {result.status}: task {result.task_id[:8]}; acceptance remains unverified.\n" +
                result.read_text(kernel.session.blobs) + (f"\n{RECONCILE}" if result.status != "completed" else ""))
    raise ValueError("use /handoff inspect, show ID, record FILE.json, or run ID")


def main(argv):
    parser = argparse.ArgumentParser(prog="harness handoff", description=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--base-dir", type=Path, default=Path.home() / ".local/share/harness")
    actions = parser.add_subparsers(dest="action", required=True)
    inspect = actions.add_parser("inspect", parents=[common], help="Read effects without opening a provider.")
    inspect.add_argument("session_id")
    inspect.add_argument("--json", action="store_true", help="Exact snapshot JSON for preparing a reconciliation.")
    show = actions.add_parser("show", parents=[common])
    show.add_argument("session_id")
    show.add_argument("record_id")
    record = actions.add_parser("record", parents=[common])
    record.add_argument("session_id")
    record.add_argument("specification")
    run = actions.add_parser("run", parents=[common])
    run.add_argument("session_id")
    run.add_argument("record_id")
    for action in (record, run):
        action.add_argument("--model", required=True)
        action.add_argument("--catalog", type=Path, default=Path.home() / ".config/harness/models.toml")
        action.add_argument("--native-tools", action="store_true")
        action.add_argument("--workspace", type=Path, default=Path.cwd())
        action.add_argument("--allow", action="append", default=[])
    args = parser.parse_args(argv)
    session_id = SessionId(args.session_id)
    try:
        if args.action in {"inspect", "show"}:
            root = args.base_dir / "sessions" / str(session_id) / "blobs"
            if not root.is_dir():
                raise ValueError("session blob store is unavailable")
            session = SimpleNamespace(base=args.base_dir, id=session_id, blobs=BlobStore(root))
            print(render_handoff(session, getattr(args, "record_id", None), raw=getattr(args, "json", False)))
            return
        from harness.catalog import Catalog
        from harness.cli import _apply_allow_flags, build_kernel
        from harness.permissions import PermissionEngine, default_engine
        from harness.provider_litellm import CatalogProvider
        catalog = Catalog.load(args.catalog)
        engine = default_engine(project_dir=args.workspace)
        if engine is None and args.allow:
            engine = PermissionEngine([])
        _apply_allow_flags(engine, args.allow)
        kernel = build_kernel(base_dir=args.base_dir, model=ModelId(args.model), provider=CatalogProvider(catalog),
            permissions=engine, resume_session_id=session_id, native_tools=args.native_tools,
            workspace_root=args.workspace, model_pinned=True)

        async def execute():
            try:
                print(await perform(kernel, [args.action, args.specification if args.action == "record" else args.record_id]))
            finally:
                try:
                    await kernel.resources.close(emit=kernel.session.append)
                finally:
                    try:
                        await kernel.loop.end()
                    finally:
                        kernel.session.close()
        asyncio.run(execute())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as exc:
        raise SystemExit(failure_message(exc)) from None
