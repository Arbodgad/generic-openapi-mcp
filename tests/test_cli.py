import json

from openapi_mcp.cli import main


def test_validate_command_uses_config_relative_spec(tmp_path, capsys):
    spec = tmp_path / "api.json"
    spec.write_text(
        json.dumps(
            {
                "openapi": "3.0.1",
                "servers": [{"url": "https://api.example.test"}],
                "paths": {"/items": {"get": {"responses": {"200": {"description": "ok"}}}}},
            }
        ),
        encoding="utf-8",
    )
    config = tmp_path / "openapi-mcp.yaml"
    config.write_text("apis:\n  demo:\n    spec: ./api.json\n", encoding="utf-8")
    assert main(["--config", str(config), "validate"]) == 0
    assert "demo: valid document, 1 operations" in capsys.readouterr().out


def test_validate_returns_nonzero_for_unsupported_spec(tmp_path, capsys):
    spec = tmp_path / "api.json"
    spec.write_text('{"openapi":"4.0.0","paths":{}}', encoding="utf-8")
    config = tmp_path / "openapi-mcp.yaml"
    config.write_text("apis:\n  demo:\n    spec: ./api.json\n", encoding="utf-8")
    assert main(["--config", str(config), "validate"]) == 1
    assert "Unsupported OpenAPI version" in capsys.readouterr().err


def test_list_tools_uses_default_get_policy(tmp_path, capsys):
    spec = tmp_path / "api.json"
    spec.write_text(
        json.dumps(
            {
                "openapi": "3.0.1",
                "servers": [{"url": "https://api.example.test"}],
                "paths": {
                    "/items": {
                        "get": {
                            "summary": "List items",
                            "responses": {"200": {"description": "ok"}},
                        },
                        "delete": {
                            "responses": {"204": {"description": "ok"}},
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    config = tmp_path / "openapi-mcp.yaml"
    config.write_text("apis:\n  demo:\n    spec: ./api.json\n", encoding="utf-8")
    assert main(["--config", str(config), "list-tools"]) == 0
    output = capsys.readouterr().out
    assert "demo_get_items" in output
    assert "delete_items" not in output
