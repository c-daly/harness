"""Inspect shared usage stop limits without running models or changing records."""

import argparse
from pathlib import Path

from harness.log import TornLogError, read_session
from harness.usage_budget import project_usage


def budget_snapshot(base, session_id, *, events=None):
    # Export passes its already frozen source prefix; do not reread a live root.
    if events is None:
        events = read_session(base, session_id, repair=False)
    state = project_usage(events)
    root = state.root_session_id or str(session_id)
    if state.root_session_id:
        if Path(root).name != root or root in (".", ".."):
            raise ValueError("invalid budget root session")
        events = read_session(base, root, repair=False)
        state = project_usage(events)
        if state.root_session_id:
            raise ValueError("budget ownership must point directly to the root")
    return {
        "root_session_id": root, "through_seq": events[-1].seq if events else 0, "configured": state.configured,
        "limits": state.limits.model_dump(), "input_tokens": state.input_tokens,
        "output_tokens": state.output_tokens, "estimated_cost_usd": str(state.cost_usd),
        "unknown_input_attempts": state.unknown_input, "unknown_output_attempts": state.unknown_output,
        "unknown_cost_attempts": state.unknown_cost, "pending_attempts": len(state.pending),
        "settled_attempts": len(state.finished), "last_stop": state.last_stop,
    }


def render_budget(base, session_id):
    data = budget_snapshot(base, session_id)
    if not data["configured"]:
        return "No recorded usage budget. Earlier work is untracked."
    lines = [f"Shared usage budget: {data['root_session_id']}"]
    for dimension, label, value in (("input", "Input tokens", data["input_tokens"]),
                                     ("output", "Output tokens", data["output_tokens"]),
                                     ("cost", "Estimated cost USD", data["estimated_cost_usd"])):
        key = "max_cost_usd" if dimension == "cost" else f"max_{dimension}_tokens"
        limit = data["limits"][key]
        unknown = data[f"unknown_{dimension}_attempts"]
        lines.append(f"  {label}: {value} reported; stop limit: {limit if limit is not None else 'unbounded'}; "
                     f"unknown attempts: {unknown}")
    lines.append(f"Attempts: {data['settled_attempts']} settled; {data['pending_attempts']} pending (usage unconfirmed)")
    if data["last_stop"]:
        lines.append(f"Last stop: {data['last_stop']}")
    lines.append("Limits stop new calls; running calls may cross them. Cost uses recorded token rates, not billing totals.")
    text = "\n".join(lines)
    return "".join(c for c in text if c in "\n\t" or ord(c) >= 32 and not 127 <= ord(c) <= 159)


def main(argv):
    parser = argparse.ArgumentParser(prog="harness budget", description=__doc__)
    parser.add_argument("session_id")
    parser.add_argument("--base-dir", type=Path, default=Path.home() / ".local/share/harness")
    args = parser.parse_args(argv)
    try:
        print(render_budget(args.base_dir, args.session_id))
    except (OSError, ValueError, TornLogError) as exc:
        parser.error(f"budget inspection failed ({type(exc).__name__})")
