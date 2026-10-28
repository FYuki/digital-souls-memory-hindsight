import socket

import pytest
from pytest_socket import SocketBlockedError  # type: ignore[import-untyped]

pytestmark = pytest.mark.ut


def test_default_unit_test_policy_blocks_network_sockets() -> None:
    with pytest.raises(SocketBlockedError):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)
