# Source promotion demonstration

[report.json](report.json) repeats the known PR56 checked-ensemble comparison
using this increment's source evaluator, then explicitly adopts the corrected
snapshot into an isolated improvement session. The real `harness run-source`
command starts that snapshot's `harness.cli:main` with `--help`, after which an
explicit rollback and second launch start the incumbent snapshot successfully.
Neither the working installation nor a live user session is changed.

The fixed checks use deterministic fake providers: both versions retain the
checked-selection behavior; only the corrected version preserves passing work
after native judge failure. These are operator-authored probes of a known
historical defect. The held-out partition is declared, not secret or an
independent model trial. CLI startup is not a terminal interaction, plugin
integration, dependency migration or daily-use qualification.

The compact report retains the paired measurements, scripts, exact snapshots,
adoption/rollback records, startup outcomes and all management source hashes.
The full journal, blobs and separate launch trees are preserved locally under
`.worktrees/tmp/source-promotion-demonstration/`; the reproduction driver is
`.worktrees/tmp/run-source-promotion-demonstration-v2.py`. Timings are observations
on this shared host, not a performance claim. The original evaluation report
continues to say `activation_qualified=false`: explicit operator selection is
separate from the evaluation result.
