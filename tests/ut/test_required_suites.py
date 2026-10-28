from pathlib import Path

import pytest

pytest_plugins = ["pytester"]
pytestmark = pytest.mark.ut


@pytest.mark.parametrize(
    ("source", "arguments", "expected"),
    [
        ("def test_ok(): pass", [], pytest.ExitCode.OK),
        ("def test_fail(): assert False", [], pytest.ExitCode.TESTS_FAILED),
        (
            "import pytest\ndef test_skip(): pytest.skip('required')",
            [],
            pytest.ExitCode.TESTS_FAILED,
        ),
        (
            "import pytest\n@pytest.mark.xfail\ndef test_xfail(): assert False",
            [],
            pytest.ExitCode.TESTS_FAILED,
        ),
        (
            "import pytest\n@pytest.mark.xfail\ndef test_xpass(): pass",
            [],
            pytest.ExitCode.TESTS_FAILED,
        ),
        ("", [], pytest.ExitCode.TESTS_FAILED),
        ("def test_ok(): pass", ["-m", "ut"], pytest.ExitCode.TESTS_FAILED),
    ],
    ids=["pass", "fail", "skip", "xfail", "xpass", "empty", "deselected"],
)
def test_required_suite_exit_status(
    pytester: pytest.Pytester, source: str, arguments: list[str], expected: pytest.ExitCode
) -> None:
    pytester.makeconftest((Path(__file__).parents[1] / "conftest.py").read_text())
    pytester.makepyfile(source)
    result = pytester.runpytest_subprocess("-q", *arguments)
    assert result.ret == expected
