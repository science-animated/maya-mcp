"""Maya 3D integration through the Model Context Protocol."""
__version__ = "1.0.0"


def main():
    from .server import mcp
    mcp.run()
