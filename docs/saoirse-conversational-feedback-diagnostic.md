# What Saoirse learns from a conversational correction

## Ordinary replies after integration

November 23, 2026. Ten ordinary CLI follow-ups were repeated on independent copies of the same original sessions. Six used Codex through the subscription SDK and four used Qwen directly. Qwen3 4B supplied local shadow judgments for all three primary roles when the session had capacity.

| Measure | Before · `df8822e` | After · `bd3f282` |
| --- | ---: | ---: |
| Ordinary replies with a semantic observation linked to the previous answer | 0/10 | 10/10 |
| Classifications | 0 | 9, of which 8 matched the expected kind |
| Recorded abstentions | 0 | 1 because the primary used its session call allowance |
| Advisory correction records | 0 | 3, including 1 false positive |
| Confirmed failures or task acceptances inferred from a reply | 0 | 0 |

| Follow-up | Observed behavior at `bd3f282` |
| --- | --- |
| Direct disagreement after the two unsupported local dispatch answers | Both classified as corrections. Qwen still failed to open the decisive files. |
| Request about the seventh registration after Luna's omission | Luna used the remaining eight foreground calls to repair its answer. The classifier recorded a budget abstention linked to the original exchange. |
| Identical request after Astra's already complete answer | Qwen classified the artificial recheck as a correction. This was a false advisory finding. |
| Three acknowledgements and three changes of subject | All six classifications matched their expected kind. No acknowledgement accepted a task. |
| Separate explicit classifier calls on the two original badge exchanges with a fresh budget | Qwen labeled the Luna follow-up `new_request` and the Astra follow-up `correction`. It missed the distinction in both directions. |

The median completed classification took 373 ms. The receipt `2026-11-23-saoirse-ordinary-reply-semantics.json` carries original and resumed journal digests, source event numbers, observed kinds, abstentions and per-turn evidence. Its SHA-256 is `68e71ce0404e59d7d2a442eb9cb61c9dc8c8c3f2df08b3d7f2c68b68e25686c8`. These are one-attempt observations per condition.

## Supervised comparison of the reply prompt

The false advisory finding on Astra's recheck supplied the evidence for one prompt candidate. The candidate asks the classifier to identify what the original answer committed to and what the follow-up disputes. The prompt and its independent 12-case suite were frozen before scoring. The suite included the required stop and ambiguous-thanks controls and paired exchanges about preserving fruit and producing audio. Each pair kept the request and follow-up identical and varied the completeness of the answer.

| Native paired evaluation on `bd3f282` | Shipped prompt | Candidate |
| --- | ---: | ---: |
| All cases | 10/12 | 11/12 |
| Held-out cases | 4/5 | 5/5 |
| Critical controls | 2/2 | 2/2 |

The candidate gained one held-out correction and introduced zero regressions. Both prompts mislabeled one request for confirmation after a complete answer. The predeclared quality and latency gates passed. The incumbent received the first cold call, which limits any speed comparison. Native result `96def3d6-5d1d-4122-ae7f-a3ce790cd8fb` and the exact candidate, suite, measurements and gates are in the vault archive `2026-11-23-saoirse-ordinary-reply-prompt-evaluation.zip`, SHA-256 `4ca26027db50ce3794e769592cd1bd668fd70450364a3a6bab3da717973f9805`.

| Final runtime check | Measured result |
| --- | --- |
| Repeat the frozen comparison on `648b80d` after foreground delivery and prompt-lineage changes | The shipped prompt scored 10/12 and the candidate scored 11/12 again. The candidate passed all five held-out cases and both critical controls. Result `0ec77c2c-b736-4e05-bfaa-9c2188336350`. |
| Explicitly adopt the passing prompt in a disposable copy and classify the two historical exchanges | The native selected prompt correctly identified Luna's omission. It also labeled Astra's redundant recheck a correction. Both calls recorded the adopted prompt. |
| Roll back the disposable selection | Native rollback restored the exact incumbent bytes. The deployed configuration and original evaluation session received no adoption. |
| Deliver a real CLI answer with review pending | Stdout arrived at 2.681 s with a finished foreground event and zero shadow observations. At exit (3.467 s), the journal contained the linked correction. |

The final qualification archive is `2026-11-23-saoirse-supervised-reply-qualification.zip`, SHA-256 `40f8a4dede9607d355284341976188f06eaf37f16d6ccd778d6cf425232af821`. It contains the original artifact bytes, both native evaluations, the selection and rollback records, and the CLI timing receipt. A subsequent fix at `38d0fbe` invalidates cached prompt lineage before rechecking publication. Its native evaluator fingerprint matches the final paired run.

On `38d0fbe` the full Python 3.13 suite passed 2,651 tests with seven skips and six deprecation warnings. All 72 focused Python 3.12 checks and Ruff passed. The installed wheel restored the recorded prompt from a copy of the genuine evaluation and refused selection from a copy missing the native completion event.

## Original diagnostic

Ten automated follow-ups on `df8822e` used Qwen3 4B, GPT-6 Luna and GPT-6.2 Astra. **One of three flawed answers was repaired with fresh evidence. Harness saved all ten replies in the conversation. None produced a correction or failure record linked to the previous answer.**

| Original answer | Follow-up | What Saoirse did | What the evidence supports |
| --- | --- | --- | --- |
| Qwen 4B said it lacked dispatch records and guessed `HOLD` for a pending crate. | Corrected the claim that records were unavailable and requested another check. | Tried two filename patterns, found nothing and repeated `HOLD` with an explanation. | The decision happened to match. The current instructions and scan remained unread. |
| Qwen 4B gave `HOLD` for a cleared crate. | Same correction. | Tried a filename pattern and a text search, found nothing and returned `HOLD`. | The authorized destination in the fixture was `FINCH`. |
| Luna explained six printed badges but missed the late seventh registration. | Asked it to check the cutoff and procedure for the seventh person. | Retrieved all three deciding source lines and prescribed one supplemental badge for Pine. | Verified repair. Eight primary model calls and seven tool attempts, including one denied by the workspace guard. |
| Astra already had the correct badge decision and the deciding source lines. | Same wording as Luna. | Explained again that Pine registered at 09:18, after the 09:15 cutoff, and needed a supplemental badge. | Correct answer retained using prior evidence. No fresh read in response to the recheck. |

| Control | Result | What Harness recorded |
| --- | --- | --- |
| “Thanks.” after the original answer, once per model | Both Codex models acknowledged it. Qwen repeated `HOLD`. | No task acceptance in any of the three. |
| A new question requesting only the number of sheets for twelve badges at four per sheet | All three got the arithmetic right. Both Codex models returned exactly `3`. Qwen included the calculation. | A new conversation turn for each. No failure attributed to the prior answer. |
| Explicitly classify the four distinct follow-up texts with Qwen 4B | The classifier labeled the direct dispatch correction `correction`, “Thanks.” `acknowledgement`, and both the arithmetic and badge recheck `question`. | Four shadow observations. The badge wording followed an omission in Luna's conversation and a correct answer in Astra's. The classifier received only the follow-up text. |
| Explicitly record the verified cleared-dispatch failure on a separate copy | Harness attached failure evidence to the original turn at event 86 and recorded a retrospective with zero eligible prior examples. | One confirmed failure and one `no_match` assessment. The empty candidate set required no model inference. |

## What the comparison establishes

The earlier seven rubric misses were scored before anyone replied. That measurement left conversational feedback untested. In these ten continuations, each journal gained a user message and a generic run outcome with acceptance unverified. The native improvement projection showed no new linked correction or failure. No ordinary follow-up invoked message classification, a failure review or a helper.

For reply interpretation, shadow mode gives the classifier permission to make and record a judgment for evaluation. A bounded inference receives the supplied reply text. Harness validates its label and journals the outcome or the reason it abstained. For example, a recorded `correction` means the reply appears to challenge something. Establishing which claim was wrong requires separate evidence. The four explicit classification controls produced shadow observations. The ten ordinary submissions produced none.

The roadmap requires Harness to interpret submitted messages in shadow mode and observe explicit corrections. Ordinary reply interpretation and linking a correction to its source were missing at the tested revision, `df8822e`. The open design question concerns what evidence establishes a free-form failure when the reply is ambiguous. [Submitted-message interpretation and evaluation gates](https://github.com/c-daly/harness/blob/a3b0eb97580a2a5aa5e8eeb231c516b6af32277e/docs/superpowers/plans/2026-09-06-core-agency-roadmap.md#L340-L373)

Luna's answer left the seventh badge unresolved, which gave the operator a reason to follow up. Astra had already resolved it. Repeating the wording after Astra's answer was an artificial control for false failure attribution. Count Astra's first turn as success without intervention when assessing usability. These trials exercised immediate repair and produced no feedback-derived lesson for a transfer test. The earlier build had no matched conversational-feedback arm, so this result cannot establish a performance gain over that build.

## Repeating the diagnostic

1. Preserve the original answer, workspace file digests and session journal. Use separate byte-identical copies of the session for each follow-up. The original sessions used `8d21c6a`, and the continuations used `df8822e`. Only documentation differed between the revisions.
2. Resume with `harness --resume SESSION_ID`, the original workspace, model and tool permissions. In the usability trial, ask about what the actual answer left unresolved. A complete, grounded answer can end the conversation. For the separate semantic control, submit identical wording after a flawed answer and after a correct one. Run direct disagreement, acknowledgement and an unrelated new request on independent copies. Record each exact utterance and its purpose.
3. Score the original answer, the new conclusion, the requested format and the decisive source lines read on each turn. Inspect new journal events and the before-and-after improvement projection for linked evidence, assessment and acceptance. A generic completed run can coexist with an incorrect answer.
4. Run explicit classification and explicit confirmed-failure controls on separate copies. Keep those results separate from ordinary conversation. Compare model or build performance only on equivalent fixtures and conversation histories.

The vault receipt `2026-11-23-saoirse-conversational-feedback.json` contains the prompts, responses, tool receipts, journal digests, event counts, projected evidence and local worker measurements. SHA-256 `afba775e11a642264e53ab6d1aeae1d743fc6ff3f414a9c40f8b80743892aae9`. All ten native runs finished. The outer process exit code was unavailable for Luna's corrective turn. The original journals and workspace contents retained their digests. The local worker peaked at 705 MiB of host memory under a 768 MiB cap and was stopped after the controls.
