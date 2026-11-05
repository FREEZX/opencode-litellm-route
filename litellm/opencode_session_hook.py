"""OpenCode Go request plumbing for the LiteLLM proxy.

Two jobs:

1. Ensure every Go request carries a stable ``x-opencode-session`` (Go rejects
   requests without it) and surface the caller's session id to the routers.

2. Show which model ``smart-router`` picked. The proxy exposes it only as the
   ``x-litellm-model-name`` response header, which OpenCode does not render, so
   the first assistant text of each new human turn ends with a one-line marker:

       > ⚡ routed via longcat-2.0

   Agent-loop continuations (tool results) do not get a second marker, and the
   marker is stripped from assistant history on the way back in, so the model
   never sees it and token counts stay clean.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

log = logging.getLogger("LiteLLM Router")

ROUTE_MARKER_PREFIX = "> ⚡ routed via "
# Older messages may carry the marker at the start; new ones append it at the end.
_LEADING_MARKER_RE = re.compile(r"^> ⚡ routed via [^\n]*\n\n")
_TRAILING_MARKER_RE = re.compile(r"\n\n> ⚡ routed via [^\n]*\n?$")
SHOW_ROUTE_MARKER = os.environ.get("LITELLM_SHOW_ROUTE_MARKER", "1").lower() not in (
    "0",
    "false",
    "no",
    "off",
)

# Route handoff to the OpenCode TUI plugin.
#
# The proxy knows which deployment the complexity router picked (``response.model``)
# but OpenCode does not surface response headers, and ``/spend/logs`` ignores a
# session filter. So the hook drops one small JSON file per OpenCode session in a
# directory shared with the host; the TUI plugin
# (``~/.config/opencode/plugins/litellm-route/``) reads it and shows a toast.
ROUTE_DIR = os.environ.get("LITELLM_ROUTE_DIR", "/app/routes")
WRITE_ROUTE_FILE = os.environ.get("LITELLM_ROUTE_FILE", "1").lower() not in (
    "0",
    "false",
    "no",
    "off",
)

# Calls that ask a model to *produce* a title/summary must not be decorated:
# OpenCode would use the marker as the session title.
_NOT_A_CHAT_TURN = (
    "here is the first message of the conversation",
    "generate a concise title",
    "generate a short title",
)


def _header_lookup(headers: dict[str, Any] | None, name: str) -> str | None:
    if not isinstance(headers, dict):
        return None
    want = name.lower()
    for key, value in headers.items():
        if str(key).lower() == want and value:
            return str(value)
    return None


def _existing_session(data: dict[str, Any]) -> str | None:
    for key in ("litellm_session_id", "session_id"):
        value = data.get(key)
        if value:
            return str(value)

    for meta_key in ("metadata", "litellm_metadata"):
        meta = data.get(meta_key)
        if isinstance(meta, dict):
            for key in ("session_id", "litellm_caller_session_id"):
                value = meta.get(key)
                if value:
                    return str(value)

    for headers in (data.get("headers"), data.get("extra_headers")):
        for name in (
            # OpenCode sends the real session id here; x-opencode-session is only
            # the affinity key, which can differ for sub-sessions.
            "x-opencode-session-id",
            "x-litellm-session-id",
            "x-opencode-session",
            "X-Litellm-Session-Id",
            "X-Opencode-Session",
        ):
            value = _header_lookup(headers if isinstance(headers, dict) else None, name)
            if value:
                return value
    return None


def _is_smart_router(data: dict[str, Any] | None) -> bool:
    return isinstance(data, dict) and (
        data.get("model") == "smart-router" or data.get("model_group") == "smart-router"
    )


def _display_model(name: Any) -> str:
    text = str(name or "?")
    return text.rsplit("/", 1)[-1]


def _marker_for(name: Any) -> str:
    return f"{ROUTE_MARKER_PREFIX}{_display_model(name)}"


def _safe_session_name(session_id: Any) -> str | None:
    name = str(session_id or "").strip()
    if not name or len(name) > 128:
        return None
    # Session ids become file names; keep them boring.
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        return None
    return name


def _write_route_file(session_id: Any, model: Any, request_id: Any = None) -> None:
    """Record the picked deployment for the OpenCode TUI plugin to read."""
    if not WRITE_ROUTE_FILE:
        return
    name = _safe_session_name(session_id)
    if not name:
        return
    payload = {
        "model": _display_model(model),
        "raw_model": str(model or ""),
        "request_id": str(request_id) if request_id else None,
        "updated_at": _utcnow(),
    }
    target = os.path.join(ROUTE_DIR, f"{name}.json")
    tmp = f"{target}.{os.getpid()}.tmp"
    try:
        os.makedirs(ROUTE_DIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp, target)
    except Exception as exc:  # noqa: BLE001 — the route file is a nicety, never fail a request
        log.debug("route file not written: %s", exc)
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in (None, "text"):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def _newest_is_human_ask(data: dict[str, Any] | None) -> bool:
    """True when the request ends with a new human ask, not a tool continuation."""
    if not isinstance(data, dict):
        return False
    messages = data.get("messages")
    if not isinstance(messages, list) or not messages:
        return True
    for message in reversed(messages):
        if not isinstance(message, dict):
            return False
        role = message.get("role")
        if role == "tool":
            return False
        if role != "user":
            return False
        content = message.get("content")
        if isinstance(content, list) and any(
            isinstance(part, dict) and part.get("type") in ("tool_result", "image_url")
            for part in content
        ):
            return False
        text = _text_of(content).strip().lower()
        if not text:
            return False
        return not any(marker in text for marker in _NOT_A_CHAT_TURN)
    return True


def _strip_marker_text(text: str) -> str:
    text = _LEADING_MARKER_RE.sub("", text, count=1)
    return _TRAILING_MARKER_RE.sub("", text, count=1)


def _strip_route_markers(messages: Any) -> None:
    if not isinstance(messages, list):
        return
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = _strip_marker_text(content)
        elif isinstance(content, list):
            for part in content:
                if (
                    isinstance(part, dict)
                    and part.get("type") in (None, "text")
                    and isinstance(part.get("text"), str)
                ):
                    part["text"] = _strip_marker_text(part["text"])
                    break


class OpenCodeSessionHook(CustomLogger):
    def __init__(self) -> None:
        super().__init__()
        # session_id -> whether this turn's final step was already decorated.
        self._turn_marked: dict[str, bool] = {}

    def _session_key(self, data: Any) -> str:
        if not isinstance(data, dict):
            return "global"
        sid = data.get("litellm_session_id")
        if sid:
            return str(sid)
        return _existing_session(data) or "global"

    def _is_title_call(self, data: Any) -> bool:
        if not isinstance(data, dict):
            return False
        for message in reversed(data.get("messages") or []):
            if not isinstance(message, dict):
                continue
            if message.get("role") != "user":
                continue
            text = _text_of(message.get("content")).strip().lower()
            if text:
                return any(marker in text for marker in _NOT_A_CHAT_TURN)
        return False

    def _decorate_allowed(self, data: Any, final: bool) -> tuple[bool, str]:
        """(allowed, session_key) — final step of a turn, not yet marked, not a title call."""
        if not SHOW_ROUTE_MARKER or not _is_smart_router(data) or not final:
            return False, ""
        if self._is_title_call(data):
            return False, ""
        key = self._session_key(data)
        if self._turn_marked.get(key):
            return False, key
        return True, key

    def _mark_done(self, key: str) -> None:
        if len(self._turn_marked) > 512:
            self._turn_marked.clear()
        self._turn_marked[key] = True

    def _route_allowed(self, data: Any) -> bool:
        """A route file is written for the first step of each smart-router turn."""
        if not WRITE_ROUTE_FILE or not _is_smart_router(data):
            return False
        return _newest_is_human_ask(data)

    def _record_route(self, data: Any, model: Any, request_id: Any = None) -> None:
        if not self._route_allowed(data):
            return
        session_id = self._session_key(data)
        if not session_id or session_id == "global":
            return
        _write_route_file(session_id, model, request_id)

    async def async_pre_call_hook(
        self,
        user_api_key_dict,  # noqa: ARG002
        cache,  # noqa: ARG002
        data: dict,
        call_type,  # noqa: ARG002
    ):
        session_id = _existing_session(data) or str(uuid.uuid4())

        data["litellm_session_id"] = session_id

        for meta_key in ("metadata", "litellm_metadata"):
            meta = data.get(meta_key)
            if not isinstance(meta, dict):
                meta = {}
                data[meta_key] = meta
            meta.setdefault("session_id", session_id)
            # Ensure affinity treats this as a caller session, not a generated one.
            meta.pop("litellm_session_id_generated", None)
            meta.setdefault("litellm_caller_session_id", session_id)

        extra = data.get("extra_headers")
        if not isinstance(extra, dict):
            extra = {}
            data["extra_headers"] = extra
        if not _header_lookup(extra, "x-opencode-session"):
            extra["x-opencode-session"] = session_id

        # Keep our own route marker out of the model's context.
        _strip_route_markers(data.get("messages"))

        # A new human ask starts a fresh turn: allow one marker again.
        if _newest_is_human_ask(data):
            self._turn_marked.pop(session_id, None)

        return data

    async def async_post_call_success_hook(self, data, user_api_key_dict, response):  # noqa: ARG002
        try:
            finish = response.choices[0].finish_reason
        except Exception:
            finish = None
        final = finish not in ("tool_calls", "function_call")
        self._record_route(data, getattr(response, "model", None))
        allowed, key = self._decorate_allowed(data, final)
        if not allowed:
            return response
        try:
            content = response.choices[0].message.content
            if isinstance(content, str) and content:
                response.choices[0].message.content = f"{content}\n\n{_marker_for(response.model)}"
                self._mark_done(key)
        except Exception as exc:  # noqa: BLE001 — decoration must never fail a request
            log.debug("route marker skipped: %s", exc)
        return response

    async def async_post_call_streaming_iterator_hook(self, user_api_key_dict, response, request_data):
        marked = False
        routed = False
        last_chunk = None
        async for chunk in response:
            last_chunk = chunk
            if not routed:
                routed = True
                self._record_route(request_data, getattr(chunk, "model", None))
            if not marked and _is_finish_chunk(chunk):
                finish = _finish_reason(chunk)
                allowed, key = self._decorate_allowed(
                    request_data, finish not in ("tool_calls", "function_call")
                )
                if allowed:
                    marker_chunk = _marker_chunk_from(chunk)
                    if marker_chunk is not None:
                        yield marker_chunk
                        marked = True
                        self._mark_done(key)
            yield chunk
        if not marked and last_chunk is not None:
            allowed, key = self._decorate_allowed(request_data, True)
            if allowed:
                # Providers that never send finish_reason: append a final text chunk.
                marker_chunk = _marker_chunk_from(last_chunk)
                if marker_chunk is not None:
                    yield marker_chunk
                    self._mark_done(key)


def _finish_reason(chunk: Any) -> Any:
    try:
        for choice in chunk.choices or []:
            reason = getattr(choice, "finish_reason", None)
            if reason:
                return reason
    except Exception:
        pass
    return None


def _is_finish_chunk(chunk: Any) -> bool:
    return _finish_reason(chunk) is not None


def _marker_chunk_from(chunk: Any) -> Any | None:
    """A copy of ``chunk`` carrying only the route marker as a text delta."""
    try:
        marker = chunk.model_copy(deep=True) if hasattr(chunk, "model_copy") else copy.deepcopy(chunk)
    except Exception:
        return None
    try:
        if getattr(marker, "usage", None) is not None:
            marker.usage = None
        if not marker.choices:
            return None
        choice = marker.choices[0]
        choice.finish_reason = None
        delta = getattr(choice, "delta", None)
        if delta is None:
            return None
        delta.content = f"\n\n{_marker_for(getattr(chunk, 'model', None))}"
        if hasattr(delta, "reasoning_content"):
            delta.reasoning_content = None
        if hasattr(delta, "tool_calls"):
            delta.tool_calls = None
        return marker
    except Exception:
        return None


proxy_handler_instance = OpenCodeSessionHook()
