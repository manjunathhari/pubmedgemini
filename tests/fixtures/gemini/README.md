# Gemini replies for the tests

Hand-written, in the shapes of the Flash-Lite probe (`probes/flash_lite_probe.py` and its unit
tests) that worked against the live `generateContent` API on 10 Nov 2026. **No live reply is
recorded here:** a live transcript holds the question and the model's text, which never go in
the repo. Every value below is made up.

| File | What it is |
|---|---|
| `reply_two_calls.json` | A 200 reply: a thought part, a text part and two function calls, the first with an `id` and a thought signature |
| `error_429_rate.json` | A per-minute 429: `QuotaFailure` with a per-minute `quotaId`, and `RetryInfo` asking for 12 s |
| `error_429_daily.json` | A daily-quota 429: `QuotaFailure` with a `quotaId` containing `PerDay` |
| `error_400_key_invalid.json` | A 400 whose `ErrorInfo.reason` is `API_KEY_INVALID` |

`tests/gemini_fakes.py` builds the other replies with its helpers (`reply`, `call`, `error`).
