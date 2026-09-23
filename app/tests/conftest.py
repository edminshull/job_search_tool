"""Shared pytest configuration for app/tests.

Installs the pinned, test-only filter configuration (see pinned_filters.py for
the full reasoning: the client tests use filters.py as a fetch gate, and
filters.yaml is user-editable config that the tests must not depend on) for
every test, and restores it afterwards via monkeypatch.

OPTING OUT: mark a test module with `pytestmark = pytest.mark.real_filters`
when the thing under test genuinely needs the LIVE filters.yaml — i.e. it is
about filter policy itself, or about a tool that reports on the user's real
market. Right now app/tests/test_build_uk_companies.py does this, because the
UK company builder counts UK postings and would see none at all under the
pinned Canada/Alberta config.

Note that this only covers the PYTEST run path. `python -m app.tests.<module>`
has no conftest and no monkeypatch, so those modules call
pinned_filters.apply() themselves in their __main__ blocks.
"""
import pytest

from app import filters
from app.tests import pinned_filters


def pytest_configure(config):
    """Register the marker (this repo has no pytest.ini/pyproject config, and
    an unregistered marker emits a PytestUnknownMarkWarning)."""
    config.addinivalue_line(
        "markers",
        "real_filters: run against the LIVE filters.yaml instead of the pinned "
        "test config (for tests of filter policy or of market-reporting tools)",
    )


@pytest.fixture(autouse=True)
def pinned_filters_config(request, monkeypatch):
    if request.node.get_closest_marker("real_filters"):
        return  # this module wants the live filters.yaml
    for attr, value in pinned_filters.compiled().items():
        monkeypatch.setattr(filters, attr, value)
