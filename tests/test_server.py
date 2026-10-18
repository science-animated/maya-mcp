"""Tests for the MCP server (src/maya_mcp/server.py) against a fake Maya commandPort."""
import json
import os
import socket
import sys
import tempfile
import threading

import pytest

from conftest import free_port, slow
from maya_mcp import server
from maya_mcp.server import MayaConnection, get_maya_connection, mcp

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def text(result):
    return "".join(c.text for c in result.content if c.type == "text")


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def test_send_command_round_trip(fake_maya):
    maya = fake_maya()
    result = get_maya_connection().send_command("get_transform", {"name": "pCube1"})
    assert maya.commands == [{"type": "get_transform", "params": {"name": "pCube1"}}]
    assert result == {"echo": {"type": "get_transform", "params": {"name": "pCube1"}}}


def test_non_ascii_parameters_survive(fake_maya):
    maya = fake_maya()
    get_maya_connection().send_command("rename_object", {"name": "a", "new_name": "Würfel_立方体"})
    assert maya.commands[0]["params"]["new_name"] == "Würfel_立方体"


def test_each_command_uses_its_own_connection(fake_maya):
    maya = fake_maya()
    for _ in range(5):
        get_maya_connection().send_command("get_scene_info")
    assert maya.connections == 5
    assert maya.open_connections == 0      # nothing is left open between commands


def test_parallel_commands_all_get_their_own_answer(fake_maya):
    fake_maya(lambda cmd: {"status": "success", "result": cmd["params"]["i"]})
    results, errors = {}, []

    def call(i):
        try:
            results[i] = get_maya_connection().send_command("get_object_info", {"i": i})
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=call, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert results == {i: i for i in range(20)}


def test_maya_error_raises_runtime_error(fake_maya):
    fake_maya(lambda cmd: {"status": "error", "message": "Object not found: x"})
    with pytest.raises(RuntimeError, match="Object not found: x"):
        get_maya_connection().send_command("get_object_info", {"name": "x"})


def test_nothing_listening(monkeypatch):
    monkeypatch.setenv("MAYA_PORT", str(free_port()))
    with pytest.raises(ConnectionError, match="nothing is listening on 127.0.0.1"):
        get_maya_connection().send_command("get_scene_info")


def test_connect_timeout(monkeypatch):
    def timeout(*args, **kwargs):
        raise socket.timeout("timed out")
    monkeypatch.setattr(server.socket, "create_connection", timeout)
    with pytest.raises(ConnectionError, match="timed out after 3s. The port is busy"):
        MayaConnection("127.0.0.1", 9877).send_command("get_scene_info")


def test_unreachable_host(monkeypatch):
    def unreachable(*args, **kwargs):
        raise OSError(65, "No route to host")
    monkeypatch.setattr(server.socket, "create_connection", unreachable)
    with pytest.raises(ConnectionError, match="is unreachable"):
        MayaConnection("10.0.0.1", 9877).send_command("get_scene_info")


def test_reply_timeout_does_not_leak_into_next_command(fake_maya):
    replies = iter([slow(1.0, {"status": "success", "result": "late"}),
                    lambda cmd: {"status": "success", "result": "fresh"}])
    fake_maya(lambda cmd: next(replies)(cmd))
    conn = get_maya_connection()
    with pytest.raises(ConnectionError, match="did not answer 'get_scene_info' within 0s"):
        conn.send_command("get_scene_info", timeout=0.2)
    assert conn.send_command("get_scene_info") == "fresh"


def test_connection_closed_without_reply(fake_maya):
    fake_maya(lambda cmd: None)
    with pytest.raises(ConnectionError, match="closed without sending a response"):
        get_maya_connection().send_command("get_scene_info")


def test_default_host_is_ipv4(monkeypatch):
    monkeypatch.delenv("MAYA_HOST", raising=False)
    monkeypatch.delenv("MAYA_PORT", raising=False)
    conn = get_maya_connection()
    assert (conn.host, conn.port) == ("127.0.0.1", 9877)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_lists_all_tools():
    tools = await mcp.list_tools()
    assert len(tools) == 23
    image_tools = {t.name: t for t in tools if t.name in ("get_viewport_screenshot", "render_scene")}
    assert set(image_tools) == {"get_viewport_screenshot", "render_scene"}
    assert all(t.output_schema is None for t in image_tools.values())


@pytest.mark.anyio
async def test_tool_returns_json(fake_maya):
    fake_maya(lambda cmd: {"status": "success", "result": {"object_count": 4}})
    result = await mcp.call_tool("get_scene_info", {})
    assert json.loads(text(result)) == {"object_count": 4}


@pytest.mark.anyio
async def test_tool_passes_arguments(fake_maya):
    maya = fake_maya()
    await mcp.call_tool("set_transform", {"name": "pCube1", "translation": [1, 2, 3]})
    assert maya.commands == [{"type": "set_transform", "params": {"name": "pCube1", "translation": [1, 2, 3]}}]


@pytest.mark.anyio
async def test_tool_errors_are_text(fake_maya):
    fake_maya(lambda cmd: {"status": "error", "message": "Traceback ...\nZeroDivisionError: division by zero"})
    result = await mcp.call_tool("execute_maya_code", {"code": "1/0"})
    assert text(result).startswith("Error: Traceback")


@pytest.mark.anyio
async def test_tool_when_maya_is_not_running(monkeypatch):
    monkeypatch.setenv("MAYA_PORT", str(free_port()))
    result = await mcp.call_tool("get_scene_info", {})
    assert text(result).startswith("Error: Could not connect to Maya: nothing is listening")


@pytest.mark.anyio
@pytest.mark.parametrize("tool, key, args", [
    ("get_viewport_screenshot", "filepath", {"max_size": 400}),
    ("render_scene", "rendered_path", {"width": 64, "height": 64}),
])
async def test_image_tools_return_png_and_clean_up(fake_maya, tool, key, args):
    folder = tempfile.mkdtemp(prefix="maya_mcp_")
    path = os.path.join(folder, "image.png")
    with open(path, "wb") as f:
        f.write(PNG)
    fake_maya(lambda cmd: {"status": "success", "result": {key: path}})
    result = await mcp.call_tool(tool, args)
    images = [c for c in result.content if c.type == "image"]
    assert len(images) == 1 and images[0].mime_type == "image/png"
    assert not os.path.exists(folder)


@pytest.mark.anyio
async def test_render_keeps_file_written_to_output_path(fake_maya, tmp_path):
    out = tmp_path / "render.png"
    out.write_bytes(PNG)
    fake_maya(lambda cmd: {"status": "success", "result": {"rendered_path": str(out)}})
    result = await mcp.call_tool("render_scene", {"output_path": str(out)})
    assert [c.type for c in result.content] == ["image"]
    assert out.exists()


@pytest.mark.anyio
@pytest.mark.parametrize("tool", ["get_viewport_screenshot", "render_scene"])
async def test_image_tools_report_missing_file(fake_maya, tool):
    fake_maya(lambda cmd: {"status": "success", "result": {"filepath": "/nonexistent/x.png",
                                                           "rendered_path": "/nonexistent/x.png"}})
    result = await mcp.call_tool(tool, {})
    assert text(result).startswith("Error:") and "not found" in text(result)


# ---------------------------------------------------------------------------
# Full protocol: start the server as a subprocess and talk to it over stdio
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_stdio_end_to_end(fake_maya):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    maya = fake_maya(lambda cmd: {"status": "success", "result": {"scene_name": "untitled"}})
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from maya_mcp import main; main()"],
        env={**os.environ, "MAYA_HOST": "127.0.0.1", "MAYA_PORT": str(maya.port)},
    )
    with open(os.devnull, "w") as devnull:
        async with stdio_client(params, errlog=devnull) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                assert init.server_info.name == "MayaMCP"
                from maya_mcp import __version__
                assert init.server_info.version == __version__
                assert len((await session.list_tools()).tools) == 23
                result = await session.call_tool("get_scene_info", {})
                assert json.loads(text(result)) == {"scene_name": "untitled"}
    assert maya.commands[-1]["type"] == "get_scene_info"


# ---------------------------------------------------------------------------
# Contract: every tool must reach a handler that accepts exactly what it sends
# ---------------------------------------------------------------------------

_DUMMY = {"string": "x", "integer": 1, "number": 1.0, "boolean": True, "array": ["x"]}


def _all_arguments(tool):
    """A value for every parameter in the tool's input schema, required or optional."""
    return {name: _DUMMY.get(prop.get("type"), 1)
            for name, prop in tool.input_schema.get("properties", {}).items()}


@pytest.mark.anyio
async def test_every_tool_reaches_a_matching_handler(fake_maya, monkeypatch):
    import inspect

    import maya_mcp_handlers as handlers

    maya = fake_maya()
    tools = await mcp.list_tools()
    for tool in tools:
        maya.commands.clear()
        await mcp.call_tool(tool.name, _all_arguments(tool))
        assert len(maya.commands) == 1, f"{tool.name} sent {len(maya.commands)} commands"
        command = maya.commands[0]
        name, params = command["type"], command["params"]

        # Replace the handler with a stub that checks the parameters against the real
        # signature, so no handler code runs (with mocked Maya it could touch real files)
        real = getattr(handlers, name, None)
        assert callable(real), f"{tool.name} sends '{name}', but maya_mcp_handlers has no such function"
        calls = []

        def stub(**kwargs):
            inspect.signature(real).bind(**kwargs)  # TypeError -> error reply from dispatch()
            calls.append(kwargs)
            return {}

        monkeypatch.setattr(handlers, name, stub)
        reply = json.loads(handlers.dispatch(json.dumps(command)))
        assert reply["status"] == "success", f"{tool.name} sends {sorted(params)}: {reply['message']}"
        assert calls == [params], f"dispatch() does not route '{name}' to {name}()"
