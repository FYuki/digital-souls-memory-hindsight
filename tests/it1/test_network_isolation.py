import json
import os
import subprocess
import sys
from urllib.parse import urlsplit

import pytest

pytestmark = pytest.mark.it1


def test_internal_connection_succeeds_and_external_tcp_is_blocked() -> None:
    database = urlsplit(os.environ["DATABASE_URL"])
    assert database.hostname is not None and database.port is not None
    probe = """
import errno
import json
import socket
import sys

assert 'pytest_socket' not in sys.modules
with socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=5):
    pass
results = []
for address in ('1.1.1.1', '8.8.8.8'):
    try:
        with socket.create_connection((address, 443), timeout=3):
            results.append({'address': address, 'blocked': False})
    except OSError as error:
        if not isinstance(error, TimeoutError) and error.errno not in (
            errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ETIMEDOUT
        ):
            raise
        results.append({'address': address, 'blocked': True})
print(json.dumps(results))
"""
    result = subprocess.run(
        [sys.executable, "-c", probe, database.hostname, str(database.port)],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        {"address": "1.1.1.1", "blocked": True},
        {"address": "8.8.8.8", "blocked": True},
    ]
