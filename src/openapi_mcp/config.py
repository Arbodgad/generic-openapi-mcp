"""Validated YAML configuration for one or more independent API definitions."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AuthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str
    token_env: str | None = None
    username_env: str | None = None
    password_env: str | None = None


class ToolPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    include_methods: list[str] = Field(default_factory=lambda: ["GET"])
    include_paths: list[str] = Field(default_factory=list)
    exclude_paths: list[str] = Field(default_factory=list)
    allow_write: bool = False
    allow_delete: bool = False

    @field_validator("include_methods")
    @classmethod
    def normalize_methods(cls, value: list[str]) -> list[str]:
        methods = list(dict.fromkeys(method.upper() for method in value))
        invalid = set(methods) - {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
        if invalid:
            raise ValueError(f"Unsupported HTTP method(s): {', '.join(sorted(invalid))}")
        return methods


class APIConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spec: str
    base_url: str | None = None
    allow_hosts: list[str] = Field(default_factory=list)
    server_variables: dict[str, str] = Field(default_factory=dict)
    auth: dict[str, AuthConfig] = Field(default_factory=dict)
    tools: ToolPolicy = Field(default_factory=ToolPolicy)
    timeout: float = Field(default=30.0, gt=0, le=300)
    max_response_bytes: int = Field(default=2 * 1024 * 1024, gt=0, le=50 * 1024 * 1024)

    @model_validator(mode="before")
    @classmethod
    def normalize_single_auth(cls, value: Any) -> Any:
        if (
            isinstance(value, dict)
            and isinstance(value.get("auth"), dict)
            and "type" in value["auth"]
        ):
            value = {**value, "auth": {"default": value["auth"]}}
        return value


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    apis: dict[str, APIConfig] = Field(min_length=1)


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    try:
        value: Any = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Could not read configuration {config_path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("Configuration root must be a YAML object")
    config = AppConfig.model_validate(value)
    for api in config.apis.values():
        if not api.spec.startswith(("http://", "https://")):
            spec_path = Path(api.spec).expanduser()
            if not spec_path.is_absolute():
                spec_path = config_path.parent / spec_path
            api.spec = str(spec_path.resolve())
    return config


def secret_from_env(name: str | None, *, required: bool = True) -> str | None:
    if not name:
        if required:
            raise ValueError("Authentication configuration must reference an environment variable")
        return None
    value = os.getenv(name)
    if required and not value:
        raise ValueError(f"Required credential environment variable {name} is not set")
    return value
