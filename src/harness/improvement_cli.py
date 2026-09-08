"""Explicit operator controls for the core prompt-improvement loop."""

import argparse
import asyncio
from pathlib import Path

from harness.improvement import verdict
from harness.prompt_improvement import PromptExperiment


def refusal_message(exc):
    from harness.errors import ProviderError
    # Provider exception bodies can contain credentials or private response text.
    return type(exc).__name__ if isinstance(exc, ProviderError) else str(exc)


async def perform(kernel, words):
    """Shared CLI/TUI actions. These are not registered as model tools."""
    service, model = kernel.improvement_service, kernel.loop.model
    if len(words) == 2 and words[0] == "compare":
        from harness.assessment_evaluation import AssessmentExperiment
        from harness.semantic_cli import _read
        experiment = AssessmentExperiment.model_validate_json(_read(Path(words[1]), 1024 * 1024))
        if experiment.configuration.model != model:
            raise ValueError("select the experiment's model before evaluation")
        result = await service.compare_assessment(experiment)
        decision = verdict(kernel.improvements.state.plans[result.plan_id], result)
        return (f"Assessment evaluation {result.id}: {decision} ({result.completion}).\n"
                "Comparison only; assessment adoption is unavailable. Builtin prompts remain active.")
    if words == ["propose"]:
        candidate = await service.propose(model=model)
        return (f"Candidate {candidate.id}: message prompt {candidate.artifact.sha256[:12]}.\n"
                f"Review with /improvements show {candidate.id} (CLI: harness improvements SESSION --show ID).\n"
                "Evaluation required; the selected prompt has not changed.")
    if len(words) == 3 and words[0] == "evaluate":
        from harness.semantic_cli import _read
        experiment = PromptExperiment.model_validate_json(_read(Path(words[2]), 1024 * 1024))
        if experiment.configuration.model != model:
            raise ValueError("select the experiment's model before evaluation")
        result = await service.evaluate(words[1], experiment=experiment)
        decision = verdict(kernel.improvements.state.plans[result.plan_id], result)
        return (f"Evaluation {result.id}: {decision} ({result.completion}).\n"
                + ("Explicit adoption is available; the selected prompt has not changed."
                   if decision == "passed" else "Candidate held; the selected prompt has not changed."))
    if len(words) == 2 and words[0] == "adopt":
        change = service.adopt(words[1], model=model)
        return f"Adopted message prompt {change.prompt.sha256[:12]}; change {change.id}. Shadow mode."
    if words == ["rollback"]:
        change = service.rollback(model=model)
        return f"Restored message prompt {change.prompt.sha256[:12]}; change {change.id}. Shadow mode."
    raise ValueError("use propose, evaluate CANDIDATE EXPERIMENT.json, compare ASSESSMENT.json, adopt RESULT, or rollback")


def main(argv):
    from harness.blobs import BlobIntegrityError, MissingBlobError
    from harness.catalog import Catalog, UnknownAliasError
    from harness.cli import _apply_allow_flags, build_kernel
    from harness.dispatcher import ModelDispatchBlocked
    from harness.errors import ProviderError
    from harness.execution import BudgetExceeded
    from harness.permissions import PermissionEngine, default_engine
    from harness.provider_litellm import CatalogProvider
    from harness.types import ModelId, SessionId

    parser = argparse.ArgumentParser(prog="harness improve", description=__doc__)
    parser.add_argument("--base-dir", type=Path, default=Path.home() / ".local/share/harness")
    parser.add_argument("--catalog", type=Path, default=Path.home() / ".config/harness/models.toml")
    parser.add_argument("--model", required=True)
    parser.add_argument("--allow", action="append", default=[])
    parser.add_argument("session_id")
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("propose")
    evaluate = actions.add_parser("evaluate")
    evaluate.add_argument("candidate_id")
    evaluate.add_argument("experiment")
    compare = actions.add_parser("compare", help="Compare an operator-authored assessment prompt with its builtin.")
    compare.add_argument("experiment")
    adopt = actions.add_parser("adopt")
    adopt.add_argument("result_id")
    actions.add_parser("rollback")
    args = parser.parse_args(argv)
    try:
        catalog = Catalog.load(args.catalog)
        if catalog.resolve(args.model).execution_kind != "inference":
            parser.error("prompt improvement requires an inference model alias")
    except (OSError, ValueError, UnknownAliasError) as exc:
        parser.error(f"invalid improvement catalog ({type(exc).__name__})")
    engine = default_engine(project_dir=Path.cwd())
    if engine is None and args.allow:
        engine = PermissionEngine([])
    _apply_allow_flags(engine, args.allow)
    kernel = build_kernel(base_dir=args.base_dir, model=ModelId(args.model), provider=CatalogProvider(catalog),
        permissions=engine, resume_session_id=SessionId(args.session_id))
    words = [args.action]
    if args.action == "evaluate":
        words += [args.candidate_id, args.experiment]
    elif args.action == "compare":
        words += [args.experiment]
    elif args.action == "adopt":
        words += [args.result_id]

    async def run():
        try:
            print(await perform(kernel, words))
        finally:
            try:
                await kernel.resources.close(emit=kernel.session.append)
            finally:
                try:
                    await kernel.loop.end()
                finally:
                    kernel.session.close()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except (ValueError, KeyError, OSError, BlobIntegrityError, MissingBlobError,
            ModelDispatchBlocked, BudgetExceeded, ProviderError) as exc:
        raise SystemExit(f"improvement refused: {refusal_message(exc)}") from None
