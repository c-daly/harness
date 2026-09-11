# Ensemble selection from recorded task evidence

**Status:** implemented; full Python 3.12/3.13 suites and packaging passed.
See the [implementation record](2026-09-06-core-agency-progress.md#checked-ensemble-selection)
for counts, development failures and remaining qualification boundaries.

PR55's live journey retained an incorrect local result alongside correct remote
and external results. Its fixture oracle caught the error, but ordinary ensemble
selection uses execution status and text votes. Extend the existing escalation
checks rather than adding another evaluator or a fixture-specific validator.

## Contract

- Freeze the actual calling task's objective and requirements before fan-out,
  using the existing ownership and before-run declaration checks.
- Give each expert and optional judge those requirements. Recheck each direct
  child's recorded output/tool evidence using existing provenance validation.
- With requirements, vote only among complete, untruncated participants whose
  every requirement passed. Preserve all results and grades in the report.
  Calculate disagreement before filtering so rejected alternatives remain visible.
- If no complete candidate passes, return incomplete without running a judge;
  retain candidate text with explicit unverified labels. Otherwise a judge
  receives only passing candidates and must independently pass the same checks.
  A failed judge is incomplete; it cannot bless a failed candidate or silently
  fall back to an expert. No automatic retries or extra model selection are added.
- Incomplete/interrupted siblings retain the existing partial-result semantics.
  A completed but failed check does not erase a passing sibling's useful answer.
  User-review requirements remain unverified; selection never accepts a task.
- Existing requirements cannot be disabled by model arguments. `require_checks`
  additionally refuses missing requirements. Expose it in the ensemble tool,
  Python API and configured ensemble-agent frontmatter, with strict validation.
- With no calling-task requirements and no explicit requirement for checks,
  retain legacy voting/synthesis. Panel and draft/refine are outside this change.
- Inspection/export/restart preserve grades and source references without
  executing agents or rechecking evidence. Interrupted verification is visible.

## Implementation and verification

1. Reproduce wrong-majority selection and an unchecked judge using real native
   children with scripted providers, then extend the shared coordinator gate.
2. Cover absent/invalid configuration, review-only requirements, wrong/corrupt
   evidence, completion order, partial work, cancellation and deadline cleanup.
3. Exercise both native tool and configured-agent paths, a native/external
   contract mix, and final terminal rendering plus read-only export/restart.
4. Reconcile the existing mixed-workflow driver's expected execution statuses
   with checked selection, preserving its independent artifact correctness gates
   and historical live reports. Do not claim another live model measurement.
5. Run focused tests during implementation, then full Python 3.12/3.13 suites,
   lint, packaging and installed-wheel smoke. Document actual outcomes and
   remaining M5 gaps, commit and open the next PR from merged main.
