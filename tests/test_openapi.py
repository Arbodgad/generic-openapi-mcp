from openapi_mcp.openapi import base_url_from_spec, parse_spec


def fixture(version="3.0.1"):
    return {
        "openapi": version,
        "servers": [{"url": "https://example.test/api"}],
        "paths": {
            "/issues/{issueID}": {
                "parameters": [{"name": "fields", "in": "query", "schema": {"type": "string"}}],
                "get": {
                    "parameters": [
                        {
                            "name": "issueID",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {"200": {"description": "ok"}},
                },
            }
        },
    }


def test_openapi_30_and_31_generate_stable_names_and_valid_required():
    for version in ("3.0.1", "3.1.0"):
        first = parse_spec(fixture(version), api_id="youtrack")
        second = parse_spec(fixture(version), api_id="youtrack")
        assert first[0].tool_name == "youtrack_get_issues_by_issue_id"
        assert first[0].tool_name == second[0].tool_name
        assert first[0].input_schema["required"] == ["issueID"]
        assert "fields" in first[0].input_schema["properties"]


def test_operation_id_is_used_and_name_collisions_are_deterministic():
    doc = fixture()
    doc["paths"]["/issues/{issueID}"]["get"]["operationId"] = "findIssue"
    doc["paths"]["/other"] = {}
    doc["paths"]["/other"]["get"] = {"operationId": "findIssue"}
    operations = parse_spec(doc, api_id="x")
    assert [operation.tool_name for operation in operations] == ["x_find_issue", "x_find_issue_2"]


def test_swagger_20_is_recognized():
    doc = {
        "swagger": "2.0",
        "host": "example.test",
        "basePath": "/v1",
        "schemes": ["https"],
        "paths": {
            "/items": {
                "get": {
                    "parameters": [
                        {"name": "limit", "in": "query", "type": "integer", "required": True}
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    operation = parse_spec(doc, api_id="demo")[0]
    assert operation.tool_name == "demo_get_items"
    assert operation.input_schema["properties"]["limit"]["type"] == "integer"
    assert operation.input_schema["required"] == ["limit"]


def test_parse_spec_skips_methods_outside_configured_tool_policy():
    doc = {
        "openapi": "3.0.1",
        "paths": {
            "/items": {
                "get": {"responses": {"200": {"description": "ok"}}},
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/UnusedWritePayload"}
                            }
                        }
                    },
                    "responses": {"201": {"description": "created"}},
                },
            }
        },
    }

    operations = parse_spec(doc, include_methods={"GET"})

    assert [operation.method for operation in operations] == ["GET"]


def test_local_refs_cycles_and_body_required_are_handled():
    doc = {
        "openapi": "3.1.0",
        "components": {
            "schemas": {
                "Node": {
                    "type": "object",
                    "properties": {"next": {"$ref": "#/components/schemas/Node"}},
                },
                "Payload": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "node": {"$ref": "#/components/schemas/Node"},
                    },
                    "required": ["title"],
                },
            }
        },
        "paths": {
            "/nodes": {
                "post": {
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Payload"}}
                        },
                    },
                    "responses": {"204": {"description": "ok"}},
                }
            }
        },
    }
    operation = parse_spec(doc, "demo")[0]
    assert operation.input_schema["required"] == ["body"]
    assert operation.input_schema["properties"]["body"]["required"] == ["title"]
    assert isinstance(operation.input_schema["properties"]["body"]["required"], list)
    recursive = operation.input_schema["properties"]["body"]["properties"]["node"]
    assert recursive["properties"]["next"]["additionalProperties"] is True


def test_unknown_local_reference_fails_with_clear_diagnostic():
    doc = {
        "openapi": "3.0.1",
        "paths": {
            "/things": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Missing"}}
                        }
                    },
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    try:
        parse_spec(doc)
    except Exception as exc:
        assert "Unresolved local reference" in str(exc)
    else:
        raise AssertionError("unknown reference should be rejected")


def test_base_url_from_scoped_server_and_server_variables():
    doc = {
        "openapi": "3.0.1",
        "paths": {
            "/items": {
                "servers": [
                    {
                        "url": "https://{tenant}.example.test/api",
                        "variables": {"tenant": {"default": "public"}},
                    }
                ],
                "get": {"responses": {"200": {"description": "ok"}}},
            }
        },
    }
    assert base_url_from_spec(doc) == "https://public.example.test/api"
    assert (
        base_url_from_spec(doc, variables={"tenant": "team one"})
        == "https://team%20one.example.test/api"
    )
