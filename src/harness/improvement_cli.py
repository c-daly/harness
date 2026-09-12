"""Explicit operator controls for the core prompt-improvement loop."""

import argparse
import asyncio
from pathlib import Path

from harness.improvement import verdict
from harness.prompt_improvement import PromptExperiment

FUNCTIONS = {"message": "message_kind", "context": "context_selection", "progress": "progress_assessment"}


def _function(value):
    if value not in FUNCTIONS:
        raise ValueError("prompt function must be message, context, or progress")
    return FUNCTIONS[value]


def refusal_message(exc):
    from harness.errors import ProviderError
    # Provider exception bodies can contain credentials or private response text.
    return type(exc).__name__ if isinstance(exc, ProviderError) else str(exc)


async def perform(kernel, words):
    """Shared CLI/TUI actions. These are not registered as model tools."""
    if words and words[0] in {"source-prepare", "source-evaluate"}:
        from harness.source_improvement_cli import perform as source_perform
        return await source_perform(kernel.improvements, [words[0].removeprefix("source-"), *words[1:]])
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
                "The selected prompt has not changed. " +
                ("Explicit shadow adoption is available with adopt RESULT context|progress."
                 if decision == "passed" else "Candidate held."))
    if len(words) in (1, 2) and words[0] == "propose":
        label = words[1] if len(words) == 2 else "message"
        candidate = await service.propose(model=model, function=_function(label))
        return (f"Candidate {candidate.id}: {label} prompt {candidate.artifact.sha256[:12]}.\n"
                f"Review with /improvements show {candidate.id} (CLI: harness improvements SESSION --show ID).\n"
                "Evaluation required; the selected prompt has not changed.")
    if len(words) == 3 and words[0] == "evaluate":
        from harness.semantic_cli import _read
        from pydantic import TypeAdapter
        from harness.assessment_evaluation import AssessmentPromptExperiment
        experiment = TypeAdapter(PromptExperiment | AssessmentPromptExperiment).validate_json(
            _read(Path(words[2]), 1024 * 1024))
        if experiment.configuration.model != model:
            raise ValueError("select the experiment's model before evaluation")
        result = await service.evaluate(words[1], experiment=experiment)
        decision = verdict(kernel.improvements.state.plans[result.plan_id], result)
        return (f"Evaluation {result.id}: {decision} ({result.completion}).\n"
                + ("Explicit adoption is available; the selected prompt has not changed."
                   if decision == "passed" else "Candidate held; the selected prompt has not changed."))
    if len(words) in (2, 3) and words[0] == "adopt":
        label = words[2] if len(words) == 3 else "message"
        change = service.adopt(words[1], model=model, function=_function(label))
        return f"Adopted {label} prompt {change.prompt.sha256[:12]}; change {change.id}. Shadow mode."
    if len(words) in (1, 2) and words[0] == "rollback":
        label = words[1] if len(words) == 2 else "message"
        change = service.rollback(model=model, function=_function(label))
        return f"Restored {label} prompt {change.prompt.sha256[:12]}; change {change.id}. Shadow mode."
    raise ValueError("use propose [message|context|progress], evaluate CANDIDATE EXPERIMENT.json, "
                     "compare ASSESSMENT.json, adopt RESULT [message|context|progress], rollback [message|context|progress], "
                     "source-prepare SPEC.json, or source-evaluate PLAN_ID")


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
    propose = actions.add_parser("propose")
    evaluate = actions.add_parser("evaluate")
    evaluate.add_argument("candidate_id")
    evaluate.add_argument("experiment")
    compare = actions.add_parser("compare", help="Compare an operator-authored assessment prompt with its current incumbent.")
    compare.add_argument("experiment")
    adopt = actions.add_parser("adopt")
    adopt.add_argument("result_id")
    rollback = actions.add_parser("rollback")
    for command in (propose, adopt, rollback):
        command.add_argument("--function", choices=tuple(FUNCTIONS), default="message")
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
    if args.action in ("propose", "adopt", "rollback"):
        words += [args.function]

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
