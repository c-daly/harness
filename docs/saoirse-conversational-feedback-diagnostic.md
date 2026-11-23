# What Saoirse learns from a conversational correction

November 23, 2026. Ten automated follow-ups on the ordinary Harness CLI, using Qwen3 4B, GPT-6 Luna and GPT-6.2 Astra. **One of three flawed answers was repaired with fresh evidence. Harness saved all ten replies in the conversation. None produced a correction or failure record linked to the previous answer.**

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

The roadmap requires Harness to interpret submitted messages in shadow mode and observe explicit corrections. Ordinary reply interpretation and linking a correction to the earlier turn are missing implementation work. The open design question concerns what evidence establishes a free-form failure when the reply is ambiguous. [Submitted-message interpretation and evaluation gates](https://github.com/c-daly/harness/blob/a3b0eb97580a2a5aa5e8eeb231c516b6af32277e/docs/superpowers/plans/2026-09-06-core-agency-roadmap.md#L340-L373)

Luna's answer left the seventh badge unresolved, which gave the operator a reason to follow up. Astra had already resolved it. Repeating the wording after Astra's answer was an artificial control for false failure attribution. Count Astra's first turn as success without intervention when assessing usability. These trials exercised immediate repair and produced no feedback-derived lesson for a transfer test. The earlier build had no matched conversational-feedback arm, so this result cannot establish a performance gain over that build.

## Repeating the diagnostic

1. Preserve the original answer, workspace file digests and session journal. Use separate byte-identical copies of the session for each follow-up. The original sessions used `8d21c6a`, and the continuations used `df8822e`. Only documentation differed between the revisions.
2. Resume with `harness --resume SESSION_ID`, the original workspace, model and tool permissions. In the usability trial, ask about what the actual answer left unresolved. A complete, grounded answer can end the conversation. For the separate semantic control, submit identical wording after a flawed answer and after a correct one. Run direct disagreement, acknowledgement and an unrelated new request on independent copies. Record each exact utterance and its purpose.
3. Score the original answer, the new conclusion, the requested format and the decisive source lines read on each turn. Inspect new journal events and the before-and-after improvement projection for linked evidence, assessment and acceptance. A generic completed run can coexist with an incorrect answer.
4. Run explicit classification and explicit confirmed-failure controls on separate copies. Keep those results separate from ordinary conversation. Compare model or build performance only on equivalent fixtures and conversation histories.

The vault receipt `2026-11-23-saoirse-conversational-feedback.json` contains the prompts, responses, tool receipts, journal digests, event counts, projected evidence and local worker measurements. SHA-256 `afba775e11a642264e53ab6d1aeae1d743fc6ff3f414a9c40f8b80743892aae9`. All ten native runs finished. The outer process exit code was unavailable for Luna's corrective turn. The original journals and workspace contents retained their digests. The local worker peaked at 705 MiB of host memory under a 768 MiB cap and was stopped after the controls.
