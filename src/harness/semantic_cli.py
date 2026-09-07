"""Explicit semantic diagnostics and evaluation; normal input remains deterministic."""

import argparse
import asyncio
import json
from pathlib import Path

from harness.catalog import Catalog, UnknownAliasError
from harness.provider_litellm import CatalogProvider
from harness.semantic_evaluation import EvaluatorConfig, run_evaluation
from harness.semantic_assessment import ContextSelectionInput
from harness.semantics import MessagePrompt, SemanticLimits, read_semantics, render_semantics
from harness.types import ModelId, SessionId


def _read(path, limit):
    with path.open("rb") as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{path.name} exceeds the {limit}-byte file limit")
    return data


def main(argv):
    parser = argparse.ArgumentParser(prog="harness semantic", description=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--base-dir", type=Path, default=Path.home() / ".local/share/harness")
    actions = parser.add_subparsers(dest="action", required=True)
    inspect = actions.add_parser("inspect", parents=[common], help="Read saved shadow observations.")
    inspect.add_argument("session_id")
    classify = actions.add_parser("classify", parents=[common], help="Run one tool-free shadow judgment.")
    classify.add_argument("text")
    classify.add_argument("--model", required=True)
    classify.add_argument("--prompt", type=Path, help="Versioned MessagePrompt JSON artifact.")
    classify.add_argument("--timeout", type=float, default=5)
    context = actions.add_parser("context", parents=[common], help="Suggest from supplied context candidates.")
    context.add_argument("input", type=Path, help="Bounded ContextSelectionInput JSON; no retrieval occurs.")
    progress = actions.add_parser("progress", parents=[common], help="Assess recorded task evidence in a session.")
    progress.add_argument("session_id")
    progress.add_argument("--task-id", help="Defaults to the selected tracked task.")
    for action in (context, progress):
        action.add_argument("--model", required=True)
        action.add_argument("--timeout", type=float, default=5)
    evaluate = actions.add_parser("evaluate", parents=[common], help="Run an existing fixed evaluation plan.")
    evaluate.add_argument("session_id")
    evaluate.add_argument("plan_id")
    evaluate.add_argument("--incumbent", type=Path, required=True, help="Exact incumbent prompt artifact.")
    evaluate.add_argument("--config", type=Path, required=True, help="EvaluatorConfig JSON fixed by the plan.")
    for action in (classify, context, progress, evaluate):
        action.add_argument("--catalog", type=Path, default=Path.home() / ".config/harness/models.toml")
        action.add_argument("--allow", action="append", default=[])
    args = parser.parse_args(argv)
    if args.action == "inspect":
        print(render_semantics(read_semantics(args.base_dir, SessionId(args.session_id))))
        return
    try:
        config = EvaluatorConfig.model_validate_json(_read(args.config, 8192)) if args.action == "evaluate" else None
        model = config.model if config else ModelId(args.model)
        from harness.semantics import ASSESSMENT_LIMITS
        limits = config.limits if config else (SemanticLimits(timeout_seconds=args.timeout)
            if args.action == "classify" else SemanticLimits.model_validate(
                {**ASSESSMENT_LIMITS.model_dump(), "timeout_seconds": args.timeout}))
        prompt_data = _read(args.incumbent, 32768) if config else (
            _read(args.prompt, 32768) if getattr(args, "prompt", None) else None)
        context_input = ContextSelectionInput.model_validate_json(_read(args.input, 16384)) \
            if args.action == "context" else None
        if prompt_data is not None:
            MessagePrompt.model_validate_json(prompt_data)
        catalog = Catalog.load(args.catalog)
        if catalog.resolve(str(model)).execution_kind != "inference":
            parser.error("semantic operations require an inference model alias")
    except (OSError, ValueError, UnknownAliasError) as exc:
        parser.error(f"invalid semantic configuration ({type(exc).__name__})")

    from harness.cli import _apply_allow_flags, build_kernel
    from harness.permissions import PermissionEngine, default_engine
    engine = default_engine(project_dir=Path.cwd())
    if engine is None and args.allow:
        engine = PermissionEngine([])
    _apply_allow_flags(engine, args.allow)
    kernel = build_kernel(base_dir=args.base_dir, model=model, provider=CatalogProvider(catalog),
        permissions=engine, resume_session_id=SessionId(args.session_id)
        if args.action in {"evaluate", "progress"} else None)

    async def run():
        try:
            if not kernel.resumed:
                await kernel.loop.start()
            ref = kernel.session.blobs.put(prompt_data) if prompt_data is not None else None
            if config:
                result = await run_evaluation(kernel, args.plan_id, incumbent=ref, config=config)
                from harness.improvement import verdict
                report = json.loads(kernel.session.blobs.get(result.artifact))
                report["result_id"] = result.id
                report["verdict"] = verdict(kernel.improvements.state.plans[args.plan_id], result)
                print(json.dumps(report, indent=2))
                return report["verdict"] == "passed"
            if args.action == "context":
                observation = await kernel.semantics.select_context(context_input, model=model, limits=limits)
            elif args.action == "progress":
                observation = await kernel.semantics.assess_progress(
                    model=model, task_id=args.task_id, limits=limits)
            else:
                observation = await kernel.semantics.interpret(args.text, model=model, prompt=ref, limits=limits)
            print(f"Session: {kernel.session.id}")
            print(render_semantics([observation]))
            return observation.reason in {"classified", "assessed", "no_match", "uncertain"}
        finally:
            try:
                await kernel.resources.close(emit=kernel.session.append)
            finally:
                try:
                    await kernel.loop.end()
                finally:
                    kernel.session.close()

    try:
        successful = asyncio.run(run())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except (ValueError, KeyError) as exc:
        raise SystemExit(f"semantic operation refused: {exc}") from None
    if not successful:
        raise SystemExit(1)
