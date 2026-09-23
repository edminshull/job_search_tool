"""QA harness for the container-backed component tests.

Not a test package itself — `pytest` collects nothing from here. It holds the
machinery the two `test_qa_component*` modules share:

  mock_upstream.py  a stdlib HTTP server that stands in for every upstream the
                    pipeline talks to. Runs INSIDE a container; imports nothing
                    from this repo on purpose, so the container needs no mount
                    of app/ and cannot accidentally test a local copy.
  upstream.py       the testcontainers fixture, the Docker-availability probe
                    that turns "no daemon" into a clear skip rather than an
                    error, and the URL-rewriting shim that points the pipeline's
                    hard-coded production hosts at the container.

QA_REPORT.md at the repo root documents how to run both suites, what each test
protects, and how the mock upstream is driven.
"""
