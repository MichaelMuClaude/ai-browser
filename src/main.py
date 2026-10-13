#!/usr/bin/env python3
"""AI Browser — unified entry point.

Usage:
  python -m src.main server    # Start HTTP API server (default)
  python -m src.main mcp       # Start MCP server (for Claude Desktop, etc.)
  python -m src.main cli <cmd> # CLI tool
"""
import sys
import asyncio


def run_server():
    from .server import main
    main()


def run_mcp():
    from .mcp_server import main as mcp_main
    mcp_main()


def run_cli():
    from .cli import main as cli_main
    cli_main()


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m src.main [server|mcp|cli] [args...]")
        print()
        print("  server    Start HTTP API server (default)")
        print("  mcp       Start MCP server (stdio, for Claude Desktop etc.)")
        print("  cli       Run CLI command (e.g. cli health, cli navigate <url>)")
        sys.exit(1)

    mode = sys.argv[1]

    if mode == "server":
        run_server()
    elif mode == "mcp":
        run_mcp()
    elif mode == "cli":
        # Remove 'cli' from argv so CLI argparse works
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        run_cli()
    else:
        # If first arg looks like a CLI command, run CLI
        sys.argv = [sys.argv[0]] + sys.argv[1:]
        run_cli()


if __name__ == "__main__":
    main()
