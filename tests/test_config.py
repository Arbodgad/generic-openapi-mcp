from openapi_mcp.config import APIConfig, ToolPolicy


def test_single_auth_profile_and_safe_default_policy():
    config = APIConfig.model_validate(
        {"spec": "spec.json", "auth": {"type": "bearer", "token_env": "API_TOKEN"}}
    )
    assert config.auth["default"].type == "bearer"
    assert config.tools.include_methods == ["GET"]
    assert config.tools.allow_write is False
    assert config.tools.allow_delete is False


def test_method_policy_normalizes_methods():
    assert ToolPolicy(include_methods=["get", "post"], allow_write=True).include_methods == [
        "GET",
        "POST",
    ]
