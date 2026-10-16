import asyncio
import os

import httpx
import pytest

from openapi_mcp.client import GenericHTTPClient
from openapi_mcp.config import APIConfig, AuthConfig, ToolPolicy
from openapi_mcp.openapi import parse_spec
from openapi_mcp.tools import DynamicToolRegistry


@pytest.mark.skipif(
    bool(os.getenv("CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY")),
    reason="This Windows sandbox blocks asyncio's local socketpair",
)
def test_http_request_path_query_bearer_and_json(monkeypatch):
    monkeypatch.setenv("DEMO_TOKEN", "secret-token")
    document = {
        "openapi": "3.0.1",
        "components": {"securitySchemes": {"access": {"type": "http", "scheme": "bearer"}}},
        "security": [{"access": []}],
        "paths": {
            "/items/{itemId}": {
                "post": {
                    "parameters": [
                        {
                            "name": "itemId",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {"name": "fields", "in": "query", "schema": {"type": "string"}},
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"name": {"type": "string"}},
                                    "required": ["name"],
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    api_config = APIConfig(
        spec="unused",
        allow_hosts=["internal.test"],
        tools=ToolPolicy(include_methods=["POST"], allow_write=True),
        auth={"access": AuthConfig(type="bearer", token_env="DEMO_TOKEN")},
    )
    observed = {}

    async def handler(request):
        observed["url"] = str(request.url)
        observed["auth"] = request.headers["Authorization"]
        observed["body"] = request.content.decode()
        return httpx.Response(200, json={"accepted": True})

    async def exercise():
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        operation = parse_spec(document, "demo")[0]
        client = GenericHTTPClient("demo", api_config, document, "https://internal.test/api", http)
        result = await client.request(
            operation, {"itemId": "a b", "fields": "id,name", "body": {"name": "test"}}
        )
        await http.aclose()
        return result

    result = asyncio.run(exercise())
    assert result == {"accepted": True}
    assert observed["url"] == "https://internal.test/api/items/a%20b?fields=id%2Cname"
    assert observed["auth"] == "Bearer secret-token"
    assert observed["body"] == '{"name":"test"}'


def test_policy_hides_write_and_delete_by_default():
    document = {
        "openapi": "3.0.1",
        "paths": {
            "/items": {
                "get": {"responses": {"200": {"description": "ok"}}},
                "delete": {"responses": {"204": {"description": "ok"}}},
            }
        },
    }
    api_config = APIConfig(spec="unused")
    operation = parse_spec(document, "demo")[0]
    delete = parse_spec(document, "demo")[1]
    client = GenericHTTPClient("demo", api_config, document, "https://example.test")
    registry = DynamicToolRegistry([operation, delete], {"demo": client}, {"demo": api_config})
    assert [op.method for op in registry.visible_operations()] == ["GET"]
    assert not registry.enabled(delete)
    assert registry._validators == {}


def test_api_key_security_does_not_expose_secret_as_tool_parameter(monkeypatch):
    monkeypatch.setenv("DEMO_KEY", "hidden")
    document = {
        "openapi": "3.0.1",
        "components": {
            "securitySchemes": {"key": {"type": "apiKey", "in": "query", "name": "api_key"}}
        },
        "paths": {
            "/items": {
                "get": {"security": [{"key": []}], "responses": {"200": {"description": "ok"}}}
            }
        },
    }
    config = APIConfig(
        spec="unused", auth={"key": AuthConfig(type="api_key", token_env="DEMO_KEY")}
    )
    operation = parse_spec(document, "demo")[0]
    client = GenericHTTPClient("demo", config, document, "https://example.test")
    query = []
    client._auth(operation, query, {}, {})
    assert query == [("api_key", "hidden")]
    assert "api_key" not in operation.input_schema["properties"]


def test_multi_api_names_and_bearer_credentials_are_isolated(monkeypatch):
    monkeypatch.setenv("FIRST_TOKEN", "first-secret")
    monkeypatch.setenv("SECOND_TOKEN", "second-secret")
    document = {
        "openapi": "3.0.1",
        "components": {"securitySchemes": {"access": {"type": "http", "scheme": "bearer"}}},
        "paths": {
            "/items": {
                "get": {"security": [{"access": []}], "responses": {"200": {"description": "ok"}}}
            }
        },
    }
    first_operation = parse_spec(document, "first")[0]
    second_operation = parse_spec(document, "second")[0]
    first = GenericHTTPClient(
        "first",
        APIConfig(
            spec="unused", auth={"access": AuthConfig(type="bearer", token_env="FIRST_TOKEN")}
        ),
        document,
        "https://api.example.test",
    )
    second = GenericHTTPClient(
        "second",
        APIConfig(
            spec="unused", auth={"access": AuthConfig(type="bearer", token_env="SECOND_TOKEN")}
        ),
        document,
        "https://api.example.test",
    )
    first_headers, second_headers = {}, {}
    first._auth(first_operation, [], first_headers, {})
    second._auth(second_operation, [], second_headers, {})
    assert first_operation.tool_name == "first_get_items"
    assert second_operation.tool_name == "second_get_items"
    assert first_headers["Authorization"] == "Bearer first-secret"
    assert second_headers["Authorization"] == "Bearer second-secret"
