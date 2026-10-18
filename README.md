# Maya MCP

[![Tests](https://github.com/science-animated/maya-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/science-animated/maya-mcp/actions/workflows/tests.yml)

Connect Autodesk Maya to AI assistants (Claude, Cursor, VS Code) via the [Model Context Protocol](https://modelcontextprotocol.io).

## How it works

```
AI Client  ──stdio──►  MayaMCP server  ──TCP :9877──►  Maya commandPort  ──►  maya.cmds
```

Maya's built-in `commandPort` acts as the TCP bridge. The MCP server sends Python expressions to it; Maya evaluates them on its main thread and returns results. No plugin required.

The server opens a new connection for every command and closes it afterwards. Maya's commandPort serves one connection at a time, so this lets several AI clients (e.g. Claude Desktop and Claude Code) use the same Maya session.

> **Security:** the commandPort runs any Python code sent to it, and so does the `execute_maya_code` tool. Only open the port on a machine you trust, keep it bound to localhost, and close it (`cmds.commandPort(name=":9877", close=True)`) when you don't need it.

## Setup

### 1. Install the MCP server

Requires [uv](https://docs.astral.sh/uv/) (or pip) and Python 3.10+.

```bash
uv tool install git+https://github.com/science-animated/maya-mcp
# or with pip:
pip install git+https://github.com/science-animated/maya-mcp
```

> Install from this repository. The `maya-mcp` package on PyPI is a different project.

### 2. Configure Maya

Copy (or symlink) `maya_mcp_handlers.py` and `userSetup.py` from this repository to your Maya scripts directory:

- **macOS:** `~/Library/Preferences/Autodesk/maya/scripts/`
- **Linux:** `~/maya/scripts/`
- **Windows:** `%USERPROFILE%\Documents\maya\scripts\`

`userSetup.py` opens commandPort `:9877` every time Maya starts. If you already have a `userSetup.py`, add its contents to yours instead. After restarting Maya, the Script Editor shows `maya-mcp: commandPort :9877 open: True`.

Maya 2022+ may ask in a Safe Mode dialog whether to allow the new `userSetup.py`. Allow it, otherwise Maya skips the file (see **Preferences → Security**).

To activate it without restarting Maya, run this in the Script Editor:

```python
import maya.cmds as cmds
cmds.commandPort(name=":9877", sourceType="python")
```

### 3. Configure your AI client

The AI client starts `maya-mcp` itself. You don't need to run it in a terminal.

**Claude Desktop**: edit `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`) and restart Claude Desktop:

```json
{
  "mcpServers": {
    "maya": {
      "command": "/full/path/to/maya-mcp"
    }
  }
}
```

Use the full path (find it with `which maya-mcp`, usually `~/.local/bin/maya-mcp`). Apps started from the Dock don't get your shell's `PATH`.

**Claude Code CLI:**

```bash
claude mcp add maya --scope user -- maya-mcp
```

**Environment variables** (optional):

| Variable | Default | Description |
|----------|---------|-------------|
| `MAYA_HOST` | `127.0.0.1` | Maya host (Maya's commandPort listens on IPv4 only) |
| `MAYA_PORT` | `9877` | commandPort number |

## Available tools (23)

| Category | Tools |
|----------|-------|
| **Scene** | `get_scene_info`, `get_object_info`, `get_viewport_screenshot`, `execute_maya_code`, `get_selected_objects` |
| **Objects** | `create_object`, `delete_objects`, `duplicate_object`, `rename_object`, `set_parent` |
| **Transforms** | `set_transform`, `get_transform` |
| **Materials** | `create_material`, `assign_material`, `list_materials`, `set_material_property` |
| **Attributes** | `get_attribute`, `set_attribute`, `list_attributes` |
| **Rendering** | `render_scene`, `set_render_settings` |
| **File I/O** | `export_scene`, `import_file` |

`get_scene_info` lists top-level objects in pages of 20 (sorted by name, without Maya's default cameras). Use `name_filter` (e.g. `"spike_*"`), `top_level_only=false`, `limit` (up to 500) and `offset`; `truncated` and `next_offset` tell the agent when there are more.

`render_scene` uses Arnold when it is the active renderer and the Maya Software renderer otherwise. Renders without an `output_path` are deleted after they are returned.

## Example prompts

- "Create a red sphere at position (2, 0, 0)"
- "List all objects in the scene"
- "Take a screenshot of the viewport"
- "Export the selected objects as FBX to /tmp/export.fbx"
- "Set the render resolution to 1920x1080 and render the scene"
- "Create a point light above the scene and set its intensity to 2"

## Troubleshooting

| Message | Cause |
|---------|-------|
| `nothing is listening on 127.0.0.1:9877` | Maya isn't running or the commandPort isn't open. |
| `connecting … timed out` | Maya is busy, or another client is holding the port open. |
| `Maya did not answer … within 30s` | The command is still running in Maya (long operations, renders). |

Claude Desktop logs are in `~/Library/Logs/Claude/mcp-server-maya.log` (macOS).

## Development

```bash
git clone https://github.com/science-animated/maya-mcp
cd maya-mcp
uv tool install --editable .
```

With an editable install, changes to `src/maya_mcp/server.py` take effect when the AI client restarts the server (`/mcp` → reconnect in Claude Code). Changes to `maya_mcp_handlers.py` need a Maya restart or `importlib.reload(maya_mcp_handlers)`.

### Tests

```bash
uv run pytest                  # unit + server tests, no Maya needed
uv run pytest -m integration   # against a running Maya with the commandPort open
```

- `tests/test_handlers.py`: the Maya-side handlers, with `maya.cmds` mocked.
- `tests/test_server.py`: the MCP server against a fake commandPort (connections, timeouts, error messages, tools, and a full stdio session).
- `tests/integration/`: real Maya. Creates `mcp_it_*` nodes and deletes them again, and renders a 64×64 Arnold image if Arnold is the active renderer.

GitHub Actions runs the first two on Linux, macOS and Windows for every push and pull request.

## Supported Maya versions

Maya 2022 and later (Python 3). Tested with Maya 2025 and Arnold (MtoA) 5.6 on macOS.

## License

[MIT](LICENSE)
