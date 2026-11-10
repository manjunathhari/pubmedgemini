"""Unit tests for flash_lite_probe.py (stdlib unittest; no network).

Run: python3 -m unittest discover -s probes
"""

import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import flash_lite_probe as p  # noqa: E402

QUESTION = p.DEFAULT_QUESTION
KEY = "TEST-not-a-real-key-0123456789"


# ---------------------------------------------------------------- fakes


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def efetch_xml(ids):
    arts = []
    for i in ids:
        arts.append(f"""<PubmedArticle><MedlineCitation><PMID>{i}</PMID><Article>
<Journal><JournalIssue><PubDate><Year>2020</Year></PubDate></JournalIssue><Title>Hernia</Title></Journal>
<ArticleTitle>Trial {i} of laparoscopic versus open repair</ArticleTitle>
<Abstract><AbstractText Label="RESULTS">Chronic pain was 9.8% vs 15.2% (p=0.03) in 412 patients.</AbstractText></Abstract>
<PublicationTypeList><PublicationType>Randomized Controlled Trial</PublicationType></PublicationTypeList>
</Article></MedlineCitation><PubmedData><ArticleIdList><ArticleId IdType="doi">10.1/x{i}</ArticleId></ArticleIdList></PubmedData>
</PubmedArticle>""")
    return ("<?xml version=\"1.0\"?><PubmedArticleSet>" + "".join(arts) + "</PubmedArticleSet>").encode()


class FakeLit:
    """Canned NCBI and Europe PMC replies, by URL."""

    IDS = ["11111111", "22222222", "33333333", "44444444", "55555555"]

    def __init__(self):
        self.urls = []

    def __call__(self, method, url, headers, body, timeout):
        self.urls.append(url)
        u = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.path.endswith("esearch.fcgi"):
            return 200, {}, json.dumps({"esearchresult": {"count": "120", "idlist": self.IDS[:3]}}).encode()
        if u.path.endswith("esummary.fcgi"):
            ids = q["id"].split(",")
            res = {"uids": ids}
            for i in ids:
                res[i] = {"uid": i, "title": f"Trial {i}", "source": "Hernia", "pubdate": "2020 Jan",
                          "pubtype": ["Journal Article", "Randomized Controlled Trial"]}
            return 200, {}, json.dumps({"result": res}).encode()
        if u.path.endswith("efetch.fcgi"):
            return 200, {}, efetch_xml(q["id"].split(","))
        if u.path.endswith("elink.fcgi"):
            return 200, {}, json.dumps({"linksets": [{"linksetdbs": [
                {"linkname": "pubmed_pubmed", "links": [q["id"]] + self.IDS[3:]}]}]}).encode()
        if "europepmc" in u.path:
            return 200, {}, json.dumps({"hitCount": 7, "resultList": {"result": [
                {"pmid": "55555555", "pmcid": "PMC1", "doi": "10.1/y", "title": "EPMC trial", "pubYear": "2021",
                 "journalTitle": "Surg Endosc", "pubType": "research-article; randomized controlled trial"},
                {"title": "Preprint without pmid", "pubYear": "2024", "pubType": "preprint"}]}}).encode()
        return 404, {}, b""


def cfg(**kw):
    c = {"question": QUESTION, "question_source": "default benchmark", "fake": True, "key": KEY, "model": None,
         "rpm": 600.0, "time_limit": 900.0, "schema_field": "responseSchema", "fc_mode": "AUTO",
         "ncbi_key": None, "ncbi_email": None}
    c.update(kw)
    return c


def gemini_resp(parts, finish="STOP", usage=None):
    return {"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": finish}],
            "usageMetadata": usage or {"promptTokenCount": 10, "candidatesTokenCount": 5}}


# ---------------------------------------------------------------- PMIDs and the study list


class StudyListTests(unittest.TestCase):
    def test_clean_pmid(self):
        self.assertEqual(p.clean_pmid(" 123 "), "123")
        self.assertEqual(p.clean_pmid("123456789"), "123456789")
        for bad in ["", "1234567890", "PMID 1", "12a", "１２", None, 123]:
            self.assertIsNone(p.clean_pmid(bad), bad)

    def _list(self, selected, considered=(), contradictions=()):
        return {"searches": [], "selected": selected, "considered_not_selected": list(considered),
                "possible_contradictions": list(contradictions)}

    def test_unseen_and_malformed_dropped_and_counted(self):
        # Incisive's own case (researcher.rs unseen_and_malformed_pmids_are_dropped_and_counted).
        seen = {"111", "222"}
        st = lambda pm, d: {"pmid": pm, "design": d, "why": "w"}  # noqa: E731
        v = p.validate_study_list(self._list([st("111", "RCT"), st("999", "RCT"), st(" 222 ", "SR_MA"),
                                              st("PMID 333", "other"), st("", "other"), st("111", "SR_MA")]), seen)
        self.assertEqual([s["pmid"] for s in v["selected"]], ["111", "222"])
        self.assertEqual(v["dropped_unseen"], 3)
        self.assertEqual(v["duplicates"], 1)
        self.assertEqual(v["selected"][0]["design"], "RCT")

    def test_capped_at_ten(self):
        ids = [str(i) for i in range(100, 115)]
        v = p.validate_study_list(self._list([{"pmid": i, "design": "RCT", "why": ""} for i in ids]), set(ids))
        self.assertEqual(len(v["selected"]), 10)
        self.assertEqual(v["over_cap"], 5)
        self.assertEqual(v["selected"][9]["pmid"], "109")

    def test_contradictions_only_on_seen(self):
        v = p.validate_study_list(self._list([], contradictions=[{"pmid": "111", "note": "n" * 900},
                                                                 {"pmid": "222", "note": "x"}]), {"111"})
        self.assertEqual(len(v["contradictions"]), 1)
        self.assertEqual(len(v["contradictions"][0]["note"]), p.MAX_NOTE)

    def test_seen_pmids_reads_record_fields_only(self):
        r = ['<source id="t1-1">\n{"count":2,"items":[{"pmid":"111","title":"a"},{"pmid":null,"title":"b"},'
             '{"pmid":"222","title":"c 333"}]}\n</source>',
             '{"papers":[{"pmid":"444"}],"not_found":["555"]}']
        self.assertEqual(sorted(p.seen_pmids(r)), ["111", "222", "444"])

    def test_empty_list_retry_decision(self):
        empty = self._list([])
        self.assertTrue(p.should_retry_empty(empty, {"1"}))
        self.assertFalse(p.should_retry_empty(empty, set()), "no papers seen: an empty list is honest")
        self.assertFalse(p.should_retry_empty(self._list([], considered=[{"pmid": "1", "reason": "r"}]), {"1"}))
        self.assertFalse(p.should_retry_empty(self._list([{"pmid": "1", "design": "RCT", "why": ""}]), {"1"}))

    def test_parse_study_list_is_strict(self):
        good = self._list([{"pmid": "1", "design": "RCT", "why": "w"}])
        self.assertIs(p.parse_study_list(good), good)
        with self.assertRaises(ValueError):
            p.parse_study_list(self._list([{"pmid": "1", "design": "cohort", "why": "w"}]))
        with self.assertRaises(ValueError):
            p.parse_study_list({"selected": []})


# ---------------------------------------------------------------- request bodies


class RequestBodyTests(unittest.TestCase):
    def test_function_calling_request_shape(self):
        decls = p.function_declarations([p.STAGE_A_TOOL], "responseSchema")
        body = p.build_turn_body("SYS", [p.text_content("user", "hi")], decls, 8192, "AUTO")
        self.assertEqual(body, {
            "systemInstruction": {"parts": [{"text": "SYS"}]},
            "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
            "tools": [{"functionDeclarations": [{
                "name": "get_probe_value",
                "description": "Return the stored test value for a key. Test tool with made-up data.",
                "parameters": {"type": "OBJECT",
                               "properties": {"key": {"type": "STRING", "description": "The key to look up."}},
                               "required": ["key"]},
            }]}],
            "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
            "generationConfig": {"maxOutputTokens": 8192},
        })

    def test_json_schema_variant_keeps_incisive_schema(self):
        decls = p.function_declarations([p.STAGE_A_TOOL], "responseJsonSchema")
        self.assertEqual(decls[0]["parametersJsonSchema"], p.STAGE_A_TOOL["schema"])
        self.assertNotIn("parameters", decls[0])
        body = p.build_structured_body("S", "U", p.outcomes_schema(), 100, "responseJsonSchema")
        self.assertEqual(body["generationConfig"]["responseJsonSchema"], p.outcomes_schema())
        self.assertNotIn("responseSchema", body["generationConfig"])

    def test_function_response_turn_shape(self):
        turn = p.function_response_turn([("search_pubmed", None, True, "R1"), ("find_related", "call-2", False, "E2")])
        self.assertEqual(turn, {"role": "user", "parts": [
            {"functionResponse": {"name": "search_pubmed", "response": {"output": "R1"}}},
            {"functionResponse": {"name": "find_related", "response": {"error": "E2"}, "id": "call-2"}},
        ]})

    def test_response_schema_request_shape(self):
        body = p.build_structured_body("S", "U", p.stage_a_schema(), 1024, "responseSchema")
        self.assertEqual(body, {
            "systemInstruction": {"parts": [{"text": "S"}]},
            "contents": [{"role": "user", "parts": [{"text": "U"}]}],
            "generationConfig": {
                "maxOutputTokens": 1024,
                "responseMimeType": "application/json",
                "responseSchema": {"type": "OBJECT", "properties": {
                    "status": {"type": "STRING", "enum": ["ok"]}, "number": {"type": "INTEGER"}},
                    "required": ["status", "number"]},
            },
        })

    def test_to_openapi_simplifications(self):
        s = p.to_openapi(p.outcomes_schema())
        txt = json.dumps(s)
        self.assertNotIn("additionalProperties", txt)
        self.assertNotIn('"null"', txt)
        num = s["properties"]["outcomes"]["items"]["properties"]["numbers"]["items"]
        self.assertEqual(num["properties"]["ci_low"], {"type": "NUMBER", "nullable": True})
        self.assertEqual(num["properties"]["unit"], {"type": "STRING", "nullable": True})
        self.assertEqual(num["required"], ["label", "value", "unit", "arm", "ci_low", "ci_high", "p", "pmid"])
        self.assertEqual(s["properties"]["outcomes"]["items"]["properties"]["certainty"]["enum"], p.CERTAINTY)

    def test_prompts_are_incisives(self):
        sp = p.researcher_system()
        self.assertIn("at most 8 tool turns", sp)
        self.assertIn("SELECTION: 6 to 10 studies that matter most", sp)
        self.assertNotIn("get_full_text", sp)
        self.assertIn('("practice guideline"[pt] OR guideline[pt] OR guideline*[ti] OR consensus[ti] OR recommendation*[ti])', sp)
        for w in ["hernia", "inguinal", "tapp", "robotic"]:
            self.assertNotIn(w, sp.lower())
        self.assertIn("at most 10 studies", p.FINAL_SYSTEM)
        self.assertTrue(p.OUTCOMES_INSTRUCTION.startswith("\n\nSECTION TO WRITE: OUTCOMES\n"))


# ---------------------------------------------------------------- response parsing


class ParseTests(unittest.TestCase):
    def test_text_reply(self):
        c = p.parse_reply(gemini_resp([{"text": "thinking", "thought": True}, {"text": "Hello "}, {"text": "there"}]))
        self.assertEqual(c.text, "Hello there")
        self.assertEqual(c.calls, [])
        self.assertEqual(c.finish, "STOP")
        self.assertFalse(c.empty)

    def test_single_function_call(self):
        c = p.parse_reply(gemini_resp([{"functionCall": {"name": "search_pubmed", "args": {"query": "q", "max_results": 5}},
                                        "thoughtSignature": "abc="}]))
        self.assertEqual(c.calls, [("search_pubmed", {"query": "q", "max_results": 5}, None)])
        self.assertEqual(c.content["parts"][0]["thoughtSignature"], "abc=")

    def test_multiple_function_calls(self):
        c = p.parse_reply(gemini_resp([
            {"text": "Searching."},
            {"functionCall": {"name": "search_pubmed", "args": {"query": "a"}, "id": "x1"}, "thoughtSignature": "s"},
            {"functionCall": {"name": "search_europepmc", "args": {"query": "b"}}},
        ]))
        self.assertEqual([n for n, _a, _i in c.calls], ["search_pubmed", "search_europepmc"])
        self.assertEqual(c.calls[0][2], "x1")
        self.assertEqual(c.text, "Searching.")

    def test_malformed_function_call(self):
        c = p.parse_reply({"candidates": [{"finishReason": "MALFORMED_FUNCTION_CALL", "finishMessage": "bad"}]})
        self.assertEqual(c.finish, "MALFORMED_FUNCTION_CALL")
        self.assertTrue(c.empty)
        self.assertEqual(c.calls, [])

    def test_max_tokens(self):
        c = p.parse_reply(gemini_resp([{"text": '{"outcomes": [{"outc'}], finish="MAX_TOKENS"))
        self.assertEqual(c.finish, "MAX_TOKENS")
        self.assertIsNone(p._read_json(c))

    def test_empty_candidates(self):
        c = p.parse_reply({"promptFeedback": {"blockReason": "SAFETY"}, "usageMetadata": {"promptTokenCount": 3}})
        self.assertTrue(c.empty)
        self.assertEqual(c.block_reason, "SAFETY")
        c = p.parse_reply({"candidates": []})
        self.assertTrue(c.empty)
        self.assertEqual(c.block_reason, "")

    def test_429_retry_delay(self):
        body = {"error": {"code": 429, "message": "Quota exceeded", "status": "RESOURCE_EXHAUSTED", "details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": []},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "41.5s"}]}}
        self.assertEqual(p.retry_delay_s({}, body), 41.5)
        self.assertEqual(p.retry_delay_s({"retry-after": "7"}, body), 7.0)
        self.assertIsNone(p.retry_delay_s({}, {"error": {"code": 503}}))
        self.assertEqual(p.api_error_fields(body), ("RESOURCE_EXHAUSTED", "Quota exceeded"))


# ---------------------------------------------------------------- the Gemini client


class ClientTests(unittest.TestCase):
    def _client(self, replies, rpm=600.0):
        fc = FakeClock()
        sent = []

        def http(method, url, headers, body, timeout):
            sent.append((method, url, headers, body))
            return replies.pop(0)

        g = p.Gemini(KEY, http, rpm, fc.t + 900, p.NullTranscript(), fc.clock, fc.sleep)
        return g, fc, sent

    @staticmethod
    def _r(status, obj, headers=None):
        return status, headers or {}, json.dumps(obj).encode()

    def test_key_only_in_header(self):
        g, _fc, sent = self._client([self._r(200, gemini_resp([{"text": "x"}]))])
        g.generate("gemini-x", {"contents": []}, 60, "t")
        method, url, headers, body = sent[0]
        self.assertEqual(headers["x-goog-api-key"], KEY)
        self.assertNotIn(KEY, url)
        self.assertNotIn(KEY.encode(), body)
        self.assertTrue(url.endswith("/v1beta/models/gemini-x:generateContent"))

    def test_429_waits_the_asked_delay_then_succeeds(self):
        err = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "q", "details": [
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "12s"}]}}
        g, fc, _ = self._client([self._r(429, err), self._r(200, gemini_resp([{"text": "x"}]))])
        g.generate("m", {}, 60, "t")
        self.assertEqual(g.n429, 1)
        self.assertEqual(g.wait_total, 12.0)
        self.assertIn(12.0, fc.slept)
        self.assertEqual(g.attempts, 2)

    def test_429_gives_up_after_three_retries(self):
        err = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "q"}}
        g, _fc, _ = self._client([self._r(429, err)] * 4)
        with self.assertRaises(p.ProbeError) as cm:
            g.generate("m", {}, 60, "t")
        self.assertEqual(cm.exception.kind, "LIMIT")
        self.assertEqual(g.attempts, 4)
        self.assertEqual(g.n429, 4)

    def test_long_retry_delay_is_not_waited(self):
        err = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "q", "details": [
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "3600s"}]}}
        g, fc, _ = self._client([self._r(429, err)])
        with self.assertRaises(p.ProbeError) as cm:
            g.generate("m", {}, 60, "t")
        self.assertEqual(cm.exception.kind, "LIMIT")
        self.assertEqual(fc.slept, [])

    def test_400_is_setup_and_message_scrubbed(self):
        err = {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                         "message": f"Invalid JSON payload received. Unknown name \"additionalProperties\" {KEY}"}}
        g, _fc, _ = self._client([self._r(400, err)])
        with self.assertRaises(p.ProbeError) as cm:
            g.generate("m", {}, 60, "t")
        e = cm.exception
        self.assertEqual((e.kind, e.status, e.api_status), ("SETUP", 400, "INVALID_ARGUMENT"))
        self.assertNotIn(KEY, e.detail)
        for code in (401, 403, 404):
            g, _fc, _ = self._client([self._r(code, {"error": {"code": code}})])
            with self.assertRaises(p.ProbeError) as cm:
                g.generate("m", {}, 60, "t")
            self.assertEqual(cm.exception.kind, "SETUP")

    def test_rpm_spacing(self):
        ok = self._r(200, gemini_resp([{"text": "x"}]))
        g, fc, _ = self._client([ok, ok], rpm=10)
        g.generate("m", {}, 60, "t")
        g.generate("m", {}, 60, "t")
        self.assertAlmostEqual(sum(fc.slept), 6.0)


# ---------------------------------------------------------------- model choice, tools, checks


class ModelChoiceTests(unittest.TestCase):
    def test_prefers_stable_newest_flash_lite(self):
        m = p.choose_model(p.FakeGemini.MODELS["models"])
        self.assertEqual(m["name"], "models/gemini-2.5-flash-lite")
        models = [{"name": f"models/{n}", "supportedGenerationMethods": ["generateContent"]} for n in
                  ["gemini-2.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.10-flash-lite-preview",
                   "gemini-flash-lite-latest", "gemini-3.1-flash-lite-tts"]]
        self.assertEqual(p.choose_model(models)["name"], "models/gemini-3.1-flash-lite")
        self.assertIsNone(p.choose_model([{"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]}]))
        self.assertIsNone(p.choose_model([{"name": "models/gemini-2.5-flash-lite", "supportedGenerationMethods": ["embedContent"]}]))

    def test_model_id(self):
        self.assertEqual(p.model_id("models/gemini-2.5-flash-lite"), "gemini-2.5-flash-lite")
        self.assertEqual(p.model_id("gemini-2.5-flash-lite"), "gemini-2.5-flash-lite")


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.fc = FakeClock()
        self.http = FakeLit()
        self.lit = p.LitTools(self.http, QUESTION, self.fc.t + 900, self.fc.clock, self.fc.sleep)

    def test_query_checks(self):
        self.assertIsNone(p.validate_query('"inguinal hernia"[tiab] AND (TAPP[tiab] OR TEP[tiab])'))
        for bad, kind in [("", "empty"), ("a\nb", "newline"), ("see http x", "url"), ("a; b", "character"),
                          ("(a", "unbalanced"), ("PUB_TYPE:\"x\"", "character")]:
            self.assertEqual(p.validate_query(bad), kind, bad)
        with self.assertRaises(p.ToolInputError):
            self.lit.run("search_pubmed", {"query": "adults having elective inguinal hernia repair how does laparoscopic repair",
                                           "max_results": 5})

    def test_search_pubmed_shape_and_ncbi_params(self):
        out = json.loads(self.lit.run("search_pubmed", {"query": "hernia[tiab]", "max_results": 3.0}))
        self.assertEqual(out["count"], 120)
        self.assertEqual(out["items"][0], {"pmid": "11111111", "title": "Trial 11111111", "year": 2020,
                                           "journal": "Hernia", "pub_types": ["Journal Article", "Randomized Controlled Trial"]})
        for u in self.http.urls:
            self.assertIn("tool=pubmedgemini_probe", u)
            self.assertNotIn("email=", u)
        self.assertGreaterEqual(sum(self.fc.slept), 0.3, "paced at 3 requests a second without a key")

    def test_other_tools(self):
        e = json.loads(self.lit.run("search_europepmc", {"query": "TITLE:hernia", "max_results": 5}))
        self.assertEqual(e["count"], 7)
        self.assertEqual(e["items"][0]["pmcid"], "PMC1")
        self.assertIsNone(e["items"][1]["pmid"])
        a = json.loads(self.lit.run("get_abstracts", {"pmids": ["11111111", "11111111", "22222222"]}))
        self.assertEqual([x["pmid"] for x in a["papers"]], ["11111111", "22222222"])
        self.assertIn("9.8%", a["papers"][0]["abstract"])
        self.assertEqual(a["papers"][0]["doi"], "10.1/x11111111")
        r = json.loads(self.lit.run("find_related", {"pmid": "11111111", "max_results": 2}))
        self.assertEqual([x["pmid"] for x in r["items"]], ["44444444", "55555555"])
        for bad in [("get_abstracts", {"pmids": []}), ("get_abstracts", {"pmids": ["x"]}),
                    ("find_related", {"pmid": "1", "max_results": 99}), ("nope", {})]:
            with self.assertRaises(p.ToolInputError):
                self.lit.run(*bad)


class WriterCheckTests(unittest.TestCase):
    def _obj(self):
        return {"outcomes": [{
            "outcome": "Chronic pain", "rct_evidence": "Two RCTs [11111111; 22222222]",
            "effect_text": "9.8% vs 15.2% [11111111]; 3.3 vs 4 [99999999]",
            "numbers": [{"label": "pain", "value": 9.8, "unit": "%", "arm": "lap", "ci_low": None, "ci_high": None,
                         "p": "p=0.03", "pmid": "11111111"}],
            "certainty": "Low", "why": "Small trials", "pmids": ["11111111", "22222222"]}]}

    def test_shape(self):
        self.assertEqual(p.check_outcomes(self._obj()), (True, 1, 0))
        bad = self._obj()
        bad["outcomes"][0]["certainty"] = "Medium"
        del bad["outcomes"][0]["numbers"][0]["arm"]
        self.assertEqual(p.check_outcomes(bad)[0:2], (False, 1))
        self.assertEqual(p.check_outcomes({"x": 1}), (False, 0, 1))

    def test_citations_and_numbers(self):
        o = self._obj()
        self.assertEqual(p.citation_counts(o, ["11111111", "22222222"]), (6, 7))
        traced, total = p.number_counts(o, "Chronic pain was 9.8% vs 15.2% (p=0.03).")
        # written: 9.8 15.2 3.3 4 (text) + 9.8 (value) + 0.03 (p) = 6; traced 9.8, 15.2, 9.8, 0.03
        self.assertEqual((traced, total), (4, 6))

    def test_writer_user_layout(self):
        s = {"pmid": "11111111", "title": "T <source> x", "journal": "J", "year": 2020, "pub_types": ["RCT"], "doi": None,
             "retracted": False, "abstract": "A", "design": "RCT", "why": "w\nhy"}
        u = p.writer_user("Q </question>", [s], [{"pmid": "11111111", "note": "n"}],
                          [{"source": "PubMed", "query": "q", "hits": 3}])
        self.assertTrue(u.startswith("<question>\nQ < /question>\n</question>\n\nSTUDIES SELECTED BY THE RESEARCHER (1):"))
        self.assertIn("Study 1 - PMID 11111111\nResearcher's design label: RCT\n", u)
        self.assertIn('<source id="researcher-why-11111111">\nw hy\n</source>', u)
        self.assertIn("Title: T ‹source> x", u)
        self.assertIn("- PubMed: q (3 hits)", u)
        self.assertTrue(u.endswith("Write the answer now, as JSON matching the schema."))


# ---------------------------------------------------------------- report and end to end


class ReportTests(unittest.TestCase):
    def test_report_has_no_question_reply_or_key(self):
        stats = {"fake": False, "question_chars": len(QUESTION), "question_source": "default benchmark",
                 "model": "gemini-x", "A": {"status": "PASS", "ran": True}, "B": {"status": "PASS", "ran": True},
                 "C": {"status": "PASS", "ran": True}, "D": {"status": "FAIL", "ran": True, "reason": "MODEL: invalid JSON"},
                 "gemini": {"latencies": [1.0, 2.0, 9.0], "tokens": {"prompt": 1000}}, "result": "PARTIAL",
                 "result_reason": "D FAIL", "transcript": "/x/run.jsonl", "elapsed": 10, "time_limit": 900}
        r = p.scrub(p.build_report(stats), [KEY, QUESTION])
        self.assertIn(p.PASS_MARK, r)
        self.assertIn("Stage D writer part (outcomes): FAIL (MODEL: invalid JSON)", r)
        self.assertIn("median 2.0", r)
        self.assertTrue(r.rstrip().splitlines()[-2].startswith("RESULT: PARTIAL"))
        self.assertNotIn(QUESTION, r)
        self.assertNotIn(KEY, r)
        self.assertEqual(p.scrub(f"a {KEY} b", [KEY]), "a [removed] b")


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="probe-home-")
        self.old_home = os.environ.get("HOME")
        os.environ["HOME"] = self.home

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)
        if self.old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.old_home

    def _run(self, fake, **kw):
        fc = FakeClock()
        report, code = p.run_probe(cfg(**kw), fake, FakeLit(), fc.clock, fc.sleep)
        return report, code, fake

    def test_full_run_passes_with_retry_drop_and_429(self):
        report, code, fake = self._run(p.FakeGemini())
        self.assertEqual(code, 0, report)
        self.assertIn("RESULT: PASS", report)
        self.assertIn("Empty-list retry (S3) fired: yes; retry outcome: usable", report)
        self.assertIn("dropped unseen or not a PMID: 2", report)
        self.assertIn("429 responses 1", report)
        self.assertIn("Turns used: 3 of 8; loop ended: model finished", report)
        self.assertIn("search_pubmed 1, search_europepmc 1, get_abstracts 1, find_related 1", report)
        self.assertNotIn(QUESTION, report)
        self.assertNotIn(KEY, report)
        self.assertNotIn("Reported 9.8", report)
        # Thought signatures were echoed unchanged (the fake refuses otherwise).
        turns = [r for r in fake.requests if r.get("tools") and
                 r["tools"][0]["functionDeclarations"][0]["name"] == "search_pubmed"]
        self.assertEqual(turns[-1]["contents"][1]["parts"][0]["thoughtSignature"], "c2lnQg==")
        self.assertEqual(len(turns[-1]["contents"][2]["parts"]), 2, "both results in one user turn")
        # The transcript: private, holds the question, never the key.
        d = os.path.join(self.home, "pubmedgemini-probe")
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)
        files = os.listdir(d)
        self.assertEqual(len(files), 1)
        f = os.path.join(d, files[0])
        self.assertEqual(stat.S_IMODE(os.stat(f).st_mode), 0o600)
        with open(f, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn(json.dumps(QUESTION)[1:-1], text)
        self.assertNotIn(KEY, text)
        self.assertIn(f, report)

    def test_setup_failure_stops_after_stage_a(self):
        class Refusing(p.FakeGemini):
            def __call__(self, method, url, headers, body, timeout):
                if method == "POST":
                    self.requests.append(json.loads(body.decode()))
                    return self._err(400, "INVALID_ARGUMENT", 'Unknown name "nullable" at generation_config.response_schema')
                return super().__call__(method, url, headers, body, timeout)

        report, code, fake = self._run(Refusing())
        self.assertEqual(code, 2)
        self.assertIn("Stage A setup check: FAIL (SETUP: request refused (HTTP 400))", report)
        self.assertIn("API said: INVALID_ARGUMENT: Unknown name", report)
        self.assertIn("Stage B researcher loop: NOT RUN", report)
        self.assertEqual(len(fake.requests), 1)

    def test_malformed_call_and_cut_off_writer_is_partial(self):
        class Flaky(p.FakeGemini):
            def __call__(self, method, url, headers, body, timeout):
                if method == "POST":
                    req = json.loads(body.decode())
                    tools = req.get("tools")
                    if tools and tools[0]["functionDeclarations"][0]["name"] == "search_pubmed":
                        if sum(1 for c in req["contents"] if c.get("role") == "model") == 1:
                            self.requests.append(req)
                            return self._ok({"candidates": [{"finishReason": "MALFORMED_FUNCTION_CALL"}]})
                    gc = req.get("generationConfig", {})
                    if "outcomes" in (gc.get("responseSchema") or {}).get("properties", {}):
                        self.requests.append(req)
                        return self._ok(self._reply([{"text": '{"outcomes": [{"outcome": "Pa'}], finish="MAX_TOKENS"))
                return super().__call__(method, url, headers, body, timeout)

        report, code, _ = self._run(Flaky(empty_first_list=False, rate_limit_once=False))
        self.assertEqual(code, 1, report)
        self.assertIn("Stage B researcher loop: FAIL (MODEL: malformed function call)", report)
        self.assertIn("Stage C study list: PASS", report)
        self.assertIn("Stage D writer part (outcomes): FAIL (MODEL: cut off (MAX_TOKENS), JSON incomplete)", report)
        self.assertIn("cut off: yes", report)
        self.assertIn("RESULT: PARTIAL", report)

    def test_no_flash_lite_model(self):
        class NoLite(p.FakeGemini):
            MODELS = {"models": [{"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]}]}

        report, code, _ = self._run(NoLite())
        self.assertEqual(code, 2)
        self.assertIn("no Flash-Lite model found; available: gemini-2.5-flash", report)

    def test_unknown_model_id_is_setup(self):
        report, code, _ = self._run(p.FakeGemini(), model="gemini-9-flash-lite")
        self.assertEqual(code, 2)
        self.assertIn("SETUP: model or endpoint not found (HTTP 404)", report)

    def test_json_schema_variant_runs(self):
        report, code, fake = self._run(p.FakeGemini(), schema_field="responseJsonSchema")
        self.assertEqual(code, 0, report)
        self.assertTrue(any("responseJsonSchema" in r.get("generationConfig", {}) for r in fake.requests))


if __name__ == "__main__":
    unittest.main()
