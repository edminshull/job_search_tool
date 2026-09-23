"""The container-backed upstream: fixture, Docker probe, and the URL shim.

THE PROBLEM THIS SOLVES
-----------------------
Every upstream host the pipeline talks to is hard-coded at the call site:

    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    resp = httpx.get(url, ...)

There is no base_url parameter to inject, so a test cannot point
`fetch_greenhouse` at a container without either editing production code or
intercepting the request. Editing the clients to accept an override would be the
tidier fix, but it puts a test-only seam into six fetchers and invites exactly
the kind of "configurable in tests, wrong in production" drift this repo
elsewhere refuses to accept (see the comments on the pinned filter config).

So the seam lives here instead, and it stays HONEST: `_rewrite` changes the
scheme and authority of the URL and nothing else. The client's own path, query
parameters, headers, timeout and body are untouched, `httpx` still opens a real
socket, and the response goes back through the client's real `raise_for_status`/
`.json()` handling. Only the address is different. That is what makes these tests
worth having over the MagicMock doubles in test_ats_clients.py — those cannot
produce "the host answered 500 with an HTML body", which is a failure that
happens in real runs.

The shim replaces `httpx.get`/`httpx.post` globally, which is the same seam the
existing client tests already use (`patch("httpx.get", ...)`), just pointed at a
real listener. It is restored by monkeypatch at the end of each test.

NO DOCKER? THE SUITE SKIPS, IT DOES NOT FAIL
--------------------------------------------
These tests need a container runtime. On a machine without one the right answer
is a skip naming the reason, not a red test that says nothing about the code. See
`requires_docker` and `docker_available` — the probe is cached per session so a
missing daemon costs one `docker info` call, not one per test.
"""
import functools
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest

qa_dir = Path(__file__).resolve().parent
MOCK_UPSTREAM_PATH = qa_dir / "mock_upstream.py"

# A small, pinned image so a run is reproducible and the pull is ~50 MB rather
# than the ~1 GB a node/java mock-server image costs. The mock needs nothing but
# the standard library — see mock_upstream.py.
IMAGE = "python:3.12-alpine"
CONTAINER_PORT = 8080
MOUNT_TARGET = "/srv"

# How long to wait for the mock's readiness line. Generous on purpose: a cold
# machine may still be pulling the image on the first run.
STARTUP_TIMEOUT = 60

DOCKER_SKIP_REASON = (
    "needs a Docker daemon (or QA_UPSTREAM_URL pointing at a mock already "
    "running) and neither is available — start Docker Desktop, or run "
    "`python app/tests/qa/mock_upstream.py --port 8123` and set "
    "QA_UPSTREAM_URL=http://127.0.0.1:8123. These are the only tests that "
    "exercise the HTTP clients over a real socket; the rest of app/tests runs "
    "without Docker."
)

# Host -> the path prefix the container serves that upstream under. The path
# prefixes must match mock_upstream.Handler's routes.
UPSTREAM_HOSTS = {
    "boards-api.greenhouse.io": "/greenhouse",
    "api.ashbyhq.com": "/ashby",
    "apply.workable.com": "/workable",
    "api.lever.co": "/lever",
    "api.smartrecruiters.com": "/smartrecruiters",
    "jobs.smartrecruiters.com": "/smartrecruiters",
    "api.adzuna.com": "/adzuna",
    "remotive.com": "/remotive",
}

_docker_available: bool | None = None


def docker_available() -> bool:
    """Whether a Docker daemon is actually reachable, not merely installed.

    `docker info` is the only check that distinguishes the two. A machine with
    the CLI present but the daemon down is the common case on macOS (Docker
    Desktop not started, or started after the shell), and it is exactly the case
    testcontainers' own error message buries under a connection traceback.

    Cached because it costs a process spawn and its answer cannot change
    mid-session in a way worth re-checking per test."""
    global _docker_available
    if _docker_available is not None:
        return _docker_available
    if os.environ.get("QA_SKIP_DOCKER_TESTS"):
        # An explicit opt-out for CI jobs that should stay fast and offline.
        _docker_available = False
        return False
    if shutil.which("docker") is None:
        _docker_available = False
        return False
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        _docker_available = False
        return False
    _docker_available = proc.returncode == 0 and bool(proc.stdout.strip())
    return _docker_available


def external_upstream_url() -> str | None:
    """A mock upstream already running somewhere, bypassing the container.

    Exists so the assertions in the container tests can be iterated on — and
    verified on a machine with no Docker at all — without going through an image
    pull and a volume mount for every edit:

        python app/tests/qa/mock_upstream.py --port 8123 &
        QA_UPSTREAM_URL=http://127.0.0.1:8123 python -m pytest app/tests/test_qa_component_containers.py

    It runs the SAME server from the SAME file, so what it verifies is the
    assertions and the routes. What it does NOT verify is the container plumbing
    itself (image, mount, port mapping) — only a real Docker run does that."""
    return os.environ.get("QA_UPSTREAM_URL") or None


def upstream_available() -> bool:
    """Whether these tests can run at all, by either route.

    Checked rather than assumed so the skip reason can name both options; a
    message that only says "Docker" would send someone with a perfectly good
    local mock looking for the wrong problem."""
    return bool(external_upstream_url()) or docker_available()


requires_docker = pytest.mark.skipif(not upstream_available(), reason=DOCKER_SKIP_REASON)


@dataclass
class Upstream:
    """A running mock upstream, plus the tools to aim the pipeline at it."""

    base_url: str
    container: object

    def url(self, path: str, scenario: str = "ok") -> str:
        """A full URL into the mock, for the tests that call it directly rather
        than through a client (the LLM provider takes a base_url)."""
        return f"{self.base_url}/{scenario}{path}"

    def requests(self, path_contains: str | None = None) -> list[dict]:
        """What the mock has been asked for since the last reset.

        The record is on the server, not in the test process, so it captures the
        request as it arrived on the wire — including the body the client
        actually serialised."""
        resp = httpx.get(f"{self.base_url}/__requests", timeout=10.0)
        resp.raise_for_status()
        found = resp.json()["requests"]
        if path_contains is not None:
            found = [r for r in found if path_contains in r["path"]]
        return found

    def reset_requests(self) -> None:
        httpx.get(f"{self.base_url}/__reset", timeout=10.0).raise_for_status()


@pytest.fixture(scope="session")
def upstream():
    """Start the mock upstream once for the whole session.

    Session-scoped because the container takes seconds to start and pull, while
    each test is milliseconds of HTTP. The mock is stateless apart from its
    request log, which `reset_requests` clears, so sharing it cannot leak state
    between tests in a way that changes an outcome."""
    external = external_upstream_url()
    if external:
        # An already-running mock (see external_upstream_url). Verified before
        # use, so a wrong port fails here with a clear message rather than as a
        # connection error inside an assertion.
        mock = Upstream(base_url=external.rstrip("/"), container=None)
        health = httpx.get(f"{mock.base_url}/__health", timeout=15.0)
        health.raise_for_status()
        assert json.loads(health.text)["ok"] is True
        yield mock
        return

    if not docker_available():
        pytest.skip(DOCKER_SKIP_REASON)

    # Imported here, not at module scope, so that collecting the file on a
    # machine with no Docker installed does not fail on the testcontainers
    # import itself — the skip above is the intended outcome, not an ImportError.
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.wait_strategies import CompositeWaitStrategy, HttpWaitStrategy, LogMessageWaitStrategy

    container = DockerContainer(IMAGE)
    container.with_exposed_ports(CONTAINER_PORT)
    container.with_volume_mapping(str(MOCK_UPSTREAM_PATH), f"{MOUNT_TARGET}/mock_upstream.py", "ro")
    container.with_command(f"python3 {MOUNT_TARGET}/mock_upstream.py --port {CONTAINER_PORT}")
    # Wait for readiness rather than sleeping and hoping, and wait on BOTH
    # signals: the log line proves the process started, and the HTTP probe proves
    # it is serving. A log line alone can be printed before the socket accepts,
    # which shows up as an intermittent connection error in the first test.
    container.waiting_for(
        CompositeWaitStrategy(
            LogMessageWaitStrategy("mock upstream ready", times=1),
            HttpWaitStrategy(CONTAINER_PORT, path="/__health"),
        ).with_startup_timeout(STARTUP_TIMEOUT)
    )
    container.start()
    try:
        port = container.get_exposed_port(CONTAINER_PORT)
        mock = Upstream(base_url=f"http://127.0.0.1:{port}", container=container)
        # Prove the port is genuinely answering before handing it to a test, so
        # a container that started but never served fails HERE with a clear
        # message instead of as a confusing connection error in an assertion.
        health = httpx.get(f"{mock.base_url}/__health", timeout=15.0)
        health.raise_for_status()
        assert json.loads(health.text)["ok"] is True
        yield mock
    finally:
        container.stop()


def rewrite_url(url: str, base_url: str, scenario: str = "ok") -> str:
    """Point one of the pipeline's hard-coded hosts at the container.

    Unknown hosts are returned untouched, so a test that forgets to add a host
    above fails with a real DNS/connection error naming the ORIGINAL url rather
    than silently talking to nothing."""
    parts = urlsplit(url)
    prefix = UPSTREAM_HOSTS.get(parts.hostname or "")
    if prefix is None:
        return url
    host = urlsplit(base_url)
    port = host.port or 80
    return urlunsplit(("http", f"{host.hostname}:{port}", f"/{scenario}{prefix}{parts.path}",
                       parts.query, parts.fragment))


@pytest.fixture
def upstream_shim(upstream, monkeypatch):
    """Aim `httpx.get`/`httpx.post` at the container, per scenario.

    Yields a callable: `shim("err500")` makes every subsequent request through
    either client land on the error scenario. The default is the happy path, so
    a test that only cares about parsing can ignore it entirely.

    Both methods on the `httpx` module are replaced, and that covers all three
    client modules: they each do `import httpx` and then call `httpx.get(...)`,
    which is an attribute lookup on the same module object at call time."""
    state = {"scenario": "ok"}

    def install(original):
        @functools.wraps(original)
        def wrapper(url, *args, **kwargs):
            return original(rewrite_url(url, upstream.base_url, state["scenario"]), *args, **kwargs)
        return wrapper

    monkeypatch.setattr(httpx, "get", install(httpx.get))
    monkeypatch.setattr(httpx, "post", install(httpx.post))

    def shim(scenario: str = "ok") -> None:
        assert scenario in ("ok", "err500", "badjson", "authfail", "slow", "nofn"), \
            f"unknown scenario {scenario!r}"
        state["scenario"] = scenario

    return shim
