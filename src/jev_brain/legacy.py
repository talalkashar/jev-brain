"""Console aliases retained for existing agent configurations."""

import sys

from jev_brain import cli as command, mcp_server


def cli():
    print("systemone is deprecated. Use jev-brain.", file=sys.stderr)
    command.main()


def mcp():
    print("systemone-mcp is deprecated. Use jev-brain-mcp.", file=sys.stderr)
    mcp_server.main()
