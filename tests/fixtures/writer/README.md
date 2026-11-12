# Writer fixtures (F3)

Hand-written replies in the shape of the writer's JSON (`writer/prompts.py` `writer_schema`),
with the numbers of the frozen answer-page mock's hernia example
(`docs/mocks/f3-answer-page-v3.html`). No live reply is recorded here.

- `whole.json`: one reply of the one-piece writer.
- `trials.json`, `outcomes.json`, `context.json`, `summary.json`: the same reply cut into the
  four parts (each part holds only its own fields), so merging the four gives `whole.json`.

The PMIDs 90000001 to 90000008 are made up (PubMed has not assigned numbers that high); the
study labels, years and journals follow the mock. Two values are written as the model may
write them: `n` as the text `"1370"` and `n` as `0` (read as not stated).
