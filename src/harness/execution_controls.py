"""Operator configuration of core execution limits; never a model tool."""

from dataclasses import asdict, replace

from harness.execution import ExecutionLimits


def parse_overrides(words: list[str]) -> dict:
    """Parse NAME VALUE pairs using the same names as the CLI flags."""
    if not words or len(words) % 2:
        raise ValueError("Usage: /execution [task-timeout-seconds 1800 [inference-timeout-seconds 300 ...]]")
    defaults = asdict(ExecutionLimits())
    result = {}
    for name, raw in zip(words[::2], words[1::2]):
        key = name.replace("-", "_")
        if key not in defaults or key in result:
            raise ValueError(f"unknown or repeated execution limit: {name}")
        result[key] = float(raw) if key.endswith("_seconds") else int(raw)
    replace(ExecutionLimits(), **result)  # validate before any mutation
    return result


def record_execution_limits(dispatcher) -> None:
    from harness.events import ExecutionConfigured
    dispatcher.session.append(ExecutionConfigured(limits=asdict(dispatcher.scope.budget.limits)))


def configure_execution(kernel, overrides: dict) -> None:
    """Durably change defaults at an idle operator boundary, without resetting counts."""
    from harness.events import ExecutionConfigured
    scope = kernel.loop.dispatcher.scope
    if (kernel.loop._task_active or kernel.controller.active is not None or kernel.controller.pending
            or scope.budget.busy or scope.depth):
        raise ValueError("execution settings can only change while the root session is idle")
    limits = replace(scope.budget.limits, **overrides)
    kernel.session.append(ExecutionConfigured(limits=asdict(limits)))
    scope.budget.limits = limits


def render_execution(scope) -> str:
    from harness.run_budgets import render_run_budgets
    limits = scope.budget.limits
    lines = ["Execution limits (seconds are elapsed time budgets, not hang detection):"]
    for name, value in asdict(limits).items():
        lines.append(f"  {name.replace('_', '-')}: {value:g}")
    lines += [
        f"Current counts: {scope.budget.model_calls} model calls; {scope.budget.tool_calls} tool calls; "
        f"{scope.budget.children} descendants; {scope.budget.active_children} active children; "
        f"{scope.budget.active_coordinators} active coordinators.",
        "Task budgets cover native turns and external agents, including waiting; cleanup can finish after expiry.",
        "Inference budgets cap native conversational model requests; external agents use the task budget.",
        "Delegated work shares these limits; explicit task caps and enclosing deadlines can stop it sooner.",
        "Settings persist on resume. Call/child counters are per process lifetime; token/cost accounting is durable.",
        "Change idle settings with /execution task-timeout-seconds 1800 (applies to subsequent tasks).",
    ]
    return "\n".join([*lines, render_run_budgets(scope)])
