import base64
import json
import logging
import os
import socket
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict

from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import __version__

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("MayaMCP")

DEFAULT_HOST = "127.0.0.1"  # Maya's commandPort only listens on IPv4
DEFAULT_PORT = 9877
CONNECT_TIMEOUT = 3.0
DEFAULT_RESPONSE_TIMEOUT = 30.0
RENDER_RESPONSE_TIMEOUT = 600.0

OPEN_PORT_HINT = "cmds.commandPort(name=':9877', sourceType='python')"


@dataclass
class MayaConnection:
    """Talks to Maya's commandPort. Opens a fresh socket for every command so
    an idle client never holds the port and a late reply can't leak into the
    next command."""

    host: str
    port: int

    def _connect(self) -> socket.socket:
        try:
            return socket.create_connection((self.host, self.port), timeout=CONNECT_TIMEOUT)
        except ConnectionRefusedError as e:
            raise ConnectionError(
                f"Could not connect to Maya: nothing is listening on {self.host}:{self.port}. "
                f"Make sure Maya is open and run: {OPEN_PORT_HINT}"
            ) from e
        except socket.timeout as e:
            raise ConnectionError(
                f"Could not connect to Maya: connecting to {self.host}:{self.port} timed out "
                f"after {CONNECT_TIMEOUT:.0f}s. The port is busy (Maya is stuck on a long "
                f"operation or another client is using it) or the host is unreachable."
            ) from e
        except OSError as e:
            raise ConnectionError(f"Could not connect to Maya: {self.host}:{self.port} is unreachable ({e})") from e

    @staticmethod
    def _receive_response(sock: socket.socket) -> str:
        """Read from commandPort until the \\n\\x00 terminator."""
        chunks = []
        while True:
            chunk = sock.recv(8192)
            if not chunk:
                break
            chunks.append(chunk)
            # The terminator is one byte, so it can't be split across chunks
            if b"\x00" in chunk:
                data = b"".join(chunks)
                return data[: data.index(b"\x00")].decode("utf-8").strip()
        raise ConnectionError("Maya commandPort closed without sending a response")

    def send_command(
        self, command_type: str, params: Dict[str, Any] = None, timeout: float = DEFAULT_RESPONSE_TIMEOUT
    ) -> Dict[str, Any]:
        command = {"type": command_type, "params": params or {}}
        command_json = json.dumps(command)
        encoded = base64.b64encode(command_json.encode("utf-8")).decode("ascii")
        # Single-expression form — commandPort returns the value of a single expression
        code = f"__import__('maya_mcp_handlers').dispatch(__import__('base64').b64decode(b'{encoded}').decode())"

        logger.info(f"Sending command: {command_type}")
        with self._connect() as sock:
            sock.settimeout(timeout)
            try:
                sock.sendall(code.encode("utf-8"))
                raw = self._receive_response(sock)
            except socket.timeout as e:
                raise ConnectionError(
                    f"Maya did not answer '{command_type}' within {timeout:.0f}s. It may still be working on it."
                ) from e
            except (BrokenPipeError, ConnectionResetError) as e:
                raise ConnectionError(f"Lost connection to Maya: {e}") from e
        logger.info(f"Received response ({len(raw)} bytes)")

        try:
            response = json.loads(raw)
        except ValueError:
            # Maya evaluated something other than dispatch(), or the expression failed
            response = None
        if not isinstance(response, dict):
            raise RuntimeError(
                f"Unexpected reply from Maya: {raw[:200]!r}. Is maya_mcp_handlers.py in Maya's scripts "
                f"directory? Maya's Script Editor shows the error."
            )
        if response.get("status") == "error":
            raise RuntimeError(response.get("message", "Unknown error from Maya"))
        return response.get("result", {})


def get_maya_connection() -> MayaConnection:
    host = os.getenv("MAYA_HOST", DEFAULT_HOST)
    port = int(os.getenv("MAYA_PORT", DEFAULT_PORT))
    return MayaConnection(host=host, port=port)


def _send(command_type: str, params: Dict[str, Any] = None, timeout: float = DEFAULT_RESPONSE_TIMEOUT) -> Any:
    """Send a command for a tool. Failures become ToolError, which the client gets as an
    is_error result with the message (any other exception's message stays on the server)."""
    try:
        return get_maya_connection().send_command(command_type, params, timeout=timeout)
    except Exception as e:
        raise ToolError(str(e)) from e


def _read_and_remove(filepath: str) -> bytes:
    """Read a temp image written by Maya, then delete it (and its maya_mcp_* folder)."""
    with open(filepath, "rb") as f:
        data = f.read()
    try:
        os.remove(filepath)
        folder = os.path.dirname(filepath)
        if os.path.basename(folder).startswith("maya_mcp_"):
            os.rmdir(folder)
    except OSError:
        pass
    return data


@asynccontextmanager
async def server_lifespan(server: MCPServer) -> AsyncIterator[Dict[str, Any]]:
    logger.info("MayaMCP server starting")
    try:
        get_maya_connection()._connect().close()
        logger.info("Maya commandPort is reachable")
    except ConnectionError as e:
        logger.warning(str(e))
    yield {}
    logger.info("MayaMCP server stopped")


mcp = MCPServer("MayaMCP", version=__version__, lifespan=server_lifespan)


# ---------------------------------------------------------------------------
# Core tools
# ---------------------------------------------------------------------------


@mcp.tool()
def get_scene_info(
    ctx: Context,
    limit: int = 20,
    offset: int = 0,
    name_filter: str = None,
    top_level_only: bool = True,
) -> str:
    """Get the scene name, material count and a page of objects (sorted by name).
    By default lists top-level objects only and leaves out Maya's default cameras.
    name_filter takes Maya wildcards, e.g. "spike_*". If "truncated" is true, there
    are more objects: call again with offset=next_offset. Set top_level_only=false
    to include objects inside groups. limit is 1-500."""
    params = {"limit": limit, "offset": offset, "top_level_only": top_level_only}
    if name_filter:
        params["name_filter"] = name_filter
    result = _send("get_scene_info", params)
    return json.dumps(result, indent=2)


@mcp.tool()
def get_object_info(ctx: Context, object_name: str) -> str:
    """Get detailed information about a specific Maya object: transforms, bounding box, materials, hierarchy."""
    result = _send("get_object_info", {"name": object_name})
    return json.dumps(result, indent=2)


@mcp.tool(structured_output=False)
def get_viewport_screenshot(ctx: Context, max_size: int = 800) -> Image:
    """Capture the active Maya viewport as a PNG image. The longer side is scaled to max_size."""
    result = _send("get_viewport_screenshot", {"max_size": max_size})
    filepath = result.get("filepath", "")
    if not filepath or not os.path.exists(filepath):
        raise ToolError(f"Screenshot file not found at: {filepath}")
    return Image(data=_read_and_remove(filepath), format="png")


@mcp.tool()
def execute_maya_code(ctx: Context, code: str) -> str:
    """Execute arbitrary Python code inside Maya. `cmds` (maya.cmds) is pre-imported.
    Returns printed output, and the value of the last line if it is an expression
    (like the Script Editor). On errors, returns the traceback with line numbers."""
    result = _send("execute_maya_code", {"code": code})
    return json.dumps(result, indent=2)


@mcp.tool()
def get_selected_objects(ctx: Context) -> str:
    """Get the currently selected objects in Maya."""
    result = _send("get_selected_objects")
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Object management tools
# ---------------------------------------------------------------------------


@mcp.tool()
def create_object(ctx: Context, object_type: str, name: str = None) -> str:
    """Create a Maya object. object_type: cube, sphere, plane, cylinder, cone, torus,
    camera, point_light, directional_light, spot_light. Returns the created object name."""
    params = {"object_type": object_type}
    if name:
        params["name"] = name
    result = _send("create_object", params)
    return json.dumps(result, indent=2)


@mcp.tool()
def delete_objects(ctx: Context, names: list) -> str:
    """Delete one or more Maya objects by name. names is a list of object names."""
    result = _send("delete_objects", {"names": names})
    return json.dumps(result, indent=2)


@mcp.tool()
def duplicate_object(ctx: Context, name: str, new_name: str = None) -> str:
    """Duplicate a Maya object. Optionally rename the duplicate."""
    params = {"name": name}
    if new_name:
        params["new_name"] = new_name
    result = _send("duplicate_object", params)
    return json.dumps(result, indent=2)


@mcp.tool()
def rename_object(ctx: Context, name: str, new_name: str) -> str:
    """Rename a Maya object."""
    result = _send("rename_object", {"name": name, "new_name": new_name})
    return json.dumps(result, indent=2)


@mcp.tool()
def set_parent(ctx: Context, child: str, parent: str = None) -> str:
    """Parent a child object to a parent. Pass parent=null to unparent (move to world)."""
    result = _send("set_parent", {"child": child, "parent": parent})
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Transform tools
# ---------------------------------------------------------------------------


@mcp.tool()
def set_transform(
    ctx: Context,
    name: str,
    translation: list = None,
    rotation: list = None,
    scale: list = None,
) -> str:
    """Set world-space translation [x,y,z], rotation [rx,ry,rz] (degrees), and/or scale [sx,sy,sz] on an object.
    All parameters are optional — only the ones provided will be changed."""
    params = {"name": name}
    if translation is not None:
        params["translation"] = translation
    if rotation is not None:
        params["rotation"] = rotation
    if scale is not None:
        params["scale"] = scale
    result = _send("set_transform", params)
    return json.dumps(result, indent=2)


@mcp.tool()
def get_transform(ctx: Context, name: str) -> str:
    """Get world-space translation, rotation, and scale of a Maya object."""
    result = _send("get_transform", {"name": name})
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Material tools
# ---------------------------------------------------------------------------


@mcp.tool()
def create_material(ctx: Context, name: str, material_type: str = "lambert") -> str:
    """Create a Maya material/shader and its shading group.
    material_type: lambert, blinn, phong, standardSurface, aiStandardSurface."""
    result = _send("create_material", {"name": name, "material_type": material_type})
    return json.dumps(result, indent=2)


@mcp.tool()
def assign_material(ctx: Context, material_name: str, object_names: list) -> str:
    """Assign a material to one or more objects. object_names is a list."""
    result = _send("assign_material", {"material_name": material_name, "object_names": object_names})
    return json.dumps(result, indent=2)


@mcp.tool()
def list_materials(ctx: Context) -> str:
    """List all materials in the current Maya scene."""
    result = _send("list_materials")
    return json.dumps(result, indent=2)


@mcp.tool()
def set_material_property(ctx: Context, material_name: str, attribute: str, value) -> str:
    """Set a property on a Maya material. For colors pass [r,g,b] (0-1 range).
    Examples: attribute='color' value=[1,0,0] for red, attribute='transparency' value=[0.5,0.5,0.5]."""
    result = _send(
        "set_material_property",
        {"material_name": material_name, "attribute": attribute, "value": value},
    )
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Attribute tools
# ---------------------------------------------------------------------------


@mcp.tool()
def get_attribute(ctx: Context, node: str, attribute: str) -> str:
    """Get the value of an attribute on a Maya node. Example: node='pCube1', attribute='tx'."""
    result = _send("get_attribute", {"node": node, "attribute": attribute})
    return json.dumps(result, indent=2)


@mcp.tool()
def set_attribute(ctx: Context, node: str, attribute: str, value) -> str:
    """Set the value of an attribute on a Maya node. Pass lists for multi-component attrs."""
    result = _send("set_attribute", {"node": node, "attribute": attribute, "value": value})
    return json.dumps(result, indent=2)


@mcp.tool()
def list_attributes(ctx: Context, node: str, keyable: bool = False) -> str:
    """List attributes on a Maya node. Set keyable=true to list only keyable attributes."""
    result = _send("list_attributes", {"node": node, "keyable": keyable})
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Rendering tools
# ---------------------------------------------------------------------------


@mcp.tool(structured_output=False)
def render_scene(
    ctx: Context,
    width: int = None,
    height: int = None,
    output_path: str = None,
) -> Image:
    """Render the current Maya scene with Arnold or Maya Software (whichever is active) and return it as a PNG.
    width/height override the resolution for this render only. output_path is where to save the render."""
    params = {}
    if width:
        params["width"] = width
    if height:
        params["height"] = height
    if output_path:
        params["output_path"] = output_path

    result = _send("render_scene", params, timeout=RENDER_RESPONSE_TIMEOUT)
    rendered_path = result.get("rendered_path", "")
    if not rendered_path or not os.path.exists(rendered_path):
        raise ToolError(f"Rendered file not found at: {rendered_path}")
    if output_path:
        with open(rendered_path, "rb") as f:
            data = f.read()
    else:
        data = _read_and_remove(rendered_path)
    return Image(data=data, format="png")


@mcp.tool()
def set_render_settings(
    ctx: Context,
    renderer: str = None,
    width: int = None,
    height: int = None,
    start_frame: float = None,
    end_frame: float = None,
) -> str:
    """Configure Maya render settings. renderer examples: 'mayaSoftware', 'arnold', 'vray'
    (render_scene supports 'arnold' and 'mayaSoftware')."""
    params = {}
    if renderer:
        params["renderer"] = renderer
    if width:
        params["width"] = width
    if height:
        params["height"] = height
    if start_frame is not None:
        params["start_frame"] = start_frame
    if end_frame is not None:
        params["end_frame"] = end_frame
    result = _send("set_render_settings", params)
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# File I/O tools
# ---------------------------------------------------------------------------


@mcp.tool()
def export_scene(
    ctx: Context,
    filepath: str,
    export_type: str = "ma",
    selected_only: bool = False,
) -> str:
    """Export the scene or selection. export_type: ma, mb, fbx, obj, abc (Alembic).
    Set selected_only=true to export only selected objects."""
    result = _send(
        "export_scene",
        {"filepath": filepath, "export_type": export_type, "selected_only": selected_only},
    )
    return json.dumps(result, indent=2)


@mcp.tool()
def import_file(ctx: Context, filepath: str, import_type: str = None) -> str:
    """Import a file into Maya. import_type auto-detected from extension if not specified.
    Supported: ma, mb, fbx, obj, abc."""
    params = {"filepath": filepath}
    if import_type:
        params["import_type"] = import_type
    result = _send("import_file", params)
    return json.dumps(result, indent=2)
