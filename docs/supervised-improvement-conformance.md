# Supervised improvement conformance

The implementation branch starts at committed main `a3b0eb9`. The earlier polish branch at `4bbe3b3` supplies comparison cases. The first independently evaluated change on this branch is `f3c26a1`. [Original design](https://github.com/c-daly/harness/blob/a3b0eb97580a2a5aa5e8eeb231c516b6af32277e/docs/superpowers/plans/2026-09-06-core-agency-roadmap.md#L155-L191)

| Comparison | Main or incumbent | Candidate | Interpretation |
| --- | --- | --- | --- |
| Six historical lifecycle checks, native paired evaluation | Main passed 3/6 | `f3c26a1` passed 6/6. The polish branch also passed 6/6. | The original scripts and gates were reused. One main failure stopped early on an absent inspection property. A separate probe reached the underlying publication defect. No source selection was made in this experiment. |
| Six scripted ordinary-turn experience checks | `f3c26a1` passed 3/6 | Working branch passed 4/6 on the original suite after fixing a catalog import. The first preflight encountered the import error. | In both transfer cases the native read succeeded. The scripted responder omitted tool-result blocks, and the scorer expected relative paths after the workspace guard had normalized them. |
| Same ordinary-turn checks with corrected fixture observations, before fresh-check enforcement | `f3c26a1` passed 3/6 | Intermediate working branch passed 6/6 | The substantive gates stayed fixed. Two cases required prior examples plus a fresh local read giving a different answer. This rerun measures integration under scripted inference. |
| Ordinary-turn checks with fresh-check enforcement, native paired runs | `f3c26a1` passed 3/6 on both suites | `0985735` passed 4/6 on both suites. Both native gates failed. | On the corrected suite, each transfer case made one current read and displayed the right new value alongside the source and a provisional label. The fixtures required only the exact answer. The four controls passed. Both arms passed 2/3 held-out cases. |
| Original four-model, five-task help comparison | Qwen 4B passed 2/5 in both conditions. Qwen 35B passed 2/5 with help disabled and 1/5 with help enabled. Both Codex models passed 5/5 in both conditions. | New live scores pending | All 40 prior appraisals were matched to the original traces and regraded. No reviewer ran in the 20 original help-enabled trials. |
| Full project regression and static checks | | `0985735` passed 2,637 tests, with 7 skipped and 6 warnings. Ruff passed. | One serial run took 481 seconds. The test environment included the project virtual environment on PATH for subprocess fixtures. |

The original native lifecycle suite digest is `f61a07b7dece2d96cd0f1a6c9a3c18c814b1513883b9dc6e3ea28641d6d1571e`. The new native result is `805c66cc-2bfa-468a-82ac-124188bd4c5a`. The vault evidence package is `2026-11-23-supervised-improvement-conformance-evidence.zip`. [Earlier four-model results](https://github.com/c-daly/vault/blob/b11fd9abd0820ec837da1bd1cda516c1ef7100c9/10-projects/harness/insights/2026-11-23-saoirse-model-comparison.md#L18-L37)

The final ordinary-turn result IDs are `ecbd6b3f-6804-4424-b6af-c573379081b3` on the original suite and `f80643de-6fbd-4e2a-8a1b-651cc0c3f9a0` on the corrected suite. Neither experiment activated its candidate. The diagnostic observations show the two current values were `Dock 58` and `Finch - standby`.

The vault artifact `2026-11-23-supervised-experience-ordinary.zip` includes both frozen suites, every preflight, case-level native measurements, the diagnostic outputs and the replay command. Its SHA-256 is `c92c4a4c9191dbb7d02ae0cc1fa02a04b448ccece7156b2a74e55d834bb6a34c`.

## Disposition of prior behavior

| Earlier behavior | Treatment on the main-based branch |
| --- | --- |
| Externally supplied reviewer score and opaque report could qualify a helper | Excluded the adaptive receipt importer and its ranking path. A checksum establishes file identity. Measured performance used to govern future routing needs native evaluation provenance. Explicit operator settings still control permissions and budgets. |
| English limitation phrases, counts of empty searches and a broad “attempted a tool” flag controlled consultation | Excluded the polish controller. Native semantics can select relevant recorded examples. Reviewers can inspect the current evidence and the original context receipt. A prior tool attempt alone cannot establish that the answer was supported. |
| Two fixed feedback prompts could produce findings but could not be selected or rolled back as their own supervised prompt functions | Excluded those profiles. Explicit failure and verified output mismatch produce native evidence. Retrospective relevance uses the existing supervised context-selection function. |
| Main exposed raw panel and helper nomination to the ordinary inference model | Retained the useful host-ownership boundary from polish without its consultation triggers. The normal headless and TUI entries reserve those choices for Harness. Explicit workflows and external agents retain their supported dispatch contracts. |
| The initial host guard also suppressed intentional delegation in the agent-swarm runner | Restored the runner through the explicit workflow entry. Eleven existing workflow tests now enter that path and retain their assertions about permission, accounting, child results and evidence. |
| Selection provenance checks added on polish | Ported the native checks. Prompt use, source launch, journal publication and replay check the evaluated version and supporting records. |
| An intermediate implementation used a project memory plugin to publish references to example sessions | The plugin can write to the project vault. Saoirse's ordinary experience belongs to Harness. The native journal holds the source evidence, and the vault records project decisions and qualification results. |

## Experience, supervision and remaining scope

A confirmed flawed response leaves a failure record. A retrospective looks for earlier experience that was eligible at the time and distinguishes a possible match from evidence that it reached the turn. The primary response, later correction, and later semantic assessment retain their own event references.

Saved situations, actions and outcomes can serve directly as worked examples. A provisional example can suggest a local check. Its old answer cannot settle a new question. If no eligible earlier example applies, a new description should preserve the useful action, its conditions and its limits as generally as the supporting facts allow. Automated semantic authorship of this wording remains open. The existing message and assessment prompt proposers revise the instructions for their own functions.

Reuse an experience wherever its supporting evidence and conditions apply, subject to the receiving context's read permissions. A general guarantee can be established directly by an authoritative specification or an invariant. For example, a failed read cannot establish a file's contents. A separate demonstration in another project is unnecessary for that conclusion. A convention such as which register governs an assignment needs evidence that the convention applies here. Held-out transfer cases can measure whether Saoirse makes that distinction in practice.

Saoirse may consult scoped examples by default. A fresh check must establish the answer in the current situation. The host supplies this advisory authority. Persistent changes to this behavior require supervised evaluation and adoption. Adopting a shadow prompt changes which prompt the semantic function uses.

At `0985735`, ordinary recall uses Saoirse's native journal. Sessions in linked worktrees can recover examples traced to the same local Git checkout. An unrelated clone with the same remote URL, another named agent, and a new launch in an unidentified workspace cannot import that raw history. Marking a turn as flawed first saves the failure and then attempts retrospective selection on a ready, permitted local model. The report names original candidates that the repeat assessment could not cover.

Resolving checkout identity during startup exposed a race in the terminal. A first task command could arrive before the journal had started. The delayed-start regression exercises immediate input after the MCP checklist and verifies that task creation follows the native session-start record.

An observed read or search can support a provisional interpretation. A native output check can establish the exact answer it covers. The remaining design question for free-form answers is how Harness should establish that a conclusion follows from the source in the absence of a predeclared answer. The two exact-output misses above show the practical effect. A general wording of an experience also needs a way to carry its conditions and evidence within Saoirse's private memory, at the scope where they are readable.

Wider discovery of appropriately scoped claims in Saoirse's own memory, learned consultation and helper choice, and a new live four-model comparison remain outstanding. The four-model runner from the polish branch expects a CLI event format, subscription transport and adaptive measurement profile unavailable on committed main. Reinstating the old scoring authority would change the experiment.
