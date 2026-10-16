"""MCP STDIO server that loads API tools before opening the transport."""

from __future__ import annotations

import logging
import sys
from typing import Any

from .client import APIError, GenericHTTPClient
from .config import AppConfig
from .openapi import base_url_from_spec, load_spec, parse_spec
from .tools import DynamicToolRegistry, ToolExecutionError

LOGGER = logging.getLogger(__name__)


def build_registry(config: AppConfig) -> tuple[DynamicToolRegistry, list[dict[str, Any]]]:
    clients: dict[str, GenericHTTPClient] = {}
    configs = config.apis
    operations = []
    documents = []
    for api_id, api_config in config.apis.items():
        allowed = {host.lower() for host in api_config.allow_hosts}
        document = load_spec(api_config.spec, allowed)
        methods = set(api_config.tools.include_methods)
        if not api_config.tools.allow_write:
            methods.difference_update({"POST", "PUT", "PATCH"})
        if not api_config.tools.allow_delete:
            methods.discard("DELETE")
        api_operations = parse_spec(
            document,
            api_id=api_id,
            server_variables=api_config.server_variables,
            include_methods=methods,
        )
        base_url = base_url_from_spec(document, api_config.base_url, api_config.server_variables)
        clients[api_id] = GenericHTTPClient(api_id, api_config, document, base_url)
        operations.extend(api_operations)
        documents.append(document)
    return DynamicToolRegistry(operations, clients, configs), documents


async def run_stdio(config: AppConfig) -> None:
    from mcp.server import Server
    from mcp.server.lowlevel.server import NotificationOptions
    from mcp.server.models import InitializationOptions
    from mcp.server.stdio import stdio_server
    from mcp.types import CallToolResult, TextContent

    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    registry, _ = build_registry(config)
    server = Server("generic-openapi-mcp")

    @server.list_tools()
    async def list_tools() -> list[Any]:
        return registry.mcp_tools()

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> CallToolResult:
        try:
            result = await registry.call(name, arguments)
            return CallToolResult(
                content=[TextContent(type="text", text=registry.render_result(result))],
                structuredContent=result if isinstance(result, dict) else None,
            )
        except (APIError, ToolExecutionError, ValueError) as exc:
            return CallToolResult(content=[TextContent(type="text", text=str(exc))], isError=True)
        except Exception:
            LOGGER.exception("Unexpected failure while calling tool %s", name)
            return CallToolResult(
                content=[TextContent(type="text", text="Unexpected internal server error")],
                isError=True,
            )

    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="generic-openapi-mcp",
                    server_version="0.1.0",
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(), experimental_capabilities={}
                    ),
                ),
            )
    finally:
        for client in registry.clients.values():
            await client.close()
