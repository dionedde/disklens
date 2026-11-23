"""Entry point for AI clients (MCP over stdio). Configure your client to run:
    <python.exe> "<pasta do DiskLens>\\disklens_mcp.py"
Only the Python standard library is needed.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from disklens.mcp_server import main  # noqa: E402

main()
