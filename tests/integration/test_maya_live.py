"""Integration tests against a real, running Maya with the commandPort open.

Skipped unless Maya is reachable on MAYA_HOST:MAYA_PORT (default 127.0.0.1:9877).
They create nodes prefixed with "mcp_it_" and delete them again; the render test
renders the current scene with Arnold at 64x64 and restores the render resolution.

    uv run pytest -m integration
"""

import base64
import json
import os
import socket
import struct

import pytest

from maya_mcp.server import mcp

HOST = os.getenv("MAYA_HOST", "127.0.0.1")
PORT = int(os.getenv("MAYA_PORT", "9877"))


def _maya_reachable():
    try:
        socket.create_connection((HOST, PORT), timeout=1).close()
        return True
    except OSError:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.anyio,
    pytest.mark.skipif(not _maya_reachable(), reason=f"Maya commandPort not reachable on {HOST}:{PORT}"),
]

# conftest.py mocks maya.cmds for the unit tests; these tests talk to the real Maya,
# which imports its own maya_mcp_handlers, so make sure Maya uses the current file.
RELOAD = "import importlib, maya_mcp_handlers; importlib.reload(maya_mcp_handlers)"


async def call(tool, **args):
    result = await mcp.call_tool(tool, args)
    out = "".join(c.text for c in result.content if c.type == "text")
    assert not out.startswith("Error:"), out
    try:
        return json.loads(out)
    except ValueError:
        return result


async def maya(code):
    return (await call("execute_maya_code", code=code)).get("result")


@pytest.fixture(autouse=True)
async def clean_scene():
    await call("execute_maya_code", code=RELOAD)
    yield
    await call("execute_maya_code", code=("nodes = cmds.ls('mcp_it_*')\nif nodes: cmds.delete(nodes)"))


async def test_scene_info():
    info = await call("get_scene_info")
    assert "objects" in info and "materials_count" in info


async def test_object_workflow():
    created = await call("create_object", object_type="cube", name="mcp_it_cube")
    assert created["name"] == "mcp_it_cube"
    await call("set_transform", name="mcp_it_cube", translation=[1, 2, 3], rotation=[0, 45, 0], scale=[2, 2, 2])
    t = await call("get_transform", name="mcp_it_cube")
    assert t["translation"] == pytest.approx([1, 2, 3])
    assert t["rotation"] == pytest.approx([0, 45, 0])
    await call("duplicate_object", name="mcp_it_cube", new_name="mcp_it_dup")
    await call("rename_object", name="mcp_it_dup", new_name="mcp_it_dup2")
    await call("set_parent", child="mcp_it_dup2", parent="mcp_it_cube")
    assert await maya("cmds.listRelatives('mcp_it_dup2', parent=True)") == "['mcp_it_cube']"
    deleted = await call("delete_objects", names=["mcp_it_cube", "mcp_it_missing"])
    assert deleted["not_found"] == ["mcp_it_missing"]


async def test_attributes_and_materials():
    await call("create_object", object_type="sphere", name="mcp_it_sphere")
    await call("set_attribute", node="mcp_it_sphere", attribute="visibility", value=False)
    assert await maya("cmds.getAttr('mcp_it_sphere.visibility')") == "False"
    assert "translateX" in json.dumps(await call("list_attributes", node="mcp_it_sphere", keyable=True))
    await call("create_material", name="mcp_it_MAT", material_type="lambert")
    await call("assign_material", material_name="mcp_it_MAT", object_names=["mcp_it_sphere"])
    await call("set_material_property", material_name="mcp_it_MAT", attribute="color", value=[1, 0, 0])
    assert await maya("cmds.getAttr('mcp_it_MAT.color')[0]") == "(1.0, 0.0, 0.0)"
    assert "mcp_it_MAT" in json.dumps(await call("list_materials"))


async def test_execute_code_result_and_traceback():
    assert (await call("execute_maya_code", code="x = 6\nx * 7"))["result"] == "42"
    result = await mcp.call_tool("execute_maya_code", {"code": "a = 1\ncmds.getAttr('mcp_it_nothing.tx')"})
    out = result.content[0].text
    assert 'File "<maya_mcp>", line 2' in out and "cmds.getAttr('mcp_it_nothing.tx')" in out


async def test_export_and_import(tmp_path):
    await call("create_object", object_type="cube", name="mcp_it_export")
    await maya("cmds.select('mcp_it_export')")
    path = str(tmp_path / "mcp_it_export.ma")
    await call("export_scene", filepath=path, export_type="ma", selected_only=True)
    assert os.path.exists(path)
    await call("delete_objects", names=["mcp_it_export"])
    imported = await call("import_file", filepath=path)
    assert imported["count"] > 0
    assert await maya("cmds.objExists('mcp_it_export')") == "True"


async def test_viewport_screenshot_keeps_aspect_ratio():
    result = await mcp.call_tool("get_viewport_screenshot", {"max_size": 300})
    image = [c for c in result.content if c.type == "image"][0]
    width, height = struct.unpack(">II", base64.b64decode(image.data)[16:24])
    assert max(width, height) == 300
    view = await maya(
        "import maya.OpenMayaUI as omui\nv = omui.M3dView.active3dView()\n(v.portWidth(), v.portHeight())"
    )
    vw, vh = eval(view)
    assert width / height == pytest.approx(vw / vh, rel=0.02)


async def test_render_scene():
    renderer = await maya("cmds.getAttr('defaultRenderGlobals.currentRenderer')")
    if renderer != "'arnold'":
        pytest.skip(f"active renderer is {renderer}, not Arnold")
    size = await maya("(cmds.getAttr('defaultResolution.width'), cmds.getAttr('defaultResolution.height'))")
    try:
        result = await mcp.call_tool("render_scene", {"width": 64, "height": 64})
        image = [c for c in result.content if c.type == "image"]
        assert image, result.content[0].text
        assert struct.unpack(">II", base64.b64decode(image[0].data)[16:24]) == (64, 64)
    finally:
        w, h = eval(size)
        await call("set_render_settings", width=w, height=h)


async def test_scene_info_paging_and_filter():
    for i in range(5):
        await call("create_object", object_type="sphere", name=f"mcp_it_ball_{i}")
    page = await call("get_scene_info", name_filter="mcp_it_ball_*", limit=3)
    assert (page["object_count"], page["returned"], page["truncated"], page["next_offset"]) == (5, 3, True, 3)
    rest = await call("get_scene_info", name_filter="mcp_it_ball_*", limit=3, offset=3)
    assert [o["name"] for o in page["objects"] + rest["objects"]] == [f"mcp_it_ball_{i}" for i in range(5)]
    assert rest["truncated"] is False and page["objects"][0]["type"] == "mesh"
    everything = await call("get_scene_info", limit=500)
    assert not {"persp", "top", "front", "side"} & {o["name"] for o in everything["objects"]}


async def test_object_info_and_selection():
    await call("create_object", object_type="cube", name="mcp_it_info")
    await call("create_material", name="mcp_it_infoMAT")
    await call("assign_material", material_name="mcp_it_infoMAT", object_names=["mcp_it_info"])
    info = await call("get_object_info", object_name="mcp_it_info")
    assert info["materials"] == ["mcp_it_infoMAT"] and info["parent"] is None
    assert info["bounding_box"]["min"] == pytest.approx([-0.5, -0.5, -0.5])
    await maya("cmds.select('mcp_it_info')")
    assert (await call("get_selected_objects"))["selected"] == ["mcp_it_info"]
    await maya("cmds.select(clear=True)")


async def test_get_attribute_and_unparent():
    await call("create_object", object_type="cube", name="mcp_it_child")
    await call("create_object", object_type="cube", name="mcp_it_parent")
    await call("set_transform", name="mcp_it_child", translation=[4, 5, 6])
    translate = await call("get_attribute", node="mcp_it_child", attribute="translate")
    assert translate["value"] == pytest.approx([4, 5, 6])
    await call("set_parent", child="mcp_it_child", parent="mcp_it_parent")
    assert await maya("cmds.listRelatives('mcp_it_child', parent=True)") == "['mcp_it_parent']"
    await call("set_parent", child="mcp_it_child")
    assert await maya("cmds.listRelatives('mcp_it_child', parent=True)") is None  # no parent: no result


RENDER_SETTINGS = "tuple(cmds.getAttr(a) for a in {})".format(
    (
        "defaultResolution.width",
        "defaultResolution.height",
        "defaultRenderGlobals.startFrame",
        "defaultRenderGlobals.endFrame",
    )
)


async def test_set_render_settings():
    w, h, start, end = eval(await maya(RENDER_SETTINGS))
    try:
        await call("set_render_settings", width=321, height=123, start_frame=5, end_frame=9)
        assert eval(await maya(RENDER_SETTINGS)) == (321, 123, 5.0, 9.0)
    finally:
        await call("set_render_settings", width=w, height=h, start_frame=start, end_frame=end)
