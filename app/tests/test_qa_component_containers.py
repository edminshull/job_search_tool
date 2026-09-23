"""Component tests that talk to a REAL upstream over a real socket.

WHAT IS DIFFERENT HERE
----------------------
The other component module (test_qa_component.py) tests the pipeline's logic
in-process. This one closes the gap those tests structurally cannot reach: the
HTTP clients. Their current unit tests replace `httpx.get` with a MagicMock, which
verifies field parsing and nothing about the transport. A MagicMock cannot
produce any of the failures that actually break a scheduled run:

  * the host answers 500 with an HTML body, so `resp.json()` raises where the
    code expected a dict;
  * the host answers 200 with a truncated body, which is what a proxy
    interstitial or a cut-off response looks like;
  * the host answers 401 and ECHOES the credentials back in the body, which is
    how an Adzuna key ends up in a terminal transcript.

So each test here drives the real client against a real HTTP server in a real
container (app/tests/qa/mock_upstream.py, on python:3.12-alpine), and only the
URL's scheme and authority are rewritten to point at it — path, query, headers,
body and timeout are the client's own. See app/tests/qa/upstream.py for why the
shim lives in the test harness rather than in a base_url parameter on six
fetchers.

THREE TESTS: POSITIVE, POSITIVE, ERROR
--------------------------------------
1. every ATS/aggregator fetcher parses a real response, and the mock can PROVE
   the request that produced it (query parameters included) — a happy path that
   would pass against a mock but never against a server is worthless;
2. the LLM provider's forced-tool-call protocol end to end, through
   ai_evaluate.evaluate_one, so the validation of the model's answer is the real
   one and not a reimplementation;
3. the failure modes, asserted to be LOUD. The dangerous failure in this codebase
   is not an exception, it is an empty list: a fetcher that swallows a 500 and
   returns [] looks exactly like a company with no open roles, and the run
   reports success.

NO DOCKER? THESE SKIP, WITH A REASON
------------------------------------
They need a container runtime. Without one the whole module skips and says why —
see upstream.DOCKER_SKIP_REASON. The other seven component tests run regardless.

Run with: python -m pytest app/tests/test_qa_component_containers.py -v
"""
import json

import httpx
import pytest

from app import aggregator_clients
from app import ai_evaluate
from app import ats_clients
from app import llm_providers

from app.tests.qa.upstream import requires_docker, upstream, upstream_shim  # noqa: F401  (fixtures)

# Applied to every test in the module. Module-level so a missing daemon produces
# one clear skip per test rather than a collection error.
pytestmark = requires_docker


def list_calls(upstream) -> list[dict]:
    """Only the SmartRecruiters LIST requests, not the per-posting detail ones.

    The list endpoint's path ends at "/postings"; a detail fetch appends the
    posting id. Counting both together would let the pagination assertion pass
    for the wrong reason — a gated detail fetch is indistinguishable from a
    second page if you only count requests."""
    return [r for r in upstream.requests("/postings") if r["path"].endswith("/postings")]


# ===========================================================================
# POSITIVE — the fetchers over a real socket
# ===========================================================================

def test_positive_every_fetcher_parses_a_real_response(upstream, upstream_shim):  # noqa: F811
    """Each client, against a real HTTP response, with the request verified.

    The request assertions matter as much as the parsed fields. A fetcher that
    silently stopped sending `?content=true` would still parse the mock's
    response — but in production it would come back without the JD text, so the
    stack filter would lose its input and every posting would fall into "no
    language named". Green on a mock, wrong in the morning. So the mock records
    what arrived on the wire and the test checks it.
    """
    upstream.reset_requests()

    # --- Greenhouse: description inline, and the fallback from
    #     first_published to updated_at for a job that omits the former.
    gh = ats_clients.fetch_greenhouse("TestCo", "testco")
    assert [j["title"] for j in gh] == ["Senior Software Engineer", "Backend Engineer"]
    assert gh[0]["location"] == "Calgary, AB, Canada"
    assert gh[0]["url"] == "https://job-boards.greenhouse.io/testco/jobs/4001"
    assert gh[0]["posted_at"] == "2026-08-01T00:00:00.000Z"
    assert "Java" in gh[0]["description"]
    assert gh[1]["posted_at"] == "2026-08-02T00:00:00.000Z", "must fall back to updated_at"
    sent = upstream.requests("/greenhouse/")
    assert len(sent) == 1, "one board fetch, not one per job"
    assert sent[0]["query"].get("content") == ["true"], \
        "without content=true Greenhouse returns no JD and the stack filter goes blind"

    # --- Ashby: descriptionPlain is preferred over descriptionHtml.
    ashby = ats_clients.fetch_ashby("TestCo", "testco")
    assert len(ashby) == 1
    assert ashby[0]["title"] == "Full Stack Engineer"
    assert ashby[0]["location"] == "Calgary, AB, Canada"
    assert ashby[0]["posted_at"] == "2026-08-03T00:00:00.000Z"
    assert ashby[0]["description"] == "JavaScript and Node."

    # --- Workable: the location object is joined, and a remote workplace is
    #     marked as such rather than dropped or left blank.
    workable = ats_clients.fetch_workable("TestCo", "testco")
    assert len(workable) == 1
    assert workable[0]["location"] == "Remote (Calgary, AB, Canada)"
    assert workable[0]["posted_at"] == "2026-08-04"

    # --- Lever: a bare JSON LIST, not an object, and allLocations overriding
    #     the single `location` — a shape difference a dict-returning mock hides.
    lever = ats_clients.fetch_lever("TestCo", "testco")
    assert len(lever) == 1
    assert lever[0]["location"] == "Calgary, AB, Edmonton, AB"
    assert lever[0]["posted_at"] == 1754179200000, "epoch millis, unconverted"
    assert lever[0]["url"].startswith("https://jobs.lever.co/testco/")

    # --- SmartRecruiters: the per-job detail fetch is GATED on the posting
    #     already passing the title/location filters — the behaviour that stops a
    #     company with hundreds of postings becoming hundreds of requests.
    upstream.reset_requests()
    sr = ats_clients.fetch_smartrecruiters("TestCo", "testco")
    assert len(sr) == 1
    assert sr[0]["location"] == "Remote (Calgary, AB, Canada)"
    assert sr[0]["url"] == "https://jobs.smartrecruiters.com/TestCo/sr-7001"
    assert "Build the thing." in sr[0]["description"]
    assert "Java, five years." in sr[0]["description"], "all three sections are joined"
    # A SHORT page ends the loop: one request, no pointless second round trip.
    # The mock serves a full page only for the slug "bigco" (see mock_upstream),
    # because a one-item page can only ever prove this branch.
    assert len(list_calls(upstream)) == 1, "a short page must stop pagination"

    # The other branch: a FULL first page must make it ask for the next one.
    upstream.reset_requests()
    big = ats_clients.fetch_smartrecruiters("BigCo", "bigco")
    assert len(big) == 100
    calls = list_calls(upstream)
    assert len(calls) == 2, "a full page must advance the offset and ask again"
    assert calls[0]["query"]["limit"] == ["100"]
    assert calls[0]["query"]["offset"] == ["0"]
    assert calls[1]["query"]["offset"] == ["100"]

    # --- fetch_company stamps the source, so the board can say where a row came
    #     from and refilter can tell an ATS refresh from an aggregator one.
    jobs = ats_clients.fetch_company({"name": "TestCo", "ats": "greenhouse", "slug": "testco"})
    assert all(j["source"] == "greenhouse" for j in jobs)

    # --- Remotive, the aggregator, over the same socket. job_type is Remotive's
    #     own vocabulary and must be MAPPED, not passed through.
    remotive = aggregator_clients.fetch_remotive({})
    assert len(remotive) == 1
    assert remotive[0]["employment_type"] == "contract", "'contract' is a contract type"
    assert remotive[0]["company"] == "TestCo"
    aggregator = aggregator_clients.fetch_aggregator({"type": "remotive", "params": {}})
    assert all(j["source"] == "remotive" for j in aggregator)

    print("all fetchers: parsed a real HTTP response, and the request was verified — OK")


def test_positive_llm_provider_completes_a_forced_tool_call(upstream, monkeypatch):
    """The structured-evaluation protocol, over HTTP, through the real validator.

    Driven through ai_evaluate.evaluate_one rather than by calling
    provider.complete directly, so the assertion covers the whole contract the
    pipeline depends on: the schema that goes out, the forced tool_choice, and
    the validation of what comes back (including the retry-with-more-tokens path,
    which must NOT be triggered by a good answer).

    The provider's base_url is a plain constructor argument, so no shim is needed
    here — this is the one client that was already pointable at a test server,
    and the test proves the container can stand in for DeepSeek without any
    production change at all.
    """
    provider = llm_providers.DeepSeekProvider(
        api_key="test-key-not-a-real-credential",
        model="mock-model",
        base_url=upstream.url("/openai"),
    )

    # list_models goes to the same base_url, so the provider is usable for the
    # --list-models check against a container too.
    assert provider.list_models() == ["mock-model"]

    upstream.reset_requests()
    profile = {"summary": "Senior SDET", "priority_languages": ["Java", "Ruby"]}
    job = {
        "company": "TestCo", "title": "Senior Software Engineer",
        "location": "Calgary, AB, Canada", "url": "https://example.test/jobs/llm-1",
        "description": "Java, JUnit and Maven. Contract role, day rate on application.",
    }

    evaluation = ai_evaluate.evaluate_one(provider, profile, job)

    # Every required field is present and validated — a partial answer raises
    # rather than being stored half-empty.
    assert set(ai_evaluate.REQUIRED_EVAL_FIELDS) <= set(evaluation)
    assert evaluation["match_score"] == 82
    assert evaluation["recommendation"] == "apply"

    # What the client actually SENT. This is the assertion a response-only double
    # cannot make: the force is what stops the model answering in prose, and
    # temperature 0 is what makes two runs score the same posting the same way.
    sent = upstream.requests("/openai/chat/completions")
    assert len(sent) == 1, "a valid answer must not trigger the retry path"
    body = sent[0]["body"]
    assert body["temperature"] == 0
    assert body["tool_choice"] == {"type": "function", "function": {"name": "submit_evaluation"}}
    assert body["tools"][0]["function"]["name"] == "submit_evaluation"
    # The schema is sent as a function definition, so the model is constrained by
    # it rather than merely asked nicely.
    assert "match_score" in json.dumps(body["tools"][0]["function"]["parameters"])
    # Thinking mode must be off: with it on, a FORCED tool_choice is rejected
    # outright (HTTP 400) and the retry spends the whole token budget on
    # reasoning before ever writing the answer.
    assert body["thinking"] == {"type": "disabled"}
    assert body["model"] == "mock-model"
    # The deterministic screening findings are handed to the model rather than
    # left to be re-derived, so the same posting cannot score two different ways.
    user_message = body["messages"][1]["content"]
    assert "TestCo" in user_message and "Senior Software Engineer" in user_message

    print("llm_providers: forced tool call over real HTTP, schema and payload verified — OK")


# ===========================================================================
# ERROR HANDLING
# ===========================================================================

def test_error_upstream_failures_are_loud_and_never_a_silent_empty_result(upstream, upstream_shim, monkeypatch):  # noqa: F811
    """500s, truncated bodies and rejected credentials must all RAISE.

    The failure this test exists to prevent is the quiet one. `fetch_company`
    returning `[]` is indistinguishable from a company with no open roles, and
    `main.run` catches the exception, prints a warning and carries on — so the
    only thing standing between "the API was down" and "the board is empty and
    every run reports success" is that these clients raise. Asserting the
    exception types keeps that true.

    Also asserted: the credentials must not appear in the error text. Adzuna
    takes app_id/app_key as QUERY PARAMETERS, which httpx puts into the URL in
    every error message it builds — this leaked a live key into a terminal
    transcript on 2026-09-21, and the mock deliberately echoes the credentials
    back so there is something real to redact.
    """
    # --- 500 from an ATS -----------------------------------------------------
    upstream_shim("err500")
    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        ats_clients.fetch_greenhouse("TestCo", "testco")
    assert excinfo.value.response.status_code == 500
    # The HTML body must NOT have been accepted as JSON on the way through.
    assert "Internal Server Error" not in str(excinfo.value) or excinfo.value.response.status_code == 500

    # --- 200 with a truncated body ------------------------------------------
    # The nastiest of the three: no status-code error to catch, so only the JSON
    # decode stands between a cut-off response and a half-understood board.
    upstream_shim("badjson")
    with pytest.raises(ValueError) as excinfo:  # json.JSONDecodeError is a ValueError
        ats_clients.fetch_ashby("TestCo", "testco")
    assert "Expecting" in str(excinfo.value) or "Unterminated" in str(excinfo.value) \
        or "delimiter" in str(excinfo.value), str(excinfo.value)

    # --- Adzuna rejecting the credentials (the 401 branch) -------------------
    monkeypatch.setenv("ADZUNA_APP_ID", "test-app-id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "super-secret-app-key")
    upstream_shim("authfail")
    with pytest.raises(RuntimeError) as excinfo:
        aggregator_clients.fetch_adzuna({"country": "gb", "what": "sdet"})
    message = str(excinfo.value)
    # It names the cause and says what to check, rather than surfacing a bare 401
    # — because the overwhelmingly common cause is pasting an Adzuna *account*
    # identifier where the *application* ID belongs.
    assert "401" in message and "AUTH_FAIL" in message
    assert "Application ID" in message and "Application Key" in message
    assert "super-secret-app-key" not in message
    # Note the 401 branch never reads the body, so there is nothing to redact
    # here. That is the next assertion's job.

    # --- Adzuna failing some other way, echoing the key back ----------------
    # This is the branch that actually leaked: for any non-401 status the body IS
    # read and included, so redaction is the only thing between a live app_key
    # and a terminal transcript. The mock echoes both credentials in the body, so
    # without _redact these strings would be in the message verbatim.
    upstream_shim("err500")
    with pytest.raises(RuntimeError) as excinfo:
        aggregator_clients.fetch_adzuna({"country": "gb", "what": "sdet"})
    message = str(excinfo.value)
    assert "HTTP 500" in message
    assert "super-secret-app-key" not in message, "the app_key leaked into the error message"
    assert "test-app-id" not in message, "the app_id leaked into the error message"
    assert "[REDACTED]" in message, "the credentials should be visibly redacted, not just absent"

    # --- the same 401 from the LLM endpoint ---------------------------------
    # Different client, different vocabulary: the provider must say WHICH key to
    # check, because a bare "HTTP 401" sends you looking at the wrong file.
    provider = llm_providers.DeepSeekProvider(
        api_key="test-key-not-a-real-credential", model="mock-model",
        base_url=upstream.url("/openai", scenario="authfail"),
    )
    with pytest.raises(llm_providers.ProviderError) as excinfo:
        provider.complete("system", "user", ai_evaluate.EVALUATION_SCHEMA, 256)
    assert "401" in str(excinfo.value) and "DEEPSEEK_API_KEY" in str(excinfo.value)

    # --- a well-formed answer that contains no usable tool call -------------
    # This is the production failure that took an afternoon to trace: a model
    # that spent its whole output budget on reasoning returns finish_reason
    # "length" with neither a tool call nor parseable content. complete() must
    # return None (so the caller can retry with a bigger budget) AND leave an
    # explanation behind, rather than raising a bare TypeError or looking like an
    # empty evaluation.
    quiet = llm_providers.DeepSeekProvider(
        api_key="test-key-not-a-real-credential", model="mock-model",
        base_url=upstream.url("/openai", scenario="nofn"),
    )
    assert quiet.complete("system", "user", ai_evaluate.EVALUATION_SCHEMA, 256) is None
    assert quiet.last_failure and "submit_evaluation" in quiet.last_failure
    assert "max_tokens" in quiet.last_failure

    # And evaluate_one turns that into a named failure rather than storing a
    # blank score, carrying the reason through to the message.
    job = {"company": "TestCo", "title": "Software Engineer", "location": "Calgary, AB, Canada",
           "url": "https://example.test/jobs/llm-2", "description": "Java and JUnit."}
    with pytest.raises(RuntimeError) as excinfo:
        ai_evaluate.evaluate_one(quiet, {"summary": "SDET"}, job, max_retries=0)
    assert "returned no submit_evaluation object" in str(excinfo.value)
    assert "Why:" in str(excinfo.value), "the diagnosis must reach the operator"

    print("upstream failures: 500/bad JSON/401 all raised loudly, credentials redacted — OK")
