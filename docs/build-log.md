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
