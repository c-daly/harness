"""Explicit source experiment controls. Checks execute with the operator's OS authority."""

import argparse
import asyncio
import os
import stat
from pathlib import Path

from harness.improvement import verdict
from harness.source_improvement import SourceProposal, prepare_source, run_source_evaluation


async def perform(journal, words):
    if len(words) == 2 and words[0] == "prepare":
        path = Path(words[1]).absolute()
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError("source proposal paths must not follow symlinks")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError("source proposal must be a regular JSON file")
            data = source.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError("source proposal exceeds 1 MiB")
        proposal = SourceProposal.model_validate_json(data)
        plan = await prepare_source(journal, proposal)
        return (f"Source candidate {plan.candidate_id}; fixed plan {plan.id}.\n"
                f"Review: /improvements show {plan.candidate_id} and /improvements show {plan.id}.\n"
                f"Run: /improvements source-evaluate {plan.id} "
                f"(CLI: harness improve-source SESSION evaluate {plan.id}).\n"
                "Checks execute with your OS authority in fresh directories. No code is activated.")
    if len(words) == 2 and words[0] == "evaluate":
        result = await run_source_evaluation(journal, words[1])
        decision = verdict(journal.state.plans[result.plan_id], result)
        return (f"Source evaluation {result.id}: {decision} ({result.completion}).\n"
                "Recorded checks do not qualify code activation. Review the retained patch and results.")
    raise ValueError("use source-prepare SPEC.json or source-evaluate PLAN_ID")


def main(argv):
    from harness.blobs import BlobIntegrityError, MissingBlobError
    from harness.improvement_journal import ImprovementJournal
    from harness.log import SessionLockedError
    from harness.resume import resume_session
    from harness.types import SessionId

    parser = argparse.ArgumentParser(prog="harness improve-source", description=__doc__)
    parser.add_argument("--base-dir", type=Path, default=Path.home() / ".local/share/harness")
    parser.add_argument("session_id")
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("prepare", help="Freeze two committed revisions and operator-authored checks.").add_argument("spec")
    actions.add_parser("evaluate", help="Execute a recorded plan in fresh source directories.").add_argument("plan_id")
    args = parser.parse_args(argv)

    async def run():
        session, _ = resume_session(args.base_dir, SessionId(args.session_id))
        with session:
            print(await perform(ImprovementJournal(session), [args.action,
                        args.spec if args.action == "prepare" else args.plan_id]))

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except SessionLockedError:
        raise SystemExit("source improvement refused: session is open in another process; "
                         "use its /improvements controls or close it before the CLI operation") from None
    except (ValueError, KeyError, OSError, BlobIntegrityError, MissingBlobError) as exc:
        raise SystemExit(f"source improvement refused: {exc}") from None
