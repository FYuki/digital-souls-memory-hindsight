from importlib.metadata import version

import pytest

pytestmark = pytest.mark.ut


def test_hindsight_client_is_pinned_release() -> None:
    """A1 / AC-O1: the official client is used unmodified at the fixed release."""
    import hindsight_client

    assert hindsight_client.__name__ == "hindsight_client"
    assert version("hindsight-client") == "1.1.0"
