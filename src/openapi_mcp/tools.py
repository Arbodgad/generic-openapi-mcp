"""MCP tool registry and operation policy enforcement."""

from __future__ import annotations

import json
from typing import Any

from jsonschema import validators

from .client import GenericHTTPClient
from .config import APIConfig
from .openapi import Operation


class ToolExecutionError(RuntimeError):
    """A tool was unknown, disallowed, or called with invalid arguments."""


def _validator(schema: dict[str, Any]) -> Any:
    validator_type = validators.validator_for(schema)
    validator_type.check_schema(schema)
    return validator_type(schema)


class DynamicToolRegistry:
    def __init__(
        self,
        operations: list[Operation],
        clients: dict[str, GenericHTTPClient],
        configs: dict[str, APIConfig],
    ):
        self.operations = operations
        self.clients = clients
        self.configs = configs
        self._by_name = {op.tool_name: op for op in operations}
        # Building jsonschema validators is expensive for large OpenAPI documents.
        # Compile each operation's validator only when that tool is actually called.
        self._validators: dict[str, Any] = {}

    def enabled(self, operation: Operation) -> bool:
        policy = self.configs[operation.api_id].tools
        if operation.method not in policy.include_methods:
            return False
        if operation.method in {"POST", "PUT", "PATCH"} and not policy.allow_write:
            return False
        if operation.method == "DELETE" and not policy.allow_delete:
            return False
        if policy.include_paths and not any(
            operation.path.startswith(p) for p in policy.include_paths
        ):
            return False
        return not any(operation.path.startswith(p) for p in policy.exclude_paths)

    def visible_operations(self) -> list[Operation]:
        return [op for op in self.operations if self.enabled(op)]

    def mcp_tools(self) -> list[Any]:
        from mcp.types import Tool

        return [
            Tool(name=op.tool_name, description=op.description, inputSchema=op.input_schema)
            for op in self.visible_operations()
        ]

    async def call(self, name: str, arguments: dict[str, Any] | None) -> Any:
        operation = self._by_name.get(name)
        if operation is None:
            raise ToolExecutionError(f"Unknown tool {name!r}")
        if not self.enabled(operation):
            raise ToolExecutionError(
                f"Tool {name!r} is disabled by the configured method/path policy"
            )
        values = arguments or {}
        validator = self._validators.get(name)
        if validator is None:
            validator = _validator(operation.input_schema)
            self._validators[name] = validator
        errors = sorted(validator.iter_errors(values), key=lambda e: list(e.path))
        if errors:
            raise ToolExecutionError(
                f"Invalid arguments for {name}: " + "; ".join(e.message for e in errors[:5])
            )
        return await self.clients[operation.api_id].request(operation, values)

    @staticmethod
    def render_result(value: Any) -> str:
        return json.dumps(value, indent=2, ensure_ascii=False, default=str)
