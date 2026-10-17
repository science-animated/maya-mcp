# Maya runs this file on startup when it is in a scripts directory, e.g.
# ~/Library/Preferences/Autodesk/maya/scripts/ (macOS). It opens the
# commandPort that maya-mcp talks to.
import maya.cmds as cmds
import maya.utils

MAYA_MCP_PORT = ":9877"


def _open_maya_mcp_port():
    try:
        if not cmds.commandPort(MAYA_MCP_PORT, query=True):
            cmds.commandPort(name=MAYA_MCP_PORT, sourceType="python")
        print("maya-mcp: commandPort %s open: %s" % (MAYA_MCP_PORT, cmds.commandPort(MAYA_MCP_PORT, query=True)))
    except Exception as e:
        cmds.warning("maya-mcp: could not open commandPort %s: %s" % (MAYA_MCP_PORT, e))


print("maya-mcp: userSetup.py loaded, opening commandPort %s when Maya is idle" % MAYA_MCP_PORT)
maya.utils.executeDeferred(_open_maya_mcp_port)
