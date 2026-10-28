import pytest


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Required suites may not turn skips, xfails or empty discovery into PASS."""
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    assert reporter is not None
    if session.testscollected == 0 or any(
        reporter.stats.get(status) for status in ("skipped", "xfailed", "xpassed")
    ):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
