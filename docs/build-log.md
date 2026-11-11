# Build log

## 10 Nov 2026
- Handover read; Incisive repo read for the real Quick pipeline.
- Owner decisions recorded (spec §2). Spec approved by the owner.
- ADR-001 and the F0 plan written.
- Project Board created; agent files added.
- F0 build started on branch `f0-skeleton`.
- F0 built on `f0-skeleton` in seven tasks, tests first.
- Independent review: fix first (1 high, 5 medium, 5 low). All fixed. The high one: the sign-in gate was pinned to an old version with a known bypass (CVE-2026-40575); the skip-auth rule was removed and the gate pinned to v7.15.6.
- Re-check: 9 of 11 confirmed, 8 new findings (1 medium, 7 low). All fixed. The medium one: an interrupted deploy could leave the app half-switched; the deploy script now rolls back on interruption.
- Flash-Lite test script written on branch `probe-flash-lite`; not yet run against Gemini.
- Web address chosen by the owner and its DNS record added (details kept out of this public repo).

## 11 Nov 2026
- F1 independent review: 10 findings (2 medium, 8 low), A to J. All fixed on `f1-search-tools`, tests first; mutation checks for the rewritten tests. The owner added a free NCBI key on the server on 10 Nov after the first live search hit NCBI's 429 without one.
- F2 built on `f2-researcher`, tests first: Gemini settings, tables, the Gemini client, pacer and day count, the echo guard, Incisive's prompts and study-list checks, the researcher loop, runs on a background thread, Incisive's patient-details check (owner decision, 11 Nov), the pages, the leak tests, and the compose settings with the runbook step for the key. Coordinator rulings applied: the patient-details check sees the question exactly as typed; ported test emails use reserved example domains; the medical gate is not in F2. Spec §4, §5 and §7 brought in line with the code. Not yet run against Gemini (this workspace cannot reach it); the first real run is the owner's, after the merge and the key.
- F2 independent review, first round: fixed M3 (the 420 s hard stop now holds after the tool turns, around every study-list call and around the records fetch), L1 (the patient-details check also sees the LF text sent to Gemini), L3 (a turn sent before the wrap-up may run to the hard stop, as Incisive), L4 (network and timeout errors carry no cause or context) and L5 (a finished or stopped run is never overwritten), tests first; L2 corrected in the plan. Waiting for the owner: M1 (stale-run sweep), M2 (per-run call cap), the "Run again" button, email domains in F0 test files, L6.
- Owner decisions after the F2 review (11 Nov): stuck run, leave as is; call cap, cap per run; Run again, on failed and partial runs too; test emails, leave. Built on `f2-researcher`, tests first, one commit each: the per-run cap of 14 Gemini attempts (new failed kind `run_call_cap`, "Free runs left" unchanged at 45 on a fresh day); Run again on stopped, failed and partial runs (spec §4 and plan Task 9 updated); the parse and request-refused errors and the run-ending FatalModelErrors are raised outside their except blocks, so no reply text or other error rides along (as L4). Not pushed.
