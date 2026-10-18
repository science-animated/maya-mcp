# Maya MCP handlers module
# Place this file in ~/maya/scripts/ (or anywhere on MAYA_SCRIPT_PATH)
# Then open Maya's commandPort: cmds.commandPort(name=":9877", sourceType="python")

import ast
import io
import json
import linecache
import os
import shutil
import tempfile
import traceback
from contextlib import redirect_stdout

import maya.cmds as cmds


def dispatch(command_json: str) -> str:
    """Entry point called via commandPort. Receives JSON command, returns JSON response."""
    try:
        command = json.loads(command_json)
        cmd_type = command.get("type")
        params = command.get("params", {})

        handlers = {
            "get_scene_info": get_scene_info,
            "get_object_info": get_object_info,
            "get_viewport_screenshot": get_viewport_screenshot,
            "execute_maya_code": execute_maya_code,
            "get_selected_objects": get_selected_objects,
            "create_object": create_object,
            "delete_objects": delete_objects,
            "duplicate_object": duplicate_object,
            "rename_object": rename_object,
            "set_parent": set_parent,
            "set_transform": set_transform,
            "get_transform": get_transform,
            "create_material": create_material,
            "assign_material": assign_material,
            "list_materials": list_materials,
            "set_material_property": set_material_property,
            "get_attribute": get_attribute,
            "set_attribute": set_attribute,
            "list_attributes": list_attributes,
            "render_scene": render_scene,
            "set_render_settings": set_render_settings,
            "export_scene": export_scene,
            "import_file": import_file,
        }

        handler = handlers.get(cmd_type)
        if not handler:
            return json.dumps({"status": "error", "message": f"Unknown command: {cmd_type}"})

        result = handler(**params)
        return json.dumps({"status": "success", "result": result})

    except Exception as e:
        traceback.print_exc()
        return json.dumps({"status": "error", "message": str(e)})


# ---------------------------------------------------------------------------
# Core handlers
# ---------------------------------------------------------------------------

_MAX_SCENE_OBJECTS = 500


def _is_startup_camera(transform):
    """True for Maya's default persp/top/front/side cameras."""
    for shape in cmds.listRelatives(transform, shapes=True, type="camera") or []:
        if cmds.camera(shape, q=True, startupCamera=True):
            return True
    return False


def _object_kind(transform):
    """Describe a transform by what it holds: its shape type (mesh, camera, ...) or "group"."""
    shapes = cmds.listRelatives(transform, shapes=True) or []
    if shapes:
        return cmds.objectType(shapes[0])
    return "group" if cmds.listRelatives(transform, children=True) else "transform"


def get_scene_info(limit=20, offset=0, name_filter=None, top_level_only=True):
    """Return scene metadata and one page of objects, sorted by name.

    By default only top-level objects are listed, and Maya's default cameras are
    left out. name_filter takes Maya wildcards ("spike_*"). "truncated" tells the
    caller there are more objects; fetch them with next_offset."""
    limit, offset = int(limit), int(offset)
    if not 1 <= limit <= _MAX_SCENE_OBJECTS:
        raise ValueError(f"limit must be between 1 and {_MAX_SCENE_OBJECTS}, got {limit}")
    if offset < 0:
        raise ValueError(f"offset must be 0 or more, got {offset}")

    pattern = name_filter or "*"
    if top_level_only:
        nodes = cmds.ls(pattern, assemblies=True) or []
    else:
        nodes = cmds.ls(pattern, type="transform") or []
    nodes = sorted(n for n in nodes if not _is_startup_camera(n))

    objects = []
    for t in nodes[offset : offset + limit]:
        try:
            pos = cmds.xform(t, q=True, worldSpace=True, translation=True)
            objects.append(
                {
                    "name": t,
                    "type": _object_kind(t),
                    "location": [round(v, 3) for v in pos],
                    "children": len(cmds.listRelatives(t, children=True, type="transform") or []),
                }
            )
        except Exception:
            # The node can't be queried (e.g. deleted meanwhile); list it without details
            objects.append({"name": t, "type": "unknown"})

    result = {
        "scene_name": cmds.file(q=True, sceneName=True) or "untitled",
        "object_count": len(nodes),
        "offset": offset,
        "returned": len(objects),
        "truncated": offset + len(objects) < len(nodes),
        "objects": objects,
        "materials_count": len(cmds.ls(materials=True) or []),
    }
    if result["truncated"]:
        result["next_offset"] = offset + len(objects)
    return result


def get_object_info(name):
    """Return detailed info for one object."""
    if not cmds.objExists(name):
        raise ValueError(f"Object not found: {name}")

    t = cmds.xform(name, q=True, worldSpace=True, translation=True)
    r = cmds.xform(name, q=True, worldSpace=True, rotation=True)
    s = cmds.xform(name, q=True, worldSpace=True, scale=True)
    bb = cmds.xform(name, q=True, worldSpace=True, boundingBox=True)

    shapes = cmds.listRelatives(name, shapes=True, fullPath=True) or []
    children = cmds.listRelatives(name, children=True, type="transform") or []
    parents = cmds.listRelatives(name, parent=True) or []

    # Collect materials via shading engines connected to shapes
    materials = []
    for shape in shapes:
        sgs = cmds.listConnections(shape, type="shadingEngine") or []
        for sg in sgs:
            mats = cmds.listConnections(f"{sg}.surfaceShader") or []
            materials.extend(mats)
    materials = list(set(materials))

    return {
        "name": name,
        "type": cmds.objectType(name),
        "translation": t,
        "rotation": r,
        "scale": s,
        "bounding_box": {"min": bb[:3], "max": bb[3:]},
        "shapes": shapes,
        "children": children,
        "parent": parents[0] if parents else None,
        "materials": materials,
    }


def _screenshot_size(view_w, view_h, max_size):
    """Scale the viewport size so its longer side is max_size, keeping the aspect ratio."""
    scale = float(max_size) / max(view_w, view_h, 1)
    return max(1, int(round(view_w * scale))), max(1, int(round(view_h * scale)))


def get_viewport_screenshot(max_size=800):
    """Capture the active viewport and return the file path.
    Keeps the viewport's aspect ratio, scaling the longer side to max_size."""
    import maya.OpenMayaUI as omui

    view = omui.M3dView.active3dView()
    width, height = _screenshot_size(view.portWidth(), view.portHeight(), max_size)

    tmp_dir = tempfile.mkdtemp(prefix="maya_mcp_")
    filepath = os.path.join(tmp_dir, "screenshot.png")
    frame = cmds.currentTime(q=True)

    try:
        cmds.playblast(
            format="image",
            compression="png",
            completeFilename=filepath,
            frame=[frame],
            viewer=False,
            showOrnaments=False,
            percent=100,
            widthHeight=[width, height],
            clearCache=True,
        )
        if not os.path.exists(filepath):
            raise RuntimeError(f"Playblast did not write {filepath}")
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return {"filepath": filepath, "width": width, "height": height}


_CODE_FILENAME = "<maya_mcp>"


def execute_maya_code(code):
    """Execute arbitrary Python code in Maya's context, capturing stdout.

    Like the Script Editor, if the last statement is an expression its value is
    returned as "result". On failure the error message contains the traceback of
    the submitted code (with line numbers) and any output printed before it."""
    # Register the source so tracebacks can show the failing lines
    linecache.cache[_CODE_FILENAME] = (len(code), None, code.splitlines(True), _CODE_FILENAME)
    namespace = {"cmds": cmds, "__name__": "__main__"}
    buf = io.StringIO()
    result = None
    try:
        tree = ast.parse(code, _CODE_FILENAME)
        last_expr = None
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            last_expr = ast.Expression(tree.body.pop().value)
        with redirect_stdout(buf):
            exec(compile(tree, _CODE_FILENAME, "exec"), namespace)
            if last_expr is not None:
                result = eval(compile(last_expr, _CODE_FILENAME, "eval"), namespace)
    except Exception as e:
        frames = traceback.extract_tb(e.__traceback__)
        # Drop the handler's own frames; keep the submitted code and what it called
        while frames and frames[0].filename != _CODE_FILENAME:
            frames.pop(0)
        if isinstance(e, SyntaxError):
            details = "".join(traceback.format_exception_only(type(e), e))
        else:
            details = (
                "Traceback (most recent call last):\n"
                + "".join(traceback.format_list(frames))
                + "".join(traceback.format_exception_only(type(e), e))
            )
        output = buf.getvalue()
        if output:
            details = "Output before the error:\n" + output + "\n" + details
        raise RuntimeError(details.rstrip()) from None

    response = {"executed": True, "output": buf.getvalue()}
    if result is not None:
        response["result"] = repr(result)
    return response


def get_selected_objects():
    """Return currently selected objects."""
    selected = cmds.ls(selection=True) or []
    return {"selected": selected, "count": len(selected)}


# ---------------------------------------------------------------------------
# Object management
# ---------------------------------------------------------------------------

_OBJECT_CREATORS = {
    "cube": lambda: cmds.polyCube()[0],
    "sphere": lambda: cmds.polySphere()[0],
    "plane": lambda: cmds.polyPlane()[0],
    "cylinder": lambda: cmds.polyCylinder()[0],
    "cone": lambda: cmds.polyCone()[0],
    "torus": lambda: cmds.polyTorus()[0],
    "camera": lambda: cmds.camera()[0],
    "point_light": lambda: cmds.pointLight(),
    "directional_light": lambda: cmds.directionalLight(),
    "spot_light": lambda: cmds.spotLight(),
}


def create_object(object_type, name=None):
    """Create a Maya object of the given type."""
    creator = _OBJECT_CREATORS.get(object_type)
    if not creator:
        raise ValueError(f"Unknown object type: {object_type}. Valid types: {list(_OBJECT_CREATORS)}")
    obj = creator()
    if name:
        obj = cmds.rename(obj, name)
    return {"name": obj, "type": object_type}


def delete_objects(names):
    """Delete objects by name, ignoring any that don't exist."""
    existing = [n for n in names if cmds.objExists(n)]
    if existing:
        cmds.delete(existing)
    return {"deleted": existing, "not_found": [n for n in names if n not in existing]}


def duplicate_object(name, new_name=None):
    """Duplicate an object."""
    if not cmds.objExists(name):
        raise ValueError(f"Object not found: {name}")
    dupes = cmds.duplicate(name, returnRootsOnly=True)
    result = dupes[0]
    if new_name:
        result = cmds.rename(result, new_name)
    return {"original": name, "duplicate": result}


def rename_object(name, new_name):
    """Rename an object."""
    if not cmds.objExists(name):
        raise ValueError(f"Object not found: {name}")
    result = cmds.rename(name, new_name)
    return {"old_name": name, "new_name": result}


def set_parent(child, parent=None):
    """Parent child to parent, or unparent to world if parent is None."""
    if not cmds.objExists(child):
        raise ValueError(f"Object not found: {child}")
    if parent is None:
        cmds.parent(child, world=True)
        return {"child": child, "parent": "world"}
    if not cmds.objExists(parent):
        raise ValueError(f"Parent not found: {parent}")
    cmds.parent(child, parent)
    return {"child": child, "parent": parent}


# ---------------------------------------------------------------------------
# Transform
# ---------------------------------------------------------------------------


def set_transform(name, translation=None, rotation=None, scale=None):
    """Set world-space transform values. All parameters are optional."""
    if not cmds.objExists(name):
        raise ValueError(f"Object not found: {name}")
    if translation is not None:
        cmds.xform(name, worldSpace=True, translation=translation)
    if rotation is not None:
        cmds.xform(name, worldSpace=True, rotation=rotation)
    if scale is not None:
        cmds.xform(name, worldSpace=True, scale=scale)
    return {
        "name": name,
        "applied": {
            "translation": translation,
            "rotation": rotation,
            "scale": scale,
        },
    }


def get_transform(name):
    """Get world-space translation, rotation, and scale."""
    if not cmds.objExists(name):
        raise ValueError(f"Object not found: {name}")
    t = cmds.xform(name, q=True, worldSpace=True, translation=True)
    r = cmds.xform(name, q=True, worldSpace=True, rotation=True)
    s = cmds.xform(name, q=True, worldSpace=True, scale=True)
    return {"name": name, "translation": t, "rotation": r, "scale": s}


# ---------------------------------------------------------------------------
# Materials
# ---------------------------------------------------------------------------


def create_material(name, material_type="lambert"):
    """Create a shader and its shading group."""
    mat = cmds.shadingNode(material_type, asShader=True, name=name)
    sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=f"{name}SG")
    cmds.connectAttr(f"{mat}.outColor", f"{sg}.surfaceShader", force=True)
    return {"material": mat, "shading_group": sg, "type": material_type}


def assign_material(material_name, object_names):
    """Assign a material to a list of objects."""
    if not cmds.objExists(material_name):
        raise ValueError(f"Material not found: {material_name}")
    sgs = cmds.listConnections(material_name, type="shadingEngine") or []
    if not sgs:
        raise ValueError(f"No shading group found for material: {material_name}")
    sg = sgs[0]
    assigned = []
    for obj in object_names:
        if cmds.objExists(obj):
            cmds.sets(obj, edit=True, forceElement=sg)
            assigned.append(obj)
    return {"material": material_name, "shading_group": sg, "assigned_to": assigned}


def list_materials():
    """List all materials in the scene."""
    mats = cmds.ls(materials=True) or []
    result = []
    for m in mats:
        result.append({"name": m, "type": cmds.objectType(m)})
    return {"materials": result, "count": len(result)}


def set_material_property(material_name, attribute, value):
    """Set an attribute on a material node."""
    if not cmds.objExists(material_name):
        raise ValueError(f"Material not found: {material_name}")
    attr_path = f"{material_name}.{attribute}"
    if isinstance(value, (list, tuple)):
        cmds.setAttr(attr_path, *value, type="double3")
    elif isinstance(value, str):
        cmds.setAttr(attr_path, value, type="string")
    else:
        cmds.setAttr(attr_path, value)
    return {"material": material_name, "attribute": attribute, "value": value}


# ---------------------------------------------------------------------------
# Attributes
# ---------------------------------------------------------------------------


def get_attribute(node, attribute):
    """Get an attribute value."""
    if not cmds.objExists(node):
        raise ValueError(f"Node not found: {node}")
    value = cmds.getAttr(f"{node}.{attribute}")
    # Maya returns some color/vector attrs as [(r,g,b)] — flatten to list
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], tuple):
        value = list(value[0])
    return {"node": node, "attribute": attribute, "value": value}


def set_attribute(node, attribute, value):
    """Set an attribute value."""
    if not cmds.objExists(node):
        raise ValueError(f"Node not found: {node}")
    attr_path = f"{node}.{attribute}"
    if isinstance(value, (list, tuple)):
        cmds.setAttr(attr_path, *value)
    elif isinstance(value, str):
        cmds.setAttr(attr_path, value, type="string")
    else:
        cmds.setAttr(attr_path, value)
    return {"node": node, "attribute": attribute, "value": value}


def list_attributes(node, keyable=False):
    """List attributes on a node."""
    if not cmds.objExists(node):
        raise ValueError(f"Node not found: {node}")
    if keyable:
        attrs = cmds.listAttr(node, keyable=True) or []
    else:
        attrs = cmds.listAttr(node) or []
    return {"node": node, "attributes": attrs, "count": len(attrs)}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_scene(output_path=None, width=None, height=None):
    """Render the current scene using the active renderer.
    If output_path is given, the image is moved there; otherwise the caller
    deletes the temp render after reading it."""
    if width:
        cmds.setAttr("defaultResolution.width", int(width))
    if height:
        cmds.setAttr("defaultResolution.height", int(height))
    if cmds.getAttr("defaultRenderGlobals.currentRenderer") == "arnold":
        rendered = _render_arnold()
    else:
        # cmds.render() is the Maya Software renderer
        rendered = cmds.render()
    if output_path:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        shutil.move(rendered, output_path)
        rendered = output_path
    return {"rendered_path": rendered}


def _render_arnold():
    """Render with Arnold into the Render View and save it as a PNG in a temp folder."""
    cameras = [cmds.listRelatives(c, parent=True)[0] for c in cmds.ls(type="camera") if cmds.getAttr(c + ".renderable")]
    if not cameras:
        raise RuntimeError("No renderable camera in the scene")
    cmds.arnoldRender(
        width=cmds.getAttr("defaultResolution.width"),
        height=cmds.getAttr("defaultResolution.height"),
        camera=cameras[0],
    )

    tmp_dir = tempfile.mkdtemp(prefix="maya_mcp_")
    filepath = os.path.join(tmp_dir, "render.png")
    image_format = cmds.getAttr("defaultRenderGlobals.imageFormat")
    try:
        cmds.setAttr("defaultRenderGlobals.imageFormat", 32)  # writeImage uses this format
        cmds.renderWindowEditor("renderView", e=True, colorManage=True, writeImage=filepath)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    finally:
        cmds.setAttr("defaultRenderGlobals.imageFormat", image_format)
    return filepath


def set_render_settings(renderer=None, width=None, height=None, start_frame=None, end_frame=None):
    """Configure render globals."""
    if renderer:
        cmds.setAttr("defaultRenderGlobals.currentRenderer", renderer, type="string")
    if width:
        cmds.setAttr("defaultResolution.width", int(width))
    if height:
        cmds.setAttr("defaultResolution.height", int(height))
    if start_frame is not None:
        cmds.setAttr("defaultRenderGlobals.startFrame", float(start_frame))
    if end_frame is not None:
        cmds.setAttr("defaultRenderGlobals.endFrame", float(end_frame))
    return {
        "renderer": renderer,
        "width": width,
        "height": height,
        "start_frame": start_frame,
        "end_frame": end_frame,
    }


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

_EXPORT_TYPES = {
    "fbx": "FBX export",
    "obj": "OBJexport",
    "abc": "Alembic",
    "ma": "mayaAscii",
    "mb": "mayaBinary",
}

_IMPORT_TYPES = {
    "fbx": "FBX",
    "obj": "OBJ",
    "abc": "Alembic",
    "ma": "mayaAscii",
    "mb": "mayaBinary",
}

_EXPORT_PLUGINS = {
    "fbx": "fbxmaya",
    "obj": "objExport",
    "abc": "AbcExport",
}

_IMPORT_PLUGINS = {
    "fbx": "fbxmaya",
    "obj": "objExport",
    "abc": "AbcImport",
}


def export_scene(filepath, export_type="ma", selected_only=False):
    """Export scene or selection to file."""
    export_type = export_type.lower()
    file_type = _EXPORT_TYPES.get(export_type)
    if not file_type:
        raise ValueError(f"Unknown export type: {export_type}. Valid: {list(_EXPORT_TYPES)}")

    plugin = _EXPORT_PLUGINS.get(export_type)
    if plugin:
        cmds.loadPlugin(plugin, quiet=True)

    if selected_only:
        cmds.file(filepath, force=True, exportSelected=True, type=file_type)
    else:
        cmds.file(filepath, force=True, exportAll=True, type=file_type)

    return {"exported_to": filepath, "type": export_type}


def import_file(filepath, import_type=None):
    """Import a file into the scene."""
    if import_type is None:
        ext = filepath.rsplit(".", 1)[-1].lower() if "." in filepath else ""
        import_type = ext

    import_type = import_type.lower()
    file_type = _IMPORT_TYPES.get(import_type)
    if not file_type:
        raise ValueError(f"Unknown import type: {import_type}. Valid: {list(_IMPORT_TYPES)}")

    plugin = _IMPORT_PLUGINS.get(import_type)
    if plugin:
        cmds.loadPlugin(plugin, quiet=True)

    new_nodes = cmds.file(filepath, i=True, type=file_type, returnNewNodes=True, mergeNamespacesOnClash=False) or []

    return {"imported_from": filepath, "new_nodes": new_nodes, "count": len(new_nodes)}
