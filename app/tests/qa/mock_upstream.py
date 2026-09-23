"""A stand-in for every HTTP upstream the pipeline talks to.

WHY THIS EXISTS
---------------
The pipeline's HTTP clients are tested today by replacing `httpx.get` with a
MagicMock that returns a canned response object (see app/tests/test_ats_clients.py).
That verifies the PARSING but nothing about the transport: a MagicMock never
opens a socket, never serialises a body, never sets a status code that
`raise_for_status()` actually reads, and cannot reproduce "the host answered 500
with an HTML error page" or "the host answered 200 with a truncated JSON body".
Those are the failures that actually happen in a scheduled run, and they are
invisible to a mock that returns whatever shape the test asked for.

This server is the other half: it is a real HTTP server, in a real container,
over a real socket, so the client's own retry/JSON/status handling is what gets
exercised. The parsing assertions the MagicMock tests already make stay there —
these tests are about the transport, not a duplicate of the parsing suite.

HOW IT IS DRIVEN
----------------
Every route is reachable under a SCENARIO PREFIX, so one container serves both
the happy path and every failure mode without any control-plane API:

    /ok/greenhouse/v1/boards/<slug>/jobs        200 + well-formed JSON
    /err500/...                                 500 + an HTML error page
    /badjson/...                                200 + a body that is not JSON
    /authfail/adzuna/...                        401 + Adzuna's AUTH_FAIL shape
    /slow/...                                   200, after a 2s delay
    /nofn/openai/chat/completions               200, no tool call, no JSON content

`upstream.py` builds the prefix; the client under test only ever sees the
rewritten URL. Nothing here knows what the pipeline expects, which is the point:
if a client starts tolerating a malformed body, the assertion that says so
belongs in the test, not in the fake.

RUNS ON THE STANDARD LIBRARY ONLY. python:3.x-alpine is the whole dependency
list, so the container starts in well under a second and there is no image build
step to keep in sync with requirements.txt.
"""
import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# Scenarios are the first path segment. Kept as a tuple rather than a set so the
# 404 message can list them in a stable, readable order.
SCENARIOS = ("ok", "err500", "badjson", "authfail", "slow", "nofn")
DEFAULT_SCENARIO = "ok"

SLOW_SECONDS = 2.0

# ---------------------------------------------------------------------------
# Canned upstream payloads.
#
# These mirror the real API shapes documented in ats_clients.py and
# aggregator_clients.py. Values are chosen so the PARSING can be asserted
# precisely — e.g. Lever's `allLocations` overrides `location`, SmartRecruiters'
# remote flag wraps the joined location — rather than being plausible filler.
# ---------------------------------------------------------------------------

GREENHOUSE_JOBS = {
    "jobs": [
        {
            "title": "Senior Software Engineer",
            "location": {"name": "Calgary, AB, Canada"},
            "absolute_url": "https://job-boards.greenhouse.io/testco/jobs/4001",
            "first_published": "2026-08-01T00:00:00.000Z",
            "content": "<p>Java, JUnit and Maven.</p>",
        },
        {
            # No first_published: the client falls back to updated_at. A source
            # that omits one of the two date fields entirely is normal.
            "title": "Backend Engineer",
            "location": {"name": "Edmonton, AB, Canada"},
            "absolute_url": "https://job-boards.greenhouse.io/testco/jobs/4002",
            "updated_at": "2026-08-02T00:00:00.000Z",
            "content": "<p>Ruby on Rails.</p>",
        },
    ]
}

ASHBY_JOBS = {
    "jobs": [
        {
            "title": "Full Stack Engineer",
            "location": "Calgary, AB, Canada",
            "jobUrl": "https://jobs.ashbyhq.com/testco/ashby-5001",
            "publishedAt": "2026-08-03T00:00:00.000Z",
            "descriptionPlain": "JavaScript and Node.",
        }
    ]
}

WORKABLE_JOBS = {
    "jobs": [
        {
            "title": "Software Engineer",
            "location": {"city": "Calgary", "region": "AB", "country": "Canada", "workplace": "remote"},
            "url": "https://apply.workable.com/testco/j/workable-6001/",
            "published_on": "2026-08-04",
            "description": "Java and Kubernetes.",
        }
    ]
}

# Lever returns a bare LIST, not an object — a shape difference a mock that
# always returns a dict would hide.
LEVER_POSTINGS = [
    {
        "text": "Backend Engineer",
        "categories": {"location": "Calgary, AB", "allLocations": ["Calgary, AB", "Edmonton, AB"]},
        "hostedUrl": "https://jobs.lever.co/testco/11111111-2222-3333-4444-555555555555",
        "createdAt": 1754179200000,  # epoch millis
        "descriptionPlain": "Java and Spring.",
    }
]

SMARTRECRUITERS_POSTINGS = {
    "content": [
        {
            "id": "sr-7001",
            "name": "Software Engineer",
            "location": {"city": "Calgary", "region": "AB", "country": "Canada", "remote": True},
            "applyUrl": "https://jobs.smartrecruiters.com/TestCo/sr-7001",
        }
    ]
}

# A FULL page (the client asks for limit=100), served for the slug "bigco". The
# client stops as soon as a page comes back SHORT, so observing a second request
# at all requires the first page to be exactly `limit` long — a mock that returns
# one posting and then an empty page only ever proves the early stop.
SMARTRECRUITERS_FULL_PAGE_SIZE = 100


def _smartrecruiters_full_page() -> dict:
    return {
        "content": [
            {
                "id": f"big-{i}",
                "name": f"Backend Engineer {i}",
                "location": {"city": "Calgary", "region": "AB", "country": "Canada", "remote": False},
                "applyUrl": f"https://jobs.smartrecruiters.com/BigCo/big-{i}",
            }
            for i in range(SMARTRECRUITERS_FULL_PAGE_SIZE)
        ]
    }

SMARTRECRUITERS_DETAIL = {
    "jobAd": {
        "sections": {
            "jobDescription": {"text": "Build the thing."},
            "qualifications": {"text": "Java, five years."},
            "additionalInformation": {"text": "Hybrid working."},
        }
    }
}

ADZUNA_RESULTS = {
    "results": [
        {
            "title": "Software Engineer",
            "location": {"display_name": "Calgary, AB"},
            "redirect_url": "https://api.adzuna.com/land/ad/9001",
            "description": "A short snippet that is truncated…",
            "contract_type": "contract",
            "created": "2026-08-05T00:00:00Z",
            "company": {"display_name": "TestCo"},
            "salary_min": 130000,
            "salary_max": 156000,
            "salary_is_predicted": "0",
        }
    ]
}

REMOTIVE_JOBS = {
    "jobs": [
        {
            "company_name": "TestCo",
            "title": "Backend Engineer",
            "candidate_required_location": "Canada",
            "url": "https://remotive.com/remote-jobs/9001",
            "job_type": "contract",
            "publication_date": "2026-08-06T00:00:00",
        }
    ]
}

# ---------------------------------------------------------------------------
# The OpenAI-compatible endpoint.
#
# The tool NAME is not hard-coded: the mock answers with whatever function the
# request actually asked for. Hard-coding "submit_evaluation" would make the
# test pass even if the client sent the wrong schema, which is the one thing a
# forced-tool-call test should catch.
# ---------------------------------------------------------------------------

EVALUATION_ARGUMENTS = {
    "match_score": 82,
    "recommendation": "apply",
    "genuine_gaps": "No production Kotlin, and the advert asks for it.",
    "transferable_strengths": "Java SDET background transfers directly to the automation stack.",
    "risk_factors": "Contract role; IR35 status is not stated in the posting.",
}


def _tool_response(tool_name: str) -> dict:
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "model": "mock-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_mock",
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(EVALUATION_ARGUMENTS),
                            },
                        }
                    ],
                },
            }
        ],
    }


def _requested_tool_name(payload: dict) -> str:
    """The function the caller forced, or the first one it offered.

    Read from `tool_choice` first because a forced call is what the pipeline is
    supposed to send; falling back to `tools` means an unforced request still
    gets a usable answer instead of a confusing 500 from the fake."""
    choice = payload.get("tool_choice")
    if isinstance(choice, dict):
        name = (choice.get("function") or {}).get("name")
        if name:
            return name
    for tool in payload.get("tools") or []:
        name = (tool.get("function") or {}).get("name")
        if name:
            return name
    return "submit_evaluation"


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

GREENHOUSE_RE = re.compile(r"^/greenhouse/v1/boards/(?P<slug>[^/]+)/jobs$")
ASHBY_RE = re.compile(r"^/ashby/posting-api/job-board/(?P<slug>[^/]+)$")
WORKABLE_RE = re.compile(r"^/workable/api/v1/widget/accounts/(?P<slug>[^/]+)$")
LEVER_RE = re.compile(r"^/lever/v0/postings/(?P<slug>[^/]+)$")
SR_LIST_RE = re.compile(r"^/smartrecruiters/v1/companies/(?P<slug>[^/]+)/postings$")
SR_DETAIL_RE = re.compile(r"^/smartrecruiters/v1/companies/(?P<slug>[^/]+)/postings/(?P<pid>[^/]+)$")
ADZUNA_RE = re.compile(r"^/adzuna/v1/api/jobs/(?P<country>[a-z]{2})/search/(?P<page>\d+)$")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # Every request is recorded so a test can assert what the client actually
    # SENT (the forced tool_choice, temperature, the paginated query params) and
    # not only what it made of the answer. That is the half a response-only
    # double cannot see.
    requests: list[dict] = []

    def log_message(self, fmt, *args):  # keep the container's stdout for the health line only
        pass

    # -- helpers ----------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json")

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"__unparseable__": raw.decode("utf-8", "replace")}

    def _record(self, path: str, scenario: str, query: dict, body: dict) -> None:
        Handler.requests.append({
            "path": path,
            "scenario": scenario,
            "query": query,
            "body": body,
            "headers": dict(self.headers),
        })

    # -- dispatch ---------------------------------------------------------
    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        body = self._read_body() if method == "POST" else {}

        if parsed.path == "/__health":
            self._send_json(200, {"ok": True, "scenarios": list(SCENARIOS)})
            return
        if parsed.path == "/__requests":
            self._send_json(200, {"requests": Handler.requests})
            return
        if parsed.path == "/__reset":
            Handler.requests.clear()
            self._send_json(200, {"ok": True})
            return

        head, _, tail = parsed.path.lstrip("/").partition("/")
        if head in SCENARIOS:
            scenario, path = head, "/" + tail
        else:
            # No prefix given: treat it as the happy path so the server is also
            # usable by hand with curl during debugging.
            scenario, path = DEFAULT_SCENARIO, parsed.path
        self._record(path, scenario, query, body)

        if scenario == "err500":
            if path.startswith("/adzuna/") and query.get("app_key"):
                # An error body that ECHOES the credentials, which real APIs do.
                # Adzuna takes app_id/app_key as query parameters, so this is the
                # shape in which a live key ends up in a log line — see
                # aggregator_clients._redact and the 401 branch it does NOT cover.
                self._send_json(500, {
                    "error": "INTERNAL_ERROR",
                    "app_id": (query.get("app_id") or [""])[0],
                    "app_key": (query.get("app_key") or [""])[0],
                })
                return
            self._send(500, b"<html><body>Internal Server Error</body></html>", "text/html")
            return
        if scenario == "badjson":
            # 200 with a body that starts like JSON and is not: the shape a
            # truncated response or an HTML interstitial actually arrives in.
            self._send(200, b'{"jobs": [{"title": "Software Eng', "application/json")
            return
        if scenario == "authfail":
            # The credentials are ECHOED BACK, which is what many real APIs do
            # and is the whole reason aggregator_clients._redact exists: Adzuna
            # takes app_id/app_key as QUERY PARAMETERS, so anything that prints
            # the URL, the body or an httpx error puts a live key on screen. A
            # 401 body that stayed silent about them would make the redaction
            # untestable, because there would be nothing to redact.
            payload = {"error": "AUTH_FAIL"}
            if path.startswith("/adzuna/"):
                payload["app_id"] = (query.get("app_id") or [""])[0]
                payload["app_key"] = (query.get("app_key") or [""])[0]
            self._send_json(401, payload)
            return
        if scenario == "slow":
            time.sleep(SLOW_SECONDS)

        self._route(path, query, body, scenario)

    def _route(self, path: str, query: dict, body: dict, scenario: str) -> None:
        if path == "/openai/models":
            self._send_json(200, {"data": [{"id": "mock-model"}]})
            return
        if path == "/openai/chat/completions":
            if scenario == "nofn":
                # A well-formed completion whose message carries neither a tool
                # call nor parseable content — what a thinking model that spent
                # its whole max_tokens on reasoning actually returns.
                self._send_json(200, {
                    "choices": [{
                        "finish_reason": "length",
                        "message": {"role": "assistant", "content": "I will now think about this posting in detail",
                                    "reasoning_content": "x" * 400},
                    }]
                })
                return
            self._send_json(200, _tool_response(_requested_tool_name(body)))
            return

        if GREENHOUSE_RE.match(path):
            self._send_json(200, GREENHOUSE_JOBS)
            return
        if ASHBY_RE.match(path):
            self._send_json(200, ASHBY_JOBS)
            return
        if WORKABLE_RE.match(path):
            self._send_json(200, WORKABLE_JOBS)
            return
        if LEVER_RE.match(path):
            self._send_json(200, LEVER_POSTINGS)
            return
        if SR_DETAIL_RE.match(path):
            self._send_json(200, SMARTRECRUITERS_DETAIL)
            return
        if SR_LIST_RE.match(path):
            # Paginate the way the real API does, and cover BOTH exit branches of
            # the client's loop (`if len(content) < limit: break`):
            #   slug "bigco"  -> a full page, then an empty one: two requests
            #   anything else -> one short page: one request, loop stops early
            slug = SR_LIST_RE.match(path).group("slug")
            offset = int((query.get("offset") or ["0"])[0])
            if slug == "bigco":
                self._send_json(200, _smartrecruiters_full_page() if offset == 0 else {"content": []})
            else:
                self._send_json(200, SMARTRECRUITERS_POSTINGS if offset == 0 else {"content": []})
            return
        if ADZUNA_RE.match(path):
            self._send_json(200, ADZUNA_RESULTS)
            return
        if path == "/remotive/api/remote-jobs":
            self._send_json(200, REMOTIVE_JOBS)
            return

        self._send_json(404, {"error": f"mock upstream has no route for {path}"})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


def main(argv: list[str]) -> int:
    port = 8080
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    # Printed so testcontainers' wait_for_logs can block on readiness instead of
    # the test polling and hoping.
    print(f"mock upstream ready on 0.0.0.0:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
