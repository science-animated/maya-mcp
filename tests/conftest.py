"""Shared fixtures: a mocked maya.cmds for the handlers, and a fake Maya commandPort for the server."""
import base64
import importlib.machinery
import json
import re
import socket
import sys
import threading
import time
import types
from unittest import mock

import pytest

# maya_mcp_handlers does `import maya.cmds as cmds` at import time, so the stand-in
# has to be in sys.modules before any test imports it.
_maya = types.ModuleType("maya")
# Look like a real package, so imports and Python's traceback formatting work with it
_maya.__spec__ = importlib.machinery.ModuleSpec("maya", None, is_package=True)
_maya.__path__ = []
_cmds = mock.MagicMock(name="maya.cmds")
_open_maya_ui = mock.MagicMock(name="maya.OpenMayaUI")  # used by the screenshot handler
_maya.cmds = _cmds
_maya.OpenMayaUI = _open_maya_ui
sys.modules.setdefault("maya", _maya)
sys.modules.setdefault("maya.cmds", _cmds)
sys.modules.setdefault("maya.OpenMayaUI", _open_maya_ui)


@pytest.fixture
def cmds():
    """The mocked maya.cmds, reset for every test."""
    _cmds.reset_mock(return_value=True, side_effect=True)
    return _cmds


@pytest.fixture
def anyio_backend():
    return "asyncio"


_COMMAND_RE = re.compile(rb"b64decode\(b'([A-Za-z0-9+/=]*)'\)")


class FakeMaya:
    """A TCP server that behaves like Maya's commandPort for the code maya-mcp sends.

    It decodes the command, passes it to ``respond(command) -> dict`` and sends the
    JSON reply followed by Maya's ``\\n\\x00`` terminator. Like Maya, it serves one
    connection at a time. ``respond`` may also return ``None`` to close the
    connection without answering, or raise to simulate a crash.
    """

    def __init__(self, respond=None):
        self.respond = respond or (lambda cmd: {"status": "success", "result": {"echo": cmd}})
        self.commands = []
        self.connections = 0
        self.open_connections = 0
        self.max_open_connections = 0
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self.port = self._sock.getsockname()[1]
        self._lock = threading.Lock()
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with self._lock:
                self.connections += 1
                self.open_connections += 1
                self.max_open_connections = max(self.max_open_connections, self.open_connections)
            try:
                self._handle(conn)
            except OSError:
                pass  # the client gave up (e.g. timed out); Maya just moves on too
            finally:
                conn.close()
                with self._lock:
                    self.open_connections -= 1

    def _handle(self, conn):
        data = b""
        conn.settimeout(5)
        while not data.endswith(b".decode())"):
            chunk = conn.recv(65536)
            if not chunk:
                return
            data += chunk
        command = json.loads(base64.b64decode(_COMMAND_RE.search(data).group(1)))
        self.commands.append(command)
        reply = self.respond(command)
        if reply is None:
            return
        conn.sendall(json.dumps(reply).encode() + b"\n\x00")

    def close(self):
        self._stop = True
        self._sock.close()


@pytest.fixture
def fake_maya(monkeypatch):
    """Start a fake commandPort and point maya-mcp at it via MAYA_HOST/MAYA_PORT."""
    servers = []

    def start(respond=None):
        server = FakeMaya(respond)
        servers.append(server)
        monkeypatch.setenv("MAYA_HOST", "127.0.0.1")
        monkeypatch.setenv("MAYA_PORT", str(server.port))
        return server

    yield start
    for server in servers:
        server.close()


def free_port():
    """A local port with nothing listening on it."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def slow(seconds, reply):
    """A respond() function that answers after a delay."""
    def respond(command):
        time.sleep(seconds)
        return reply
    return respond
