# Flash-Lite probe

## What it is for

pubmedgemini will run Incisive's Quick evidence pipeline on Google Gemini's free tier, with the Flash-Lite model doing every step. The riskiest question is whether Flash-Lite can manage three jobs:

1. run a search loop over several rounds, choosing and calling the literature tools itself;
2. return a valid study list;
3. return a long, valid structured answer part.

This probe tests those three jobs once, cheaply, before the engine is built. It is one file with no add-ons: plain Python 3.10 or newer, which Ubuntu 24.04 already has.

## Pass mark

> Flash-Lite completes a search loop and returns a valid study list and one valid answer part on a benchmark question.

## What is sent to Google

- **The question.** By default this is a made-up benchmark question about inguinal hernia repair. It names no patient.
- **The prompts.** These are Incisive's own researcher, study-list and writer prompts.
- **What the searches return.** That means PubMed and Europe PMC titles, journals, years, publication types, and the abstracts of the papers the model asks to read and of the studies it selects.
- **Three tiny setup messages** containing made-up data.

**On the free tier, Google may use what you send and what comes back, and human reviewers may read it.** Never type anything that could identify a patient.

The search queries go to NCBI (PubMed) and Europe PMC. The question itself does not: a query that repeats eight or more words of the question in a row is refused, as Incisive does.

## How to run it

Do this once in the shell, so the key never lands in your shell history. Paste the key and press Enter; nothing is shown while you paste:

```
read -rs GEMINI_API_KEY && export GEMINI_API_KEY
```

Then, in the folder that holds the file:

```
python3 flash_lite_probe.py
```

The probe takes about 2 to 5 minutes (an estimate). It stops itself at 15 minutes and never makes more than 30 Gemini requests.

### Optional settings

Put any of these in front of the command, for example `GEMINI_RPM=5 python3 flash_lite_probe.py`.

| Setting | What it does |
|---|---|
| `GEMINI_MODEL=...` | Use this model id instead of choosing one. By default the probe picks the newest Flash-Lite that is not a preview. |
| `GEMINI_RPM=10` | Requests a minute (default 10). |
| `GEMINI_SCHEMA_FIELD=responseJsonSchema` | Send Incisive's schemas unchanged, in Google's JSON Schema field, instead of the simplified `responseSchema` (see "Not checked"). |
| `GEMINI_FC_MODE=VALIDATED` | Ask Google to check the tool calls as they are written. The default is `AUTO`, which matches Incisive. |
| `PROBE_TIME_LIMIT_S=900` | Overall time limit in seconds. |
| `NCBI_API_KEY=...` | Your free NCBI key, which allows 10 PubMed requests a second instead of 3. |
| `NCBI_EMAIL=...` | A contact email that NCBI asks tools to send. It is sent only if you set it. |
| `PROBE_QUESTION="..."` | A different question. You can also put it in quotes after the command. |

`python3 flash_lite_probe.py --fake-gemini` runs everything against scripted replies instead of Gemini. It tests the probe itself, not Flash-Lite.

## What the report means

The report holds counts and fixed labels only. It never shows the question, the queries, the abstracts, the model's replies or the key.

- **Model**: the model chosen, with the input and output limits Google reports for it.
- **Stage A setup check**: three tiny calls with made-up data, which check the key, the model id and the request shapes. A failure here is a **SETUP** failure (wrong key, wrong model, a request Google refused, or quota), not a model failure. The probe stops, and prints the HTTP status and Google's error status and message.
- **Stage B researcher loop**: how many turns were used (at most 8), and why the loop ended. "model finished" is the normal end; "turn cap" means all 8 rounds were used. It also shows how many times each tool was called, how many searches ran, and how many distinct PMIDs the model actually saw in tool results.
- **Stage C study list**:
  - **reported**: how many studies the model selected.
  - **kept**: how many passed the checks.
  - **dropped unseen or not a PMID**: the model named a paper that no search returned. This is a model fault.
  - **Empty-list retry (S3)**: whether the model returned an empty list although papers had been seen, and was asked once more, as Incisive does.
- **Stage D writer part (outcomes)**:
  - **finish reason**: STOP is normal; MAX_TOKENS means the reply was cut off.
  - **JSON valid** and **required fields present**: whether the reply can be read as the answer part.
  - **Outcomes**: how many outcome rows were written.
  - **Citations valid X of Y**: cited PMIDs that are one of the selected studies.
  - **Numbers traced X of Y**: numbers in the answer that also appear in the selected abstracts. This is a simple count, not Incisive's full number check.
- **Latency**: the time each Gemini call took (shortest, middle, longest).
- **Tokens**: prompt, output and "thinking" tokens, as Google reports them.
- **Rate limits**: how many times Google said "too many requests" (HTTP 429), and how long the probe waited.
- **Failure labels**:
  - **SETUP** means our request or account: an HTTP error other than 429, a refused schema, or the key.
  - **MODEL** means Flash-Lite: a malformed tool call, invalid JSON, a cut-off reply, an empty list or unseen PMIDs.
  - **LIMIT** means quota, overload or the time limit.
- **RESULT**:
  - `PASS`: stages B, C and D all passed.
  - `PARTIAL`: some passed.
  - `FAIL`: setup failed or nothing downstream could run.

## The transcript file

The full record is saved in the folder `pubmedgemini-probe` in your home folder, as `run-<date>-<time>.jsonl`. It holds every request sent to Google (without the key), every reply, and every tool result. Only you can open it: the folder has mode 700 and the file mode 600. The report prints only its path.

**This file holds the question and the model's reply text. Do not commit it to the repo, and do not paste it into a chat wholesale.**

## Not checked

This workspace cannot reach Gemini. Every Gemini detail below comes from Google's public pages as read on 10 Nov 2026, or from memory; none has been tried against the live API. For each one, this is what the probe does if the detail is wrong.

1. **`responseSchema`.** Google's reference now marks it deprecated in favour of `responseFormat` / `responseJsonSchema`. Whether it is still accepted is not checked. The probe sends it by default, because that was the plan. *If refused:* Stage A fails as SETUP with Google's message. Re-run with `GEMINI_SCHEMA_FIELD=responseJsonSchema`.
2. **The shape inside `responseSchema` and the tool `parameters`.** The probe sends upper-case type names (`OBJECT`, `STRING`), `nullable: true` for optional values, and no `additionalProperties`. This was not confirmed on a Google page in this session. *If refused:* Stage A fails as SETUP. Use `GEMINI_SCHEMA_FIELD=responseJsonSchema`; that also sends `parametersJsonSchema`, with Incisive's schemas unchanged.
3. **Schema size.** Google says very large schemas can be refused with HTTP 400. Whether the study-list and outcomes schemas fit is not checked. *If refused:* Stage C or D fails as SETUP, with Google's message.
4. **Tool results.** The probe sends them back as `functionResponse.response = {"output": <text>}`, or `{"error": <text>}` on failure. Google documents the `error` key; the `output` key is from memory. *If wrong:* the model may misread results. That would show as poor searching, or as HTTP 400 (SETUP).
5. **Thought signatures.** The probe sends every model turn back exactly as received, so any `thoughtSignature` or thought parts are kept. This follows Google's thought-signature page. *If wrong:* HTTP 400 or finish `MISSING_THOUGHT_SIGNATURE`, both labelled SETUP.
6. **Finish reason on tool-call turns.** The probe treats a turn with `functionCall` parts and finish `STOP` (or none) as a tool turn. *If Google uses another finish reason there:* Stage B fails as MODEL, with the reason named, and the run carries on to Stage C.
7. **Several tool calls in one turn.** They are all run, and all the answers go back in one `user` turn, as Google's page says. This is not tried live.
8. **Rate-limit replies.** On a 429, the probe looks for the wait time in `Retry-After` and in `google.rpc.RetryInfo.retryDelay` (Google's usual error format). Neither is checked live. *If neither is found:* it waits 10, 20, then 40 seconds. A wait over 90 seconds (likely the daily quota) is not attempted; the probe stops with a LIMIT failure.
9. **Thinking and `maxOutputTokens`.** Whether thinking tokens count against `maxOutputTokens` for the chosen model is not checked. The probe gives the writer up to 32,768 tokens (or the model's output limit, if lower) and reports the thinking tokens.
10. **Free-tier limits.** The 10 requests a minute come from a third-party page; Google's own figure is not checked. The probe paces itself at `GEMINI_RPM`.
11. **Model ids.** Which Flash-Lite ids your key can see is not checked. The probe lists the models and picks one; if there is none, it prints the names it found.
12. **Function-calling modes `AUTO` and `VALIDATED`.** Google's reference lists both, but whether Flash-Lite supports each is not checked. *If not:* Stage A fails as SETUP.
13. **Safety settings.** These are left at Google's defaults. A surgical question could in principle be blocked; that would show as a MODEL failure, "blocked".

Checked against Google's reference pages (10 Nov 2026):

- the `x-goog-api-key` header;
- the `generateContent` body fields (`contents`, `systemInstruction`, `tools`, `toolConfig`, `generationConfig`);
- `functionDeclarations` with `parameters` or `parametersJsonSchema`;
- `functionCall {id, name, args}` and `functionResponse {id, name, response}`;
- the `thought` and `thoughtSignature` part fields;
- the list of finish reasons;
- the `usageMetadata` token fields;
- `promptFeedback.blockReason`;
- the `models.list` fields: `name`, `inputTokenLimit`, `outputTokenLimit`, `supportedGenerationMethods`.

## Where the probe differs from Incisive

Prompts, tool descriptions, the study-list rules (seen PMIDs only, PMIDs of 1 to 9 digits, no duplicates, at most 10) and the empty-list retry are Incisive's, unchanged. The differences:

1. **Schemas.** With `responseSchema` (the default), Incisive's JSON Schemas are converted:
   - `additionalProperties: false` is dropped;
   - `["string","null"]` becomes `STRING` with `nullable: true`;
   - type names are written in capitals.

   Nothing else changes: all fields, required lists and enums are kept. With `responseJsonSchema`, the schemas are sent unchanged.
2. **Tool results.** Each one goes back as a Gemini `functionResponse` (`output` or `error`) instead of Claude's `tool_result`. The text inside, a wrapped `<source>` block, is the same.
3. **Output token caps.** These are larger than Incisive's (8,192 a turn instead of 4,000; 32,768 for the writer instead of 12,000), to leave room for any thinking.
4. **No effort setting.** Incisive makes the study-list call at low effort; Gemini's thinking level is left at the model's default.
5. **One writer call.** Incisive retries a failed part once; the probe measures the first try only.
6. **Abstracts.** In `get_abstracts` results, each is cut at 5,000 characters (Incisive relies on its 30,000-character cap per result). There is no paper cache, and abstracts come from PubMed efetch alone.
7. **The number check is a simple count.** Incisive's full tracing is not used, and nothing is removed.
8. **Time limit.** The overall limit is 15 minutes, instead of Incisive's 6-minute Quick stop and its money budget.

One fault is copied from Incisive on purpose. Incisive's query check does not allow `_`, so Europe PMC queries using `PUB_TYPE:` or `PUB_YEAR:` are refused with an input error, although Incisive's own tool description suggests them. The probe keeps this so that it measures what Incisive does. The report counts such refusals under "errors".
