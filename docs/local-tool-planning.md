# Local tool planning and candidate rejection

PR #14's real 4B profile failed all three memory-assisted artifact tasks. A fresh
diagnostic confirmed that the model proposed `read_file`, `write_file`, and
`memory_list` in one response, before observing any read results. Adding instructions
to wait for source results did not solve the problem: that diagnostic omitted the
output artifact. The model's final answer did not establish task completion.

This slice adds an explicit `parallel_tool_calls = false` context-profile option
and corresponding bounded inference request field. The LiteLLM adapter forwards
the request; Harness rejects multiple proposals locally before dispatching any of
them. The option is described by the upstream
[llama.cpp server contract](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md#post-v1chatcompletions-openai-compatible-chat-completions-api),
whose template-dependent support still needs runtime verification. An external
agent is refused under this profile because its internal batches are outside this
contract. Existing profiles retain their prior behavior.

**Final measured result: candidate rejected.** The
[eight-case paired report](handoffs/2026-09-06-core-agency/local-planning-evaluation.json)
records baseline **4/8** and candidate **2/8** passing. All four candidate cases
with normal memory returned multiple proposals despite the provider option;
the typed `ToolCallLimitExceeded` check rejected them before dispatch. Of the
four candidate cases without memory, one omitted the file and one wrote malformed
JSON. The core verdict is `failed` and adoption is `refused`. The option is a
working protocol bound, not a reliable replacement profile for this model.

For explicit use, add the following to an existing context TOML file:

```toml
parallel_tool_calls = false
```

TUI `/context` displays the bound. Normal session resume, `/clear`, and session-picker
resume retain it through the existing core context events. This is a limit on one
response, not a prohibition on multiple independent agents or a guarantee that
the model waits for all relevant evidence.

## A real core improvement experiment

`scripts/evaluate_local_planning.py` imports the prior failed qualification report
as a content-addressed blob and records a core failure observation and `Evidence`.
It records a `Candidate` for the context option and an `EvaluationPlan` **before**
running either side. The same core journal records run start, `ExperimentResult`,
and the terminal run status. The ordinary core verdict and adoption policy determine
the outcome; the script has no activation path.

The fixed suite has eight paired cases: the original harbor/3 regression and a
reserved orchard-λ/17 fixture, each with and without normal memory, each repeated
twice. Expected values live in synthetic source files, never answer hints in the
task prompt. Both arms use the same file/tool oracle and runtime profile; only the
parallel-call option differs. Pair order alternates. All cases are critical. The
candidate needs at least two improvements, no critical failure, at most a 2x total
latency ratio, and at most 60 seconds per trial. The whole experiment has a
300-second deadline. Trial timing includes MCP setup, stopped-runtime startup,
the file task, public semantic checks, and cleanup. This is a small feasibility
experiment, not broad held-out model or product qualification.

Use the Docker mounts, pinned assets and CPU/RAM/network limits from
[the qualification recipe](local-model-qualification.md). Replace its final
Python arguments with:

```sh
-B -m scripts.evaluate_local_planning \
  --memory-root "$qualification_memory_root" \
  --audit-dir /reports/planning-audit \
  --output /reports/planning-evaluation.json
```

The audit directory is a normal core session store containing only metadata,
plans, synthetic fixtures, and report blobs. Trial sessions containing private
memory are removed. Interruptions retain partial measurements and a cancelled or
timed-out result; unknown measurements cannot qualify adoption. A process crash
can leave a durable open evaluation for the existing resume/aborted-run handling;
the driver does not resume or automatically rerun experiments.

The basic qualification driver also accepts `--single-tool` and records tool
batches and privacy-safe failure categories. Invalid JSON artifacts remain failed
checks while memory transport and later semantic samples can still be measured.
The original oracle's default facts and exact object equality remain unchanged.

Automatic adoption stays disabled even if a future experiment passes: the core
policy returns `review_required`. Failed experiments return `refused`. No model
alias, prompt, provider routing rule, or memory/plugin implementation is changed.

Earlier paired runs also rejected the candidate; one passed 3/8 candidate cases
and another 4/8. The final run adds typed failure attribution and binds the current
driver/core hashes. Those variations reinforce that this model/profile is not
qualified by an occasional successful artifact. Prior reports and metadata-only
audit sessions remain under ignored `.local-runtime/reports/` on the measured machine.
The next experiment should test an appropriate tool-capable model/runtime and
artifact verification, preserving the source-fact oracle and the failed evidence.
