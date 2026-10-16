"""Load Swagger/OpenAPI documents and turn operations into stable MCP tool specs."""

from __future__ import annotations

import copy
import ipaddress
import json
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx
import yaml

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")
MAX_SPEC_BYTES = 5 * 1024 * 1024


class SpecError(ValueError):
    """A specification could not be loaded, validated, or represented safely."""


@dataclass
class Operation:
    api_id: str
    method: str
    path: str
    tool_name: str
    description: str
    input_schema: dict[str, Any]
    parameters: list[dict[str, Any]] = field(default_factory=list)
    request_body: dict[str, Any] | None = None
    security: list[dict[str, list[str]]] | None = None
    servers: list[dict[str, Any]] | None = None


def _pointer(document: dict[str, Any], ref: str) -> Any:
    if not ref.startswith("#/"):
        raise SpecError(f"External $ref is not supported: {ref}")
    value: Any = document
    for part in ref[2:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        try:
            value = value[int(key)] if isinstance(value, list) else value[key]
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise SpecError(f"Unresolved local reference: {ref}") from exc
    return value


def resolve_refs(value: Any, document: dict[str, Any], stack: tuple[str, ...] = ()) -> Any:
    """Resolve local refs while retaining recursive schemas as safe open objects."""
    if isinstance(value, dict):
        ref = value.get("$ref")
        if ref:
            if ref in stack:
                return {"type": "object", "additionalProperties": True}
            target = copy.deepcopy(_pointer(document, ref))
            siblings = {k: v for k, v in value.items() if k != "$ref"}
            result = resolve_refs(target, document, (*stack, ref))
            if siblings and isinstance(result, dict):
                result.update(resolve_refs(siblings, document, stack))
            return result
        return {k: resolve_refs(v, document, stack) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_refs(item, document, stack) for item in value]
    return value


def _mcp_schema(
    schema: Any, document: dict[str, Any], stack: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Convert a schema subset to MCP-compatible JSON Schema, preserving required lists."""
    if not isinstance(schema, dict):
        return {"type": "object", "additionalProperties": True}
    ref = schema.get("$ref")
    if ref:
        if ref in stack:
            return {"type": "object", "additionalProperties": True}
        return _mcp_schema(_pointer(document, ref), document, (*stack, ref))
    result = {
        k: copy.deepcopy(v)
        for k, v in schema.items()
        if k
        in {
            "type",
            "title",
            "description",
            "default",
            "enum",
            "const",
            "format",
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
            "multipleOf",
            "minLength",
            "maxLength",
            "pattern",
            "minItems",
            "maxItems",
            "uniqueItems",
            "minProperties",
            "maxProperties",
            "additionalProperties",
        }
    }
    if "nullable" in schema and schema["nullable"]:
        typ = result.get("type")
        result["type"] = [typ, "null"] if isinstance(typ, str) else typ
    if isinstance(schema.get("type"), list):
        result["type"] = schema["type"]
    if "properties" in schema:
        result["type"] = "object"
        result["properties"] = {
            name: _mcp_schema(child, document, stack)
            for name, child in schema["properties"].items()
        }
    required = schema.get("required", [])
    if isinstance(required, list) and all(isinstance(x, str) for x in required) and required:
        result["required"] = required
    if "items" in schema:
        result["items"] = _mcp_schema(schema["items"], document, stack)
    for keyword in ("oneOf", "anyOf", "allOf"):
        if isinstance(schema.get(keyword), list):
            result[keyword] = [_mcp_schema(x, document, stack) for x in schema[keyword]]
    return result


def _snake(text: str) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    return text or "operation"


def _fallback_name(method: str, path: str) -> str:
    chunks = []
    for part in path.strip("/").split("/"):
        if part.startswith("{") and part.endswith("}"):
            chunks.extend(("by", _snake(part[1:-1])))
        elif part:
            chunks.append(_snake(part))
    return _snake(method + "_" + "_".join(chunks))


def _server_url(server: dict[str, Any], variables: dict[str, str]) -> str:
    url = server.get("url", "")
    for name, config in server.get("variables", {}).items():
        value = variables.get(name, config.get("default"))
        if value is None:
            raise SpecError(f"No value supplied for server variable {name!r}")
        url = url.replace("{" + name + "}", quote(str(value), safe=""))
    return url


def base_url_from_spec(
    document: dict[str, Any], override: str | None = None, variables: dict[str, str] | None = None
) -> str:
    if override:
        url = override.rstrip("/")
        _validate_base_url(url)
        return url
    servers = document.get("servers") or []
    if servers:
        url = _server_url(servers[0], variables or {}).rstrip("/")
        _validate_base_url(url)
        return url
    for path_item in document.get("paths", {}).values():
        if not isinstance(path_item, dict):
            continue
        scoped_servers = path_item.get("servers")
        if not scoped_servers:
            scoped_servers = next(
                (
                    operation.get("servers")
                    for method, operation in path_item.items()
                    if method.lower() in HTTP_METHODS
                    and isinstance(operation, dict)
                    and operation.get("servers")
                ),
                None,
            )
        if scoped_servers:
            url = _server_url(scoped_servers[0], variables or {}).rstrip("/")
            _validate_base_url(url)
            return url
    if document.get("swagger") == "2.0" and document.get("host"):
        schemes = document.get("schemes") or ["https"]
        url = f"{schemes[0]}://{document['host']}{document.get('basePath', '')}".rstrip("/")
        _validate_base_url(url)
        return url
    raise SpecError("No base URL is configured and the specification has no usable server URL")


def _validate_base_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SpecError("API base URLs must use HTTP or HTTPS and include a host")
    if parsed.username or parsed.password:
        raise SpecError("Do not put credentials in an API base URL; use the auth configuration")
    if parsed.query or parsed.fragment:
        raise SpecError("API base URLs must not contain a query string or fragment")


def parse_spec(
    document: dict[str, Any],
    api_id: str = "api",
    server_variables: dict[str, str] | None = None,
    include_methods: set[str] | None = None,
) -> list[Operation]:
    if not isinstance(document, dict):
        raise SpecError("Specification root must be a JSON/YAML object")
    version = document.get("openapi")
    if version and not (str(version).startswith("3.0.") or str(version).startswith("3.1.")):
        raise SpecError(f"Unsupported OpenAPI version: {version}")
    if not version and document.get("swagger") != "2.0":
        raise SpecError("Expected Swagger 2.0 or OpenAPI 3.0.x/3.1.x document")
    paths = document.get("paths")
    if not isinstance(paths, dict):
        raise SpecError("Specification must contain a paths object")
    used: set[str] = set()
    operations: list[Operation] = []
    for path, raw_item in paths.items():
        # Avoid resolving an entire path item up front: that recursively expands
        # every operation's response schemas, which are not used to build MCP
        # input tools and can be very large in generated specifications.
        item = (
            resolve_refs(raw_item, document)
            if isinstance(raw_item, dict) and "$ref" in raw_item
            else raw_item
        )
        if not isinstance(item, dict):
            continue
        inherited = [
            resolve_refs(parameter, document)
            if isinstance(parameter, dict) and "$ref" in parameter
            else parameter
            for parameter in item.get("parameters", [])
        ]
        for method in HTTP_METHODS:
            if include_methods is not None and method.upper() not in include_methods:
                continue
            raw = item.get(method)
            if not isinstance(raw, dict):
                continue
            # Only resolve fields used below. Resolving the whole operation also
            # walks its response models, even though MCP tools only need inputs.
            if "$ref" in raw:
                op = resolve_refs(raw, document)
            else:
                op = dict(raw)
                op["parameters"] = [
                    resolve_refs(parameter, document)
                    if isinstance(parameter, dict) and "$ref" in parameter
                    else parameter
                    for parameter in raw.get("parameters", [])
                ]
                if isinstance(raw.get("requestBody"), dict) and "$ref" in raw["requestBody"]:
                    op["requestBody"] = resolve_refs(raw["requestBody"], document)
            parameters = list(inherited) + list(op.get("parameters", []))
            # Operation parameters override path parameters with same (name, in).
            combined: dict[tuple[str, str], dict[str, Any]] = {}
            for parameter in parameters:
                if isinstance(parameter, dict):
                    combined[(parameter.get("name", ""), parameter.get("in", ""))] = parameter
            parameters = list(combined.values())
            if not version:
                for parameter in parameters:
                    collection = parameter.get("collectionFormat")
                    if collection is None and parameter.get("type") == "array":
                        collection = "csv"
                    if collection in {"csv", "ssv", "pipes"}:
                        parameter["style"] = (
                            "simple"
                            if parameter.get("in") in {"path", "header"}
                            else {"csv": "form", "ssv": "spaceDelimited", "pipes": "pipeDelimited"}[
                                collection
                            ]
                        )
                        parameter["explode"] = False
                    elif collection == "multi":
                        parameter["style"], parameter["explode"] = (
                            ("simple", False)
                            if parameter.get("in") in {"path", "header"}
                            else ("form", True)
                        )
            properties: dict[str, Any] = {}
            required: list[str] = []
            for parameter in parameters:
                location = parameter.get("in")
                if location not in {"path", "query", "header", "cookie"}:
                    continue
                if "content" in parameter:
                    raise SpecError(
                        f"Parameter content serialization is not supported: {method.upper()} {path}"
                    )
                name = parameter.get("name")
                if not name:
                    continue
                schema = parameter.get("schema") or {
                    k: v for k, v in parameter.items() if k in ("type", "format", "items", "enum")
                }
                properties[name] = _mcp_schema(schema, document)
                if parameter.get("required") or location == "path":
                    required.append(name)
                if parameter.get("description"):
                    properties[name]["description"] = parameter["description"]
            request_body = op.get("requestBody")
            body_mode = None
            body_required = False
            if request_body and isinstance(request_body, dict):
                content = request_body.get("content", {})
                media = next(
                    (
                        m
                        for m in (
                            "application/json",
                            "application/x-www-form-urlencoded",
                            "multipart/form-data",
                            "text/plain",
                        )
                        if m in content
                    ),
                    None,
                )
                if media:
                    body_mode = media
                    body_schema = _mcp_schema(content[media].get("schema", {}), document)
                    properties["body"] = body_schema
                    body_required = bool(request_body.get("required"))
            # Swagger 2 body/formData normalization.
            if not version:
                body_parameter = next((p for p in parameters if p.get("in") == "body"), None)
                forms = [p for p in parameters if p.get("in") == "formData"]
                if body_parameter:
                    properties["body"] = _mcp_schema(body_parameter.get("schema", {}), document)
                    body_mode = "application/json"
                    body_required = bool(body_parameter.get("required"))
                elif forms:
                    properties["body"] = {
                        "type": "object",
                        "properties": {
                            p["name"]: _mcp_schema(p, document) for p in forms if p.get("name")
                        },
                        **(
                            {
                                "required": [
                                    p["name"] for p in forms if p.get("required") and p.get("name")
                                ]
                            }
                            if any(p.get("required") for p in forms)
                            else {}
                        ),
                    }
                    body_mode = (
                        "multipart/form-data"
                        if any(
                            "multipart/form-data" in x
                            for x in op.get("consumes", document.get("consumes", []))
                        )
                        else "application/x-www-form-urlencoded"
                    )
                    body_required = any(p.get("required") for p in forms)
            if body_required:
                required.append("body")
            op_id = op.get("operationId")
            base = _snake(str(op_id)) if op_id else _fallback_name(method, path)
            if op_id and not base.startswith(method + "_"):
                # Keep explicit operationIds readable; add verb only if not already present.
                pass
            if base in used:
                base = f"{base}_{method}"
            candidate = f"{_snake(api_id)}_{base}" if api_id else base
            suffix = 2
            unique = candidate
            while unique in used:
                unique = f"{candidate}_{suffix}"
                suffix += 1
            used.add(unique)
            summary = op.get("summary", "")
            desc = op.get("description", "")
            description = " ".join(x.strip() for x in (summary, desc) if x and x.strip())
            if not description:
                description = f"Call {method.upper()} {path}."
            operations.append(
                Operation(
                    api_id=api_id,
                    method=method.upper(),
                    path=path,
                    tool_name=unique,
                    description=description,
                    input_schema={
                        "type": "object",
                        "properties": properties,
                        **({"required": list(dict.fromkeys(required))} if required else {}),
                    },
                    parameters=parameters,
                    request_body={"mode": body_mode, "required": body_required}
                    if body_mode
                    else None,
                    security=op.get("security", document.get("security", [])),
                    servers=op.get("servers") or item.get("servers"),
                )
            )
    return operations


def _check_remote_host(url: str, allow_hosts: set[str]) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SpecError("Specification URLs must use HTTP or HTTPS")
    if parsed.username or parsed.password:
        raise SpecError("Credentials must not be embedded in a specification URL")
    host = parsed.hostname.lower()
    if host in allow_hosts:
        return
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addresses = [ipaddress.ip_address(row[4][0]) for row in socket.getaddrinfo(host, None)]
        except OSError as exc:
            raise SpecError(f"Could not resolve specification host {host!r}") from exc
    if any(
        address.is_private or address.is_loopback or address.is_link_local or address.is_reserved
        for address in addresses
    ):
        raise SpecError(f"Private network host {host!r} must be explicitly listed in allow_hosts")


def load_spec(source: str, allow_hosts: set[str] | None = None) -> dict[str, Any]:
    """Read a bounded local JSON/YAML file or a bounded remote HTTP(S) document."""
    allow_hosts = {h.lower() for h in (allow_hosts or set())}
    if source.startswith(("http://", "https://")):
        _check_remote_host(source, allow_hosts)
        try:
            with httpx.Client(timeout=httpx.Timeout(10, read=20), follow_redirects=False) as client:
                with client.stream("GET", source) as response:
                    response.raise_for_status()
                    length = int(response.headers.get("content-length", "0"))
                    if length > MAX_SPEC_BYTES:
                        raise SpecError("OpenAPI document exceeds the 5 MiB size limit")
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        content.extend(chunk)
                        if len(content) > MAX_SPEC_BYTES:
                            raise SpecError("OpenAPI document exceeds the 5 MiB size limit")
        except httpx.HTTPError as exc:
            parsed = urlparse(source)
            safe_source = f"{parsed.scheme}://{parsed.hostname or ''}{parsed.path}"
            raise SpecError(
                f"Could not download specification from {safe_source}: {type(exc).__name__}"
            ) from exc
        raw = bytes(content).decode("utf-8")
    else:
        path = Path(source).expanduser()
        try:
            if path.stat().st_size > MAX_SPEC_BYTES:
                raise SpecError("OpenAPI document exceeds the 5 MiB size limit")
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SpecError(f"Could not read specification {path}: {exc}") from exc
    try:
        data = json.loads(raw) if raw.lstrip().startswith(("{", "[")) else yaml.safe_load(raw)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise SpecError(f"Specification is not valid JSON or YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise SpecError("Specification root must be an object")
    return data
