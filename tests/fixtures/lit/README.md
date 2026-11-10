# Recorded literature replies

Real replies from NCBI E-utilities and Europe PMC, recorded once on 10 Nov 2026 (no API key,
no email, `tool=pubmedgemini`), then trimmed to the fields the parsers read. Public
bibliographic data only. The tests replay them through `tests/lit_fakes.py`; no test uses the
network.

## Recording

```sh
E=https://eutils.ncbi.nlm.nih.gov/entrez/eutils
curl -sS -o esearch20.json "$E/esearch.fcgi?db=pubmed&term=inguinal+hernia+mesh&retmax=20&sort=relevance&retmode=json&tool=pubmedgemini"
curl -sS -o esearch_error.json "$E/esearch.fcgi?db=pubmed&term=&retmax=5&sort=relevance&retmode=json&tool=pubmedgemini"
curl -sS -o esearch_retracted.json "$E/esearch.fcgi?db=pubmed&term=inguinal+hernia+mesh+AND+retracted+publication%5Bpt%5D&retmax=5&sort=relevance&retmode=json&tool=pubmedgemini"
curl -sS -o esummary.json "$E/esummary.fcgi?db=pubmed&id=36262096,33763744,31696731,999999999&retmode=json&tool=pubmedgemini"
curl -sS -o efetch20.xml "$E/efetch.fcgi?db=pubmed&id=<the 20 ids of esearch20.json>&rettype=abstract&retmode=xml&tool=pubmedgemini"
curl -sS -o efetch.xml "$E/efetch.fcgi?db=pubmed&id=31482863,29754625,35259338,38265557&rettype=abstract&retmode=xml&tool=pubmedgemini"
curl -sS -o elink.json "$E/elink.fcgi?db=pubmed&dbfrom=pubmed&id=31482863&cmd=neighbor_score&linkname=pubmed_pubmed&retmode=json&tool=pubmedgemini"
curl -sS -A pubmedgemini -o epmc25.json "https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=inguinal+hernia+mesh&resultType=core&format=json&pageSize=25"
curl -sS -G -A pubmedgemini -o epmc_rct.json "https://www.ebi.ac.uk/europepmc/webservices/rest/search" \
  --data-urlencode 'query=inguinal hernia mesh AND PUB_TYPE:"Randomized Controlled Trial"' \
  --data-urlencode resultType=core --data-urlencode format=json --data-urlencode pageSize=10
```

Only the record of 38265557 is kept from that last efetch; the other three come from `efetch20.xml`. The first `esearch.json` and `esummary.json` recordings (retmax 5; ids 31482863, 29754625, 35259338) were replaced by the ones above and are not used.

## Files, and every change from the recorded reply

| File | What it holds | Trimmed or edited |
|---|---|---|
| `esearch.json` | Search `inguinal hernia mesh`: the real `count` (5160) and three ids | `idlist` cut to 36262096, 33763744, 31696731 (hits 5, 7 and 11 of the top 20, in that order: the first two with a structured abstract, 33763744 also being in the Europe PMC reply, and one with a structured abstract, a PMCID and `<sup>` markup); `retmax` set to 3; `translationset` and `querytranslation` removed |
| `esearch_error.json` | esearch with an empty term: an `ERROR` key | none |
| `esummary.json` | The three ids, plus 999999999, which NCBI answers with `error` | Per record only `uid`, `pubdate`, `epubdate`, `source`, the first three `authors`, `title`, `fulljournalname`, `pubtype`, `articleids` (`idtype`, `value`). **Edited:** the title of 31696731 is `<script>alert(1)</script> Mesh repair` (the page's escaping test) |
| `efetch.xml` | The three ids, plus 38265557: a retracted letter with a `CommentsCorrections RefType="RetractionIn"` (from the `retracted publication[pt]` search) | XML declaration and DOCTYPE as recorded; per record only `PMID`, `ArticleTitle`, `Abstract`, `PublicationTypeList` and `CommentsCorrectionsList`. **Edited** in the METHODS and RESULTS of 36262096: `slit Vs nonslit` became `slit <i>Vs</i> nonslit`, and `95% CI (0.27-1.41)` became `95%&thinsp;CI (0.27-1.41)` (an inline tag and a DTD-only entity) |
| `elink.json` | Related articles of 31482863 (`neighbor_score`) | `links` cut to the first five |
| `europepmc.json` | Four Europe PMC core hits: 33763744 (also in PubMed), 39801280 (MEDLINE), PPR1174106 (a preprint with a DOI and no PMID), 42622923 (an RCT, from the `PUB_TYPE` search) | `hitCount` and `version` of the first search; per hit only `id`, `source`, `pmid`, `pmcid`, `doi`, `title`, `authorString`, the first three `authorList` names, `journalInfo` (`yearOfPublication`, `journal.title`), `pubYear`, `pubTypeList`, `abstractText`, `firstPublicationDate`, `isOpenAccess`. **Trimmed:** 39801280 has no `pmid` field (its PMID is in `id` only). **Edited:** in the title of 42622923, `plastic-sheath` became `<i>plastic-sheath</i>` |
