"""Tests for maya_mcp_handlers.py (the code that runs inside Maya), with maya.cmds mocked."""

import json
from unittest import mock

import pytest

import maya_mcp_handlers as handlers


def dispatch(command_type, **params):
    return json.loads(handlers.dispatch(json.dumps({"type": command_type, "params": params})))


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


def test_dispatch_success(cmds):
    cmds.objExists.return_value = True
    cmds.xform.side_effect = [[1.0, 2.0, 3.0], [0.0, 45.0, 0.0], [1.0, 1.0, 1.0]]
    response = dispatch("get_transform", name="pCube1")
    assert response == {
        "status": "success",
        "result": {
            "name": "pCube1",
            "translation": [1.0, 2.0, 3.0],
            "rotation": [0.0, 45.0, 0.0],
            "scale": [1.0, 1.0, 1.0],
        },
    }


def test_dispatch_unknown_command(cmds):
    response = dispatch("does_not_exist")
    assert response["status"] == "error"
    assert "Unknown command: does_not_exist" in response["message"]


def test_dispatch_turns_exceptions_into_errors(cmds):
    cmds.objExists.return_value = False
    response = dispatch("get_transform", name="missing")
    assert response == {"status": "error", "message": "Object not found: missing"}


def test_dispatch_rejects_bad_parameters(cmds):
    response = dispatch("get_transform", wrong_param="x")
    assert response["status"] == "error"
    assert "wrong_param" in response["message"]


# ---------------------------------------------------------------------------
# A few handlers
# ---------------------------------------------------------------------------


def test_create_object_unknown_type(cmds):
    with pytest.raises(ValueError, match="Unknown object type: teapot"):
        handlers.create_object("teapot")


@pytest.mark.parametrize(
    "object_type, command", [("point_light", "pointLight"), ("directional_light", "directionalLight")]
)
def test_create_light_names_and_returns_its_transform(cmds, object_type, command):
    getattr(cmds, command).return_value = "keyLightShape1"  # the light commands return the shape
    cmds.listRelatives.side_effect = lambda node, parent=False: ["keyLight1"] if parent else []
    cmds.rename.side_effect = lambda old, new: new
    assert handlers.create_object(object_type, name="key") == {"name": "key", "type": object_type}
    cmds.rename.assert_called_once_with("keyLight1", "key")


def test_delete_objects_reports_missing(cmds):
    cmds.objExists.side_effect = lambda name: name != "ghost"
    assert handlers.delete_objects(["a", "ghost"]) == {"deleted": ["a"], "not_found": ["ghost"]}
    cmds.delete.assert_called_once_with(["a"])


def test_import_file_detects_type_from_extension(cmds):
    cmds.file.return_value = ["node1", "node2"]
    result = handlers.import_file("/tmp/model.FBX")
    assert result == {"imported_from": "/tmp/model.FBX", "new_nodes": ["node1", "node2"], "count": 2}
    cmds.loadPlugin.assert_called_once_with("fbxmaya", quiet=True)
    assert cmds.file.call_args.kwargs["type"] == "FBX"


def test_import_file_unknown_type(cmds):
    with pytest.raises(ValueError, match="Unknown import type: xyz"):
        handlers.import_file("/tmp/model.xyz")


def test_export_scene_unknown_type(cmds):
    with pytest.raises(ValueError, match=r"Unknown export type: usd. Valid: \['fbx', 'obj', 'abc', 'ma', 'mb'\]"):
        handlers.export_scene("/tmp/out.usd", export_type="USD")
    cmds.file.assert_not_called()


@pytest.mark.parametrize(
    "view, max_size, expected",
    [
        ((1920, 1080), 800, (800, 450)),  # landscape: width is the long side
        ((1080, 1920), 800, (450, 800)),  # portrait: height is the long side
        ((5002, 1970), 600, (600, 236)),  # the wide Retina viewport from manual testing
        ((500, 500), 800, (800, 800)),  # square, and upscaling a small viewport
        ((1000, 1), 800, (800, 1)),  # extreme ratio: never 0 pixels
        ((0, 0), 800, (1, 1)),  # minimised viewport: no division by zero
    ],
)
def test_screenshot_size_keeps_aspect_ratio(view, max_size, expected):
    assert handlers._screenshot_size(*view, max_size) == expected


# ---------------------------------------------------------------------------
# execute_maya_code
# ---------------------------------------------------------------------------


def test_execute_returns_last_expression():
    assert handlers.execute_maya_code("x = 2\nx * 21") == {"executed": True, "output": "", "result": "42"}


def test_execute_returns_repr_of_strings(cmds):
    cmds.about.return_value = "2025"
    assert handlers.execute_maya_code("cmds.about(version=True)")["result"] == "'2025'"


def test_execute_captures_print_and_result():
    assert handlers.execute_maya_code("print('hi')\n'done'") == {"executed": True, "output": "hi\n", "result": "'done'"}


@pytest.mark.parametrize(
    "code",
    [
        "a = 1",  # assignment
        "for i in range(2):\n    print(i)",  # compound statement last
        "None",  # expression evaluating to None
        "",  # empty code
    ],
)
def test_execute_without_result(code):
    assert "result" not in handlers.execute_maya_code(code)


def test_execute_namespace_is_fresh_per_call():
    handlers.execute_maya_code("leftover = 1")
    with pytest.raises(RuntimeError, match="NameError"):
        handlers.execute_maya_code("leftover")


def test_execute_error_has_traceback_with_line_numbers():
    with pytest.raises(RuntimeError) as err:
        handlers.execute_maya_code("print('step 1')\nobjs = []\nfirst = objs[0]")
    message = str(err.value)
    assert message.startswith("Output before the error:\nstep 1\n")
    assert 'File "<maya_mcp>", line 3, in <module>' in message
    assert "first = objs[0]" in message
    assert message.rstrip().endswith("IndexError: list index out of range")


def test_execute_error_shows_call_chain_and_hides_handler_frames():
    with pytest.raises(RuntimeError) as err:
        handlers.execute_maya_code("def f(n):\n    return 1 / n\n\nf(0)")
    message = str(err.value)
    assert "line 4, in <module>" in message and "line 2, in f" in message
    assert "maya_mcp_handlers.py" not in message
    assert "ZeroDivisionError" in message


def test_execute_error_in_maya_call(cmds):
    cmds.getAttr.side_effect = ValueError("No object matches name: nope.tx")
    with pytest.raises(RuntimeError) as err:
        handlers.execute_maya_code("cmds.getAttr('nope.tx')")
    assert "line 1" in str(err.value)
    assert "ValueError: No object matches name: nope.tx" in str(err.value)


def test_execute_syntax_error():
    with pytest.raises(RuntimeError) as err:
        handlers.execute_maya_code("x = (1,\nprint(x")
    message = str(err.value)
    assert '"<maya_mcp>"' in message and "SyntaxError" in message
    assert "Traceback" not in message


def test_execute_errors_go_through_dispatch():
    response = dispatch("execute_maya_code", code="1/0")
    assert response["status"] == "error"
    assert "ZeroDivisionError" in response["message"] and "line 1" in response["message"]


# ---------------------------------------------------------------------------
# get_scene_info
# ---------------------------------------------------------------------------


def _scene(cmds, top_level, all_transforms=None, cameras=("persp", "top")):
    """Mock a scene: ls() returns the given nodes, `cameras` are startup cameras,
    nodes ending in "_grp" are empty groups, everything else holds a mesh."""

    def ls(pattern="*", **kwargs):
        if kwargs.get("materials"):
            return ["lambert1"]
        nodes = top_level if kwargs.get("assemblies") else (all_transforms or top_level)
        prefix = pattern.rstrip("*")
        return [n for n in nodes if n.startswith(prefix)]

    def list_relatives(node, **kwargs):
        if kwargs.get("type") == "camera":
            return [node + "Shape"] if node in cameras else []
        if kwargs.get("shapes"):
            return [] if node.endswith("_grp") else [node + "Shape"]
        return ["child1", "child2"] if node.endswith("_grp") else []

    cmds.ls.side_effect = ls
    cmds.listRelatives.side_effect = list_relatives
    cmds.camera.side_effect = lambda shape, **kw: shape[: -len("Shape")] in cameras
    cmds.objectType.return_value = "mesh"
    cmds.xform.return_value = [1.23456, 0.0, -2.0]
    cmds.file.return_value = "/scenes/virus.ma"


def test_scene_info_pages_through_objects(cmds):
    _scene(cmds, ["obj_%02d" % i for i in range(25)])
    first = handlers.get_scene_info(limit=20)
    assert (first["object_count"], first["returned"], first["truncated"], first["next_offset"]) == (25, 20, True, 20)
    second = handlers.get_scene_info(limit=20, offset=first["next_offset"])
    assert (second["returned"], second["truncated"]) == (5, False)
    assert "next_offset" not in second
    names = [o["name"] for o in first["objects"] + second["objects"]]
    assert names == sorted(names) and len(set(names)) == 25  # stable order, no duplicates or gaps


def test_scene_info_leaves_out_default_cameras(cmds):
    _scene(cmds, ["persp", "top", "render_CAM", "ball"], cameras=("persp", "top"))
    info = handlers.get_scene_info()
    assert [o["name"] for o in info["objects"]] == ["ball", "render_CAM"]
    assert info["object_count"] == 2


def test_scene_info_describes_objects(cmds):
    _scene(cmds, ["ball", "spikes_grp"])
    ball, grp = handlers.get_scene_info()["objects"]
    assert ball == {"name": "ball", "type": "mesh", "location": [1.235, 0.0, -2.0], "children": 0}
    assert (grp["type"], grp["children"]) == ("group", 2)


def test_scene_info_filter_and_nested_objects(cmds):
    _scene(cmds, top_level=["virus_grp"], all_transforms=["virus_grp", "spike_01", "spike_02", "body"])
    assert handlers.get_scene_info(name_filter="spike_*")["object_count"] == 0  # top level only
    nested = handlers.get_scene_info(name_filter="spike_*", top_level_only=False)
    assert [o["name"] for o in nested["objects"]] == ["spike_01", "spike_02"]


def test_scene_info_lists_unqueryable_objects(cmds):
    _scene(cmds, ["ball"])
    cmds.xform.side_effect = RuntimeError("deleted meanwhile")
    assert handlers.get_scene_info()["objects"] == [{"name": "ball", "type": "unknown"}]


def test_scene_info_metadata(cmds):
    _scene(cmds, [])
    info = handlers.get_scene_info()
    assert (info["scene_name"], info["materials_count"], info["objects"], info["truncated"]) == (
        "/scenes/virus.ma",
        1,
        [],
        False,
    )


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"limit": 0}, "limit must be between 1 and 500"),
        ({"limit": 501}, "limit must be between 1 and 500"),
        ({"offset": -1}, "offset must be 0 or more"),
    ],
)
def test_scene_info_rejects_bad_paging(cmds, kwargs, message):
    with pytest.raises(ValueError, match=message):
        handlers.get_scene_info(**kwargs)


# ---------------------------------------------------------------------------
# Handlers that check the object exists first
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda: handlers.get_object_info("ghost"),
        lambda: handlers.duplicate_object("ghost"),
        lambda: handlers.rename_object("ghost", "new"),
        lambda: handlers.set_parent("ghost", "parent"),
        lambda: handlers.set_transform("ghost", translation=[0, 0, 0]),
        lambda: handlers.get_transform("ghost"),
        lambda: handlers.assign_material("ghost", ["pCube1"]),
        lambda: handlers.set_material_property("ghost", "color", [1, 0, 0]),
        lambda: handlers.get_attribute("ghost", "tx"),
        lambda: handlers.set_attribute("ghost", "tx", 1),
        lambda: handlers.list_attributes("ghost"),
    ],
    ids=[
        "get_object_info",
        "duplicate_object",
        "rename_object",
        "set_parent",
        "set_transform",
        "get_transform",
        "assign_material",
        "set_material_property",
        "get_attribute",
        "set_attribute",
        "list_attributes",
    ],
)
def test_missing_object_gives_clear_error_and_changes_nothing(cmds, call):
    cmds.objExists.return_value = False
    with pytest.raises(ValueError, match="not found: ghost"):
        call()
    for change in (cmds.delete, cmds.duplicate, cmds.rename, cmds.parent, cmds.xform, cmds.sets, cmds.setAttr):
        change.assert_not_called()


# ---------------------------------------------------------------------------
# Handlers with their own logic
# ---------------------------------------------------------------------------


def test_object_info_collects_unique_materials(cmds):
    cmds.objExists.return_value = True
    cmds.xform.side_effect = [[0, 0, 0], [0, 0, 0], [1, 1, 1], [-1, -2, -3, 1, 2, 3]]
    cmds.listRelatives.side_effect = lambda n, **kw: ["|ball|ballShape", "|ball|ballShape2"] if kw.get("shapes") else []
    cmds.listConnections.side_effect = lambda n, **kw: ["redSG"] if kw.get("type") else ["red_MAT"]
    info = handlers.get_object_info("ball")
    assert info["materials"] == ["red_MAT"]  # two shapes, same material: listed once
    assert info["parent"] is None  # top-level object
    assert info["bounding_box"] == {"min": [-1, -2, -3], "max": [1, 2, 3]}


@pytest.mark.parametrize(
    "maya_value, expected",
    [
        ([(1.0, 0.5, 0.0)], [1.0, 0.5, 0.0]),  # colours/vectors come back wrapped in a list of one tuple
        (3.5, 3.5),
        (True, True),
        ("text", "text"),
        ([(1, 2), (3, 4)], [(1, 2), (3, 4)]),  # several tuples: not a single vector, left as is
    ],
)
def test_get_attribute_unwraps_vectors(cmds, maya_value, expected):
    cmds.objExists.return_value = True
    cmds.getAttr.return_value = maya_value
    assert handlers.get_attribute("ball", "color")["value"] == expected


@pytest.mark.parametrize(
    "value, expected_call",
    [
        ([1, 2, 3], (("ball.translate", 1, 2, 3), {})),  # vectors are spread out
        ("hello", (("ball.notes", "hello"), {"type": "string"})),  # Maya needs type= for strings
        (False, (("ball.notes", False), {})),
        (2.5, (("ball.notes", 2.5), {})),
    ],
)
def test_set_attribute_matches_value_type(cmds, value, expected_call):
    cmds.objExists.return_value = True
    attribute = "translate" if isinstance(value, list) else "notes"
    handlers.set_attribute("ball", attribute, value)
    assert (cmds.setAttr.call_args.args, cmds.setAttr.call_args.kwargs) == expected_call


def test_set_material_property_colour_is_double3(cmds):
    cmds.objExists.return_value = True
    handlers.set_material_property("red_MAT", "color", [1, 0, 0])
    cmds.setAttr.assert_called_once_with("red_MAT.color", 1, 0, 0, type="double3")


def test_set_transform_changes_only_given_values(cmds):
    cmds.objExists.return_value = True
    handlers.set_transform("ball", rotation=[0, 90, 0])
    cmds.xform.assert_called_once_with("ball", worldSpace=True, rotation=[0, 90, 0])


def test_set_parent_without_parent_moves_to_world(cmds):
    cmds.objExists.return_value = True
    assert handlers.set_parent("ball") == {"child": "ball", "parent": "world"}
    cmds.parent.assert_called_once_with("ball", world=True)


def test_set_parent_checks_parent_exists(cmds):
    cmds.objExists.side_effect = lambda n: n == "ball"
    with pytest.raises(ValueError, match="Parent not found: ghost_grp"):
        handlers.set_parent("ball", "ghost_grp")
    cmds.parent.assert_not_called()


def test_assign_material_skips_missing_objects(cmds):
    cmds.objExists.side_effect = lambda n: n != "ghost"
    cmds.listConnections.return_value = ["red_MATSG.surfaceShader"]
    result = handlers.assign_material("red_MAT", ["ball", "ghost", "cube"])
    assert result["assigned_to"] == ["ball", "cube"]
    assert cmds.sets.call_count == 2


def test_assign_default_material_uses_the_mesh_shading_group(cmds):
    cmds.objExists.return_value = True
    # What Maya 2025 returns for its default material: the particle group comes first
    cmds.listConnections.return_value = ["initialParticleSE.surfaceShader", "initialShadingGroup.surfaceShader"]
    assert handlers.assign_material("standardSurface1", ["ball"])["shading_group"] == "initialShadingGroup"
    cmds.sets.assert_called_once_with("ball", edit=True, forceElement="initialShadingGroup")


@pytest.mark.parametrize(
    "connections",
    [
        [],
        ["bumpySG.displacementShader"],  # connected, but not as the surface shader
        ["initialParticleSE.surfaceShader"],  # only the particle group
    ],
)
def test_assign_material_without_shading_group(cmds, connections):
    cmds.objExists.return_value = True
    cmds.listConnections.return_value = connections
    with pytest.raises(ValueError, match="No shading group found for material: red_MAT"):
        handlers.assign_material("red_MAT", ["ball"])
    cmds.sets.assert_not_called()


def _render_globals(cmds, renderer):
    """Mock render globals in a dict, so tests can check what render_scene leaves behind."""
    attrs = {
        "defaultRenderGlobals.currentRenderer": renderer,
        "defaultRenderGlobals.imageFormat": 51,  # EXR
        "defaultResolution.width": 1920,
        "defaultResolution.height": 1080,
    }
    during_render = {}

    def render():
        during_render.update(attrs)
        return "/project/images/tmp/render.png"

    cmds.getAttr.side_effect = attrs.__getitem__
    cmds.setAttr.side_effect = attrs.__setitem__
    cmds.render.side_effect = render
    return attrs, during_render


def test_software_render_is_png_and_restores_settings(cmds):
    attrs, during_render = _render_globals(cmds, "mayaSoftware")
    before = dict(attrs)
    assert handlers.render_scene(width=64, height=32) == {"rendered_path": "/project/images/tmp/render.png"}
    assert during_render["defaultRenderGlobals.imageFormat"] == 32
    assert (during_render["defaultResolution.width"], during_render["defaultResolution.height"]) == (64, 32)
    assert attrs == before


def test_render_restores_settings_when_it_fails(cmds):
    attrs, _ = _render_globals(cmds, "mayaSoftware")
    before = dict(attrs)
    cmds.render.side_effect = RuntimeError("render failed")
    with pytest.raises(RuntimeError, match="render failed"):
        handlers.render_scene(width=64)
    assert attrs == before


def test_render_rejects_unsupported_renderer(cmds):
    attrs, _ = _render_globals(cmds, "vray")
    with pytest.raises(ValueError, match="Rendering with vray is not supported"):
        handlers.render_scene(width=64)
    cmds.render.assert_not_called()
    assert attrs["defaultResolution.width"] == 1920


def test_set_render_settings_converts_and_skips_unset(cmds):
    handlers.set_render_settings(width="1920", start_frame=1)
    assert cmds.setAttr.call_args_list == [
        mock.call("defaultResolution.width", 1920),  # strings from clients become ints
        mock.call("defaultRenderGlobals.startFrame", 1.0),
    ]


def test_list_attributes_keyable_filter(cmds):
    cmds.objExists.return_value = True
    cmds.listAttr.return_value = ["translateX", "visibility"]
    assert handlers.list_attributes("ball", keyable=True)["count"] == 2
    cmds.listAttr.assert_called_once_with("ball", keyable=True)


def test_duplicate_object_optional_rename(cmds):
    cmds.objExists.return_value = True
    cmds.duplicate.return_value = ["ball1"]
    assert handlers.duplicate_object("ball")["duplicate"] == "ball1"
    cmds.rename.assert_not_called()
    cmds.rename.return_value = "ball_copy"
    assert handlers.duplicate_object("ball", new_name="ball_copy")["duplicate"] == "ball_copy"
