"""A small Model Context Protocol client, enough to drive the Robinhood Trading MCP.

Only what this agent needs: ``initialize``, ``tools/list``, and ``tools/call`` over
streamable HTTP with a bearer token. Written against the standard library so the
package does not grow a transport dependency.

**Getting a token.** The server authenticates with OAuth 2.1 + PKCE, and the
authorisation step has to happen in a desktop browser where you approve access in
the Robinhood app. That interactive flow is deliberately out of scope here: run it
once through an MCP host (Claude, Cursor, Codex) or an OAuth helper, then hand the
resulting access token to this client via ``ROBINHOOD_MCP_TOKEN``. Tokens expire,
and a 401 surfaces as :class:`McpAuthError` so the caller can prompt for a fresh one
rather than silently trading on a dead session.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger(__name__)

ROBINHOOD_MCP_URL = "https://agent.robinhood.com/mcp/trading"


class McpError(RuntimeError):
    """The server returned a JSON-RPC error or an unusable response."""


class McpAuthError(McpError):
    """The token is missing, expired, or rejected."""


@runtime_checkable
class ToolCaller(Protocol):
    """Anything that can invoke an MCP tool by name.

    The adapters depend on this rather than on a concrete transport, so the same
    code runs against the live server, against a recorded fixture in tests, or
    against a host that is already holding the MCP connection.
    """

    def call(self, name: str, arguments: dict[str, Any]) -> Any: ...

    def list_tools(self) -> list[dict[str, Any]]: ...


@dataclass
class HttpToolCaller:
    """JSON-RPC over MCP streamable HTTP."""

    url: str = ROBINHOOD_MCP_URL
    token: str | None = field(default=None, repr=False)
    timeout: float = 30.0
    client_name: str = "optionsagent"
    client_version: str = "0.1.0"
    _session_id: str | None = field(default=None, init=False)
    _next_id: int = field(default=0, init=False)
    _initialized: bool = field(default=False, init=False)
    _protocol_version: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.token = self.token or os.environ.get("ROBINHOOD_MCP_TOKEN")
        if not self.token:
            raise McpAuthError(
                "No MCP token. Authorise the Robinhood Trading MCP once in a desktop "
                "browser, then set ROBINHOOD_MCP_TOKEN to the resulting access token."
            )

    # ---- transport -------------------------------------------------------

    def _post(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        body = json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
            # Streamable HTTP may answer with either a JSON body or an SSE stream.
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.token}",
        }
        if self._protocol_version:
            headers["MCP-Protocol-Version"] = self._protocol_version
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                session = resp.headers.get("Mcp-Session-Id")
                if session:
                    self._session_id = session
                content_type = resp.headers.get("Content-Type", "")
                if "text/event-stream" in content_type:
                    message = _read_sse(resp, payload.get("id"))
                else:
                    raw = resp.read().decode()
                    message = json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise McpAuthError(
                    f"Robinhood MCP rejected the token ({exc.code}). Re-authorise and "
                    "set a fresh ROBINHOOD_MCP_TOKEN."
                ) from exc
            if exc.code == 404:
                self._initialized = False
                self._session_id = None
                self._protocol_version = None
            raise McpError(f"MCP request failed ({exc.code})") from exc
        except urllib.error.URLError as exc:
            raise McpError(f"could not reach {self.url}: {exc.reason}") from exc

        if "id" not in payload:
            return None
        if not isinstance(message, dict) or message.get("id") != payload["id"]:
            raise McpError("MCP response missing or request ID does not match")
        if "error" in message:
            err = message["error"]
            raise McpError(f"{err.get('code')}: {err.get('message')}")
        return message.get("result")

    def _request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self._next_id += 1
        return self._post(
            {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params or {}}
        )

    def _notify(self, method: str) -> None:
        self._post({"jsonrpc": "2.0", "method": method, "params": {}})

    # ---- protocol --------------------------------------------------------

    def initialize(self) -> dict[str, Any]:
        result = self._request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": self.client_name, "version": self.client_version},
            },
        )
        if not isinstance(result, dict) or result.get("protocolVersion") != "2025-06-18":
            raise McpError("Unsupported MCP protocol version")
        self._protocol_version = result["protocolVersion"]
        self._notify("notifications/initialized")
        self._initialized = True
        return result or {}

    def _ensure_initialized(self) -> None:
        if not self._initialized:
            self.initialize()

    def list_tools(self) -> list[dict[str, Any]]:
        self._ensure_initialized()
        found, cursor, seen = [], None, set()
        while True:
            result = self._request("tools/list", {"cursor": cursor} if cursor else {}) or {}
            found.extend(result.get("tools", []))
            cursor = result.get("nextCursor")
            if not cursor:
                return found
            if cursor in seen or len(seen) >= 100:
                raise McpError("Invalid MCP tool pagination")
            seen.add(cursor)

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        if name not in {
            "get_equity_quotes",
            "get_option_chains",
            "get_option_instruments",
            "get_option_quotes",
            "get_option_positions",
            "get_option_orders",
            "get_option_order",
            "get_portfolio",
            "get_accounts",
            "get_account",
            "get_equity_historicals",
            "review_option_order",
        }:
            raise McpError("Live mutation tools are disabled in this simulation release")
        self._ensure_initialized()
        result = self._request("tools/call", {"name": name, "arguments": arguments}) or {}
        if result.get("isError"):
            raise McpError(f"tool {name} failed")
        return _unwrap_content(result)


def _read_sse(stream, request_id: int | None) -> dict[str, Any] | None:
    """Stop at our response; an SSE connection need not close after sending it."""
    if request_id is None:
        return None
    parts: list[str] = []
    size = 0
    for raw in stream:
        size += len(raw)
        if size > 8 * 1024 * 1024:
            raise McpError("MCP event stream exceeded response limit")
        line = raw.decode().rstrip("\r\n")
        if line.startswith("data:"):
            parts.append(line[5:].lstrip(" "))
        elif not line and parts:
            message = json.loads("\n".join(parts))
            parts.clear()
            if isinstance(message, dict) and message.get("id") == request_id:
                return message
    raise McpError("MCP stream ended without the requested response")


def _parse_sse(raw: str) -> dict[str, Any] | None:
    """Pull the last JSON payload out of an SSE response body."""
    payload = None
    for line in raw.splitlines():
        if line.startswith("data:"):
            chunk = line[5:].strip()
            if chunk and chunk != "[DONE]":
                payload = chunk
    return json.loads(payload) if payload else None


def _content_text(result: dict[str, Any]) -> str:
    return "".join(
        part.get("text", "") for part in result.get("content", []) if isinstance(part, dict)
    )


def _unwrap_content(result: dict[str, Any]) -> Any:
    """Return the most useful representation of a tool result.

    MCP tools may answer with ``structuredContent``, with JSON encoded inside a text
    block, or with plain prose. Prefer structured data, fall back to parsing the
    text, and return the raw string only when it is not JSON at all.
    """
    if "structuredContent" in result:
        return result["structuredContent"]
    text = _content_text(result)
    if not text:
        return result.get("content", [])
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


@dataclass
class FakeToolCaller:
    """Canned responses keyed by tool name, for tests and offline development.

    Every call is recorded so a test can assert not just the outcome but that the
    agent did not, for example, place an order twice.
    """

    responses: dict[str, Any] = field(default_factory=dict)
    tools: list[dict[str, Any]] = field(default_factory=list)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        if name not in self.responses:
            raise McpError(f"no canned response for tool {name}")
        value = self.responses[name]
        return value(arguments) if callable(value) else value

    def list_tools(self) -> list[dict[str, Any]]:
        return list(self.tools)

    def calls_to(self, name: str) -> list[dict[str, Any]]:
        return [args for called, args in self.calls if called == name]
