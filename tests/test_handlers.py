"""Tests for maya_mcp_handlers.py (the code that runs inside Maya), with maya.cmds mocked."""
import json

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
    assert response == {"status": "success", "result": {
        "name": "pCube1", "translation": [1.0, 2.0, 3.0],
        "rotation": [0.0, 45.0, 0.0], "scale": [1.0, 1.0, 1.0]}}


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

def test_create_object_renames(cmds):
    cmds.polyCube.return_value = ["pCube1", "polyCube1"]
    cmds.rename.return_value = "box"
    assert handlers.create_object("cube", name="box") == {"name": "box", "type": "cube"}
    cmds.rename.assert_called_once_with("pCube1", "box")


def test_create_object_unknown_type(cmds):
    with pytest.raises(ValueError, match="Unknown object type: teapot"):
        handlers.create_object("teapot")


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


def test_export_scene_selected_only(cmds):
    handlers.export_scene("/tmp/out.obj", export_type="OBJ", selected_only=True)
    cmds.loadPlugin.assert_called_once_with("objExport", quiet=True)
    cmds.file.assert_called_once_with("/tmp/out.obj", force=True, exportSelected=True, type="OBJexport")


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


@pytest.mark.parametrize("code", [
    "a = 1",                                  # assignment
    "for i in range(2):\n    print(i)",       # compound statement last
    "None",                                   # expression evaluating to None
    "",                                       # empty code
])
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
    assert 'line 4, in <module>' in message and 'line 2, in f' in message
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
