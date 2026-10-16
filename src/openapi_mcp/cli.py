"""Command line interface for validation, tool inspection, and MCP STDIO."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .config import load_config
from .openapi import HTTP_METHODS, base_url_from_spec, load_spec, parse_spec
from .server import build_registry, run_stdio


def _print_tools(config_path: str, schemas: bool = False) -> int:
    config = load_config(config_path)
    registry, _ = build_registry(config)
    for operation in registry.visible_operations():
        print(f"{operation.method:7} {operation.path:36} {operation.tool_name}")
        print(f"        {operation.description}")
        if schemas:
            print(json.dumps(operation.input_schema, indent=2, ensure_ascii=False))
    print(f"\n{len(registry.visible_operations())} generated tools")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generic OpenAPI to MCP STDIO server")
    parser.add_argument("--config", default="openapi-mcp.yaml", help="YAML API configuration")
    parser.add_argument("--version", action="version", version="0.1.0")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("validate", help="Validate configured specifications and policies")
    listing = commands.add_parser("list-tools", help="List generated MCP tools")
    listing.add_argument("--schemas", action="store_true", help="Display generated JSON Schema")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "list-tools":
            return _print_tools(args.config, args.schemas)
        config = load_config(args.config)
        if args.command == "validate":
            for api_id, api in config.apis.items():
                document = load_spec(api.spec, {h.lower() for h in api.allow_hosts})
                methods = set(api.tools.include_methods)
                if not api.tools.allow_write:
                    methods.difference_update({"POST", "PUT", "PATCH"})
                if not api.tools.allow_delete:
                    methods.discard("DELETE")
                parse_spec(
                    document,
                    api_id,
                    api.server_variables,
                    include_methods=methods,
                )
                base_url_from_spec(document, api.base_url, api.server_variables)
                count = sum(
                    1
                    for path_item in document["paths"].values()
                    if isinstance(path_item, dict)
                    for method in HTTP_METHODS
                    if isinstance(path_item.get(method), dict)
                )
                print(f"{api_id}: valid document, {count} operations")
            return 0
        asyncio.run(run_stdio(config))
        return 0
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
