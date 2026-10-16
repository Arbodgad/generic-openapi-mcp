"""Generic HTTP execution for normalized OpenAPI operations."""

from __future__ import annotations

import base64
import ipaddress
import json
import logging
import socket
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import httpx

from .config import APIConfig, AuthConfig, secret_from_env
from .openapi import Operation

LOGGER = logging.getLogger(__name__)


class APIError(RuntimeError):
    """Safe HTTP/API failure with useful endpoint context."""


def _validate_target(url: str, allow_hosts: set[str]) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise APIError("Configured API URL must use HTTP or HTTPS")
    if parsed.username or parsed.password:
        raise APIError("Credentials must be configured through an OpenAPI security scheme")
    if parsed.query or parsed.fragment:
        raise APIError("API base URLs must not contain a query string or fragment")
    host = parsed.hostname.lower()
    if host in allow_hosts:
        return
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addresses = [ipaddress.ip_address(row[4][0]) for row in socket.getaddrinfo(host, None)]
        except OSError as exc:
            raise APIError(f"Could not resolve API host {host!r}") from exc
    if any(a.is_private or a.is_loopback or a.is_link_local or a.is_reserved for a in addresses):
        raise APIError(f"Private network host {host!r} must be explicitly listed in allow_hosts")


def _api_key_location(scheme: dict[str, Any]) -> tuple[str, str]:
    location, name = scheme.get("in"), scheme.get("name")
    if location not in {"header", "query", "cookie"} or not name:
        raise APIError("OpenAPI API key scheme has an unsupported location")
    return location, name


class GenericHTTPClient:
    def __init__(
        self,
        api_id: str,
        config: APIConfig,
        document: dict[str, Any],
        base_url: str,
        http_client: httpx.AsyncClient | None = None,
    ):
        self.api_id = api_id
        self.config = config
        self.document = document
        self.base_url = base_url.rstrip("/")
        self._http = http_client
        self._owns_http = http_client is None

    async def close(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    def _security_schemes(self) -> dict[str, dict[str, Any]]:
        return self.document.get("components", {}).get("securitySchemes") or self.document.get(
            "securityDefinitions", {}
        )

    def _auth(
        self,
        operation: Operation,
        query: list[tuple[str, Any]],
        headers: dict[str, str],
        cookies: dict[str, str],
    ) -> list[str]:
        requirements = operation.security
        if not requirements:
            return []
        schemes = self._security_schemes()
        errors: list[str] = []
        for alternative in requirements:
            alternative_query = list(query)
            alternative_headers = dict(headers)
            alternative_cookies = dict(cookies)
            secrets: list[str] = []
            try:
                for name in alternative:
                    scheme = schemes.get(name)
                    if not scheme:
                        raise APIError(f"Unknown OpenAPI security scheme {name!r}")
                    auth_config = self.config.auth.get(name) or self.config.auth.get("default")
                    secret = self._apply_auth(
                        scheme,
                        auth_config,
                        alternative_query,
                        alternative_headers,
                        alternative_cookies,
                    )
                    if secret:
                        secrets.append(secret)
                query[:] = alternative_query
                headers.update(alternative_headers)
                cookies.update(alternative_cookies)
                return secrets
            except (APIError, ValueError) as exc:
                errors.append(str(exc))
        raise APIError(
            "No configured authentication satisfies this operation: " + "; ".join(errors)
        )

    @staticmethod
    def _apply_auth(
        scheme: dict[str, Any],
        config: AuthConfig | None,
        query: list[tuple[str, Any]],
        headers: dict[str, str],
        cookies: dict[str, str],
    ) -> str | None:
        kind = scheme.get("type")
        if kind == "apiKey":
            if not config or config.type not in {"api_key", "apikey"}:
                raise APIError("API key credentials are not configured")
            token = secret_from_env(config.token_env)
            location, name = _api_key_location(scheme)
            if location == "header":
                headers[name] = token or ""
            elif location == "query":
                query.append((name, token or ""))
            else:
                cookies[name] = token or ""
            return token
        if kind == "http" and scheme.get("scheme", "").lower() == "bearer" or kind == "oauth2":
            if not config or config.type not in {"bearer", "oauth2"}:
                raise APIError("Bearer token credentials are not configured")
            headers["Authorization"] = "Bearer " + (secret_from_env(config.token_env) or "")
            return secret_from_env(config.token_env) or ""
        if kind == "basic" or (kind == "http" and scheme.get("scheme", "").lower() == "basic"):
            if not config or config.type != "basic":
                raise APIError("HTTP Basic credentials are not configured")
            username = secret_from_env(config.username_env)
            password = secret_from_env(config.password_env)
            encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
            headers["Authorization"] = "Basic " + encoded
            return f"{username}:{password}"
        raise APIError(f"OpenAPI security scheme type {kind!r} is not supported")

    @staticmethod
    def _serialize(value: Any, style: str, explode: bool) -> list[tuple[str, Any]]:
        if isinstance(value, list):
            if style == "pipeDelimited":
                return [("", "|".join(map(str, value)))]
            if style == "spaceDelimited":
                return [("", " ".join(map(str, value)))]
            if explode:
                return [("", item) for item in value]
            return [("", ",".join(map(str, value)))]
        if isinstance(value, dict):
            pairs = list(value.items())
            if explode:
                return [(str(k), v) for k, v in pairs]
            return [("", ",".join(f"{k},{v}" for k, v in pairs))]
        return [("", value)]

    @staticmethod
    def _path_value(name: str, value: Any, style: str, explode: bool) -> str:
        if style not in {"simple", "label", "matrix"}:
            raise APIError(f"Unsupported path parameter style {style!r}")
        if isinstance(value, list):
            joined = (
                ".".join(map(str, value))
                if style == "label" and explode
                else ",".join(map(str, value))
            )
            if style == "matrix":
                return ";" + (
                    ";".join(f"{name}={v}" for v in value) if explode else f"{name}={joined}"
                )
            return ("." if style == "label" else "") + joined
        if isinstance(value, dict):
            pairs = list(value.items())
            if style == "matrix" and explode:
                serialized = ";".join(f"{k}={v}" for k, v in pairs)
            elif explode:
                separator = "." if style == "label" else ","
                serialized = separator.join(
                    (f"{k}={v}" if style in {"simple", "matrix"} else str(v)) for k, v in pairs
                )
            else:
                serialized = ",".join(f"{k},{v}" for k, v in pairs)
            if style == "matrix" and not explode:
                return f";{name}={serialized}"
            if style == "label" and not serialized.startswith("."):
                return "." + serialized
            return serialized
        serialized = str(value)
        if style == "label":
            return "." + serialized
        if style == "matrix":
            return f";{name}={serialized}"
        return serialized

    async def request(self, operation: Operation, arguments: dict[str, Any]) -> Any:
        allow_hosts = {h.lower() for h in self.config.allow_hosts}
        base_url = self.base_url
        if operation.servers and not self.config.base_url:
            from .openapi import _server_url

            server_url = _server_url(operation.servers[0], self.config.server_variables)
            base_url = urljoin(self.base_url.rstrip("/") + "/", server_url)
        _validate_target(base_url, allow_hosts)
        path = operation.path
        query: list[tuple[str, Any]] = []
        headers: dict[str, str] = {}
        cookies: dict[str, str] = {}
        for parameter in operation.parameters:
            name = parameter.get("name")
            location = parameter.get("in")
            if not name or name not in arguments:
                continue
            value = arguments[name]
            style = parameter.get("style") or {
                "path": "simple",
                "header": "simple",
                "query": "form",
                "cookie": "form",
            }.get(location, "form")
            explode = parameter.get("explode", style == "form")
            collection = parameter.get("collectionFormat")
            if (
                location in {"path", "header"}
                and isinstance(value, list)
                and collection
                in {
                    "csv",
                    "ssv",
                    "pipes",
                }
            ):
                separator = {"csv": ",", "ssv": " ", "pipes": "|"}[collection]
                value = separator.join(map(str, value))
            if location == "path":
                if "{" + name + "}" not in path:
                    raise APIError(f"Path parameter {name!r} has no matching placeholder")
                path = path.replace(
                    "{" + name + "}",
                    quote(self._path_value(name, value, style, explode), safe=";=,."),
                )
            elif location in {"query", "header", "cookie"}:
                if location == "query" and parameter.get("allowReserved"):
                    raise APIError(
                        f"allowReserved query serialization is not supported for parameter {name!r}"
                    )
                if style not in (
                    {"form", "spaceDelimited", "pipeDelimited", "deepObject"}
                    if location == "query"
                    else {"simple"}
                    if location == "header"
                    else {"form"}
                ):
                    raise APIError(f"Unsupported {location} parameter style {style!r} for {name!r}")
                if style == "deepObject":
                    if not isinstance(value, dict):
                        raise APIError(f"deepObject parameter {name!r} must be an object")
                    query.extend((f"{name}[{key}]", item) for key, item in value.items())
                    continue
                items = self._serialize(value, style, explode)
                if location == "query":
                    query.extend((k or name, v) for k, v in items)
                elif location == "header":
                    if isinstance(value, dict) and explode:
                        headers[name] = ",".join(f"{k}={v}" for k, v in value.items())
                    else:
                        headers[name] = ",".join(str(v) for _, v in items)
                else:
                    if isinstance(value, dict):
                        if explode:
                            cookies.update({str(k): str(v) for k, v in value.items()})
                        else:
                            cookies[name] = ",".join(f"{k},{v}" for k, v in value.items())
                    elif isinstance(value, list) and explode:
                        raise APIError(
                            f"Exploded cookie array serialization is not supported for {name!r}"
                        )
                    else:
                        cookies[name] = ",".join(str(v) for _, v in items)
        secrets = self._auth(operation, query, headers, cookies)
        if cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        url = base_url.rstrip("/") + "/" + path.lstrip("/")
        kwargs: dict[str, Any] = {"params": query, "headers": headers}
        body = arguments.get("body")
        mode = (operation.request_body or {}).get("mode")
        if body is not None:
            if mode == "application/json":
                kwargs["json"] = body
            elif mode == "text/plain":
                if not isinstance(body, str):
                    raise APIError("text/plain request body must be a string")
                kwargs["content"] = body
                headers["Content-Type"] = mode
            elif mode == "application/x-www-form-urlencoded":
                if not isinstance(body, dict):
                    raise APIError("Form request body must be an object")
                kwargs["data"] = body
            elif mode == "multipart/form-data":
                if not isinstance(body, dict) or any(not isinstance(v, str) for v in body.values()):
                    raise APIError(
                        "Multipart form supports text fields only; binary uploads are not enabled"
                    )
                body_schema = operation.input_schema.get("properties", {}).get("body", {})
                binary_fields = {
                    name
                    for name, field_schema in body_schema.get("properties", {}).items()
                    if field_schema.get("format") in {"binary", "byte"}
                }
                if binary_fields.intersection(body):
                    raise APIError("Binary multipart fields are not supported")
                kwargs["data"] = body
            else:
                raise APIError(f"Unsupported request body media type: {mode}")
        client = self._http
        if client is None:
            client = httpx.AsyncClient(timeout=self.config.timeout, follow_redirects=False)
            self._http = client
        try:
            LOGGER.info("Calling %s %s %s", self.api_id, operation.method, operation.path)
            async with client.stream(operation.method, url, **kwargs) as response:
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > self.config.max_response_bytes:
                        raise APIError(
                            f"Response exceeds configured size limit for {operation.method} {operation.path}"
                        )
                text = bytes(payload).decode(response.encoding or "utf-8", errors="replace")
                if response.is_error:
                    safe_text = text[:1000]
                    for value in [*headers.values(), *secrets]:
                        if value:
                            safe_text = safe_text.replace(value, "[redacted]")
                    raise APIError(
                        f"{self.api_id} {operation.method} {operation.path}: HTTP {response.status_code}: {safe_text}"
                    )
                if not payload:
                    return {"status": "success", "http_status": response.status_code}
                if "json" in response.headers.get("content-type", "").lower():
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError:
                        return {"http_status": response.status_code, "body": text[:1000]}
                return {
                    "http_status": response.status_code,
                    "content_type": response.headers.get("content-type", ""),
                    "body": text[:1000],
                }
        except httpx.TimeoutException as exc:
            raise APIError(
                f"{self.api_id} {operation.method} {operation.path}: request timed out"
            ) from exc
        except httpx.HTTPError as exc:
            raise APIError(
                f"{self.api_id} {operation.method} {operation.path}: HTTP transport error ({type(exc).__name__})"
            ) from exc
