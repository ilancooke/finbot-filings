"""Mounted test-only boundaries; never installed in the production image.

Python imports this shim before the unchanged production module entry point.
Even subprocess health/help tests get their own network guard.
"""

import os
import socket


def forbidden(*args, **kwargs):
    raise AssertionError("live network access is forbidden in the lifecycle fixture")


socket.socket.connect = forbidden
socket.socket.connect_ex = forbidden
socket.create_connection = forbidden

if os.environ.get("FINBOT_TEST_SCENARIO"):
    # Fail closed if fixture initialization fails: sitecustomize exceptions would
    # otherwise be printed and ignored by Python's startup machinery.
    try:
        from runtime_fixture import install
        install()
    except BaseException:
        import traceback
        traceback.print_exc()
        os._exit(70)
