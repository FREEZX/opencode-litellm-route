"""Budget-aware classifier and cross-provider balancer for the LiteLLM smart-router(s).

OpenCode keeps the real allowance; LiteLLM's own dollar estimates do not mirror
it, so this plugin polls the key-authenticated usage endpoint and caps the tier
before a paid Go model is chosen. One instance of this classifier is shared by
every smart-router* model group (they differ only in which models populate each
tier, e.g. a 200k-context-safe pool vs a 500k-context-safe pool).

Policy (lean: spend as little plan allowance as possible):
  * SIMPLE is the zero-cost pool: local swift-1.5 + the unlimited free Go models.
  * Obvious simple asks answer from that pool with no classifier call at all.
  * Everything else is classified by claude-haiku-5-5 (Claude Pro subscription,
    via CLIProxyAPI). If it fails or times out, the local heuristic tier is used.
  * When the classifier and the heuristic disagree, take the LOWER tier.
  * Windows cap the tier. Rolling and weekly bind first on agent traffic (rolling
    cap = 20% of the monthly bucket, weekly = 50%), so their thresholds are the
    tightest. A limited window or a window past its cap forces the zero-cost pool.

Separately, ``ProviderBalanceFilter`` (registered as its own litellm callback)
polls both subscriptions' real quota windows -- OpenCode Go's
``/zen/go/v1/usage`` (rolling/weekly/monthly) and Claude Pro's undocumented
``/api/oauth/usage`` (five_hour/seven_day) -- and excludes a provider's
deployments from the router's pick once that provider's hourly, weekly, or
monthly window is exhausted. This is independent of the tier-capping above: a
Go window being near its cap force the *tier* down to SIMPLE, but it was still
possible for a tier to be built entirely out of (now-exhausted) Claude or Go
deployments. The filter makes sure a request always lands on the other, still
healthy provider, and on local swift-1.5 / the free Go models when both are
exhausted at once.
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import time
import urllib.request
from typing import Any

import litellm
from litellm.integrations.custom_logger import CustomLogger

log = logging.getLogger("LiteLLM Router")

USAGE_URL = os.environ.get("OPENCODE_GO_USAGE_URL", "https://opencode.ai/zen/go/v1/usage")
USAGE_TTL_SECONDS = 60
CLASSIFIER_TIMEOUT_SECONDS = 8
# When the classifier endpoint is unreachable, do not pay its timeout on every
# turn: skip it for a while and classify with the free heuristic instead.
CLASSIFIER_COOLDOWN_SECONDS = 120

# Claude Pro's usage endpoint (undocumented, used internally by Claude Code's
# own /usage command). The OAuth credential CLIProxyAPI maintains (and
# auto-refreshes) is mounted read-only into this container; the filename
# varies by account, so the first claude-*.json found is used.
CLAUDE_USAGE_URL = os.environ.get("CLAUDE_USAGE_URL", "https://api.anthropic.com/api/oauth/usage")
CLAUDE_OAUTH_AUTH_DIR = os.environ.get("CLAUDE_OAUTH_AUTH_DIR", "/app/cliproxy-auth")
# Hard cutoffs a provider's deployments are excluded past. Mirrors the Go
# window caps below (rolling/5h = 90%, weekly = 85%); Claude Pro has no
# monthly window, so there is no third cutoff for it.
CLAUDE_HARD_CAPS = {"five_hour": 90.0, "seven_day": 85.0}

# Tier severity order (must match the tier names configured in config.yaml).
_TIERS = ("SIMPLE", "MEDIUM", "COMPLEX", "REASONING")

# (percent used -> highest tier still allowed), checked top-down.
# Rolling/weekly are tighter than monthly because their windows are smaller
# (20% / 50% of the monthly bucket) and they throttle first.
_WINDOW_CAPS = {
    "rolling": ((90, "SIMPLE"), (70, "MEDIUM"), (50, "COMPLEX")),
    "weekly": ((85, "SIMPLE"), (65, "MEDIUM"), (45, "COMPLEX")),
    "monthly": ((95, "SIMPLE"), (80, "MEDIUM"), (65, "COMPLEX")),
}

_SYSTEM = (
    "Classify the user task into exactly one of: SIMPLE, MEDIUM, COMPLEX, REASONING.\n"
    "SIMPLE = a small local model can do it (greeting, lookup, typo, trivial edit, short answer).\n"
    "MEDIUM = normal coding, explanation, or a single-file change.\n"
    "COMPLEX = multi-file work, subtle bugs, design tradeoffs.\n"
    "REASONING = proofs, architecture, deep debugging, or a long multi-step plan.\n"
    "Prefer the lowest tier that can still do the job. Reply with the tier word only."
)


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") in (None, "text"):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def _human_asks(context: Any) -> list[str]:
    messages = getattr(context, "structured_messages", None) or getattr(context, "raw_messages", None) or []
    asks: list[str] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = _text_from_content(message.get("content")).strip()
        if text:
            asks.append(text)
    return asks


def _task_text(context: Any) -> str:
    """Opening ask + latest ask, mirroring the built-in classifier.

    A short follow-up ("now add unit tests") is meaningless without the task it
    follows. Tool-result turns flatten to empty text and are skipped, so an
    agent loop's twenty turns do not masquerade as twenty human asks.
    """
    asks = _human_asks(context)
    if not asks:
        return ""
    latest = asks[-1]
    opening = asks[0]
    if len(asks) > 1 and opening != latest:
        return f"Opening task:\n{opening[:2000]}\n\nLatest follow-up:\n{latest[:2000]}"
    return latest[:4000]


def _lower(a: str, b: str) -> str:
    return a if _TIERS.index(a) <= _TIERS.index(b) else b


def heuristic_tier(text: str) -> str:
    """Free local score. Used to short-circuit obvious SIMPLE and as swift's fallback."""
    lowered = text.lower()
    if any(
        marker in lowered
        for marker in (
            "step by step",
            "think through",
            "root cause",
            "prove ",
            "architecture",
            "tradeoff",
            "trade-off",
        )
    ):
        return "REASONING"
    if (
        len(text) > 4000
        or text.count("```") >= 2
        or any(marker in lowered for marker in ("refactor", "migrate", "across the codebase", "multi-file"))
    ):
        return "COMPLEX"
    if len(text) > 500 or any(
        marker in lowered for marker in ("function", "class ", "bug", "error", "traceback", "implement", "fix ")
    ):
        return "MEDIUM"
    return "SIMPLE"


def _cap_tier(tier: str, usage: dict[str, Any] | None) -> str:
    if tier not in _TIERS or not usage:
        return tier
    cap = "REASONING"
    for name, ladder in _WINDOW_CAPS.items():
        window = usage.get(name)
        if not isinstance(window, dict):
            continue
        if window.get("status") not in (None, "ok"):
            return "SIMPLE"  # limited/throttled window -> route to the zero-cost pool
        percent = window.get("percent")
        if not isinstance(percent, (int, float)):
            continue
        for threshold, limited_to in ladder:
            if percent >= threshold:
                cap = _lower(cap, limited_to)
                break
    return _lower(tier, cap)


class _UsageCache:
    """TTL cache around one usage source, shared by whatever needs that source.

    On fetch failure the previous value is kept (stale-but-last-known) rather
    than clearing it, so a transient outage does not make every caller act as
    if the provider had no usage data at all.
    """

    def __init__(self, label: str, fetch: Any) -> None:
        self._label = label
        self._fetch = fetch
        self._value: dict[str, Any] | None = None
        self._at = 0.0

    async def get(self) -> dict[str, Any] | None:
        now = time.monotonic()
        if self._value is not None and now - self._at < USAGE_TTL_SECONDS:
            return self._value
        try:
            value = await self._fetch()
        except Exception as exc:  # noqa: BLE001 — a usage outage must not fail the request
            log.warning("%s usage fetch failed (%s); using last known state", self._label, exc)
            return self._value
        if value is not None:
            self._value = value
            self._at = now
        return self._value


async def _fetch_go_usage() -> dict[str, Any] | None:
    api_key = os.environ.get("OPENCODE_API_KEY")
    if not api_key:
        return None
    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            # Cloudflare on opencode.ai 403s the default Python-urllib UA.
            "User-Agent": "Mozilla/5.0 (compatible; litellm-go-router/1.0)",
        },
    )

    def _fetch() -> dict[str, Any]:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode())
        usage = payload.get("usage") if isinstance(payload, dict) else None
        if not isinstance(usage, dict):
            raise ValueError("usage payload missing")
        return usage

    return await asyncio.to_thread(_fetch)


def _claude_access_token() -> str | None:
    """The Claude Pro OAuth access token CLIProxyAPI maintains, read directly
    off the credential file mounted (read-only) from the cliproxyapi service's
    auth dir. The filename varies by account, so the first claude-*.json wins."""
    paths = sorted(glob.glob(os.path.join(CLAUDE_OAUTH_AUTH_DIR, "claude-*.json")))
    if not paths:
        return None
    try:
        with open(paths[0], encoding="utf-8") as handle:
            data = json.load(handle)
        token = data.get("access_token")
        return token if isinstance(token, str) and token else None
    except Exception as exc:  # noqa: BLE001 — a bad credential file must not fail the request
        log.warning("Claude OAuth credential unreadable (%s); Claude usage not checked", exc)
        return None


async def _fetch_claude_usage() -> dict[str, Any] | None:
    token = _claude_access_token()
    if not token:
        return None
    request = urllib.request.Request(
        CLAUDE_USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
        },
    )

    def _fetch() -> dict[str, Any]:
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode())

    return await asyncio.to_thread(_fetch)


_go_usage_cache = _UsageCache("OpenCode Go", _fetch_go_usage)
_claude_usage_cache = _UsageCache("Claude Pro", _fetch_claude_usage)


def _go_window_exhausted(usage: dict[str, Any] | None) -> str | None:
    """Name of the first Go usage window (rolling/weekly/monthly) that is
    actually exceeded (status not ok, or at/over 100%), or None."""
    if not isinstance(usage, dict):
        return None
    for name in ("rolling", "weekly", "monthly"):
        window = usage.get(name)
        if not isinstance(window, dict):
            continue
        if window.get("status") not in (None, "ok"):
            return name
        percent = window.get("percent")
        if isinstance(percent, (int, float)) and percent >= 100:
            return name
    return None


def _claude_window_exhausted(usage: dict[str, Any] | None) -> str | None:
    """Name of the first Claude Pro window (five_hour/seven_day) at or past its
    hard cap, or None. Cut off a little under 100% (CLAUDE_HARD_CAPS), the same
    margin _WINDOW_CAPS gives the Go windows, so a request never lands on a
    deployment Anthropic is about to 429."""
    if not isinstance(usage, dict):
        return None
    for name, cap in CLAUDE_HARD_CAPS.items():
        window = usage.get(name)
        if isinstance(window, dict):
            utilization = window.get("utilization")
            if isinstance(utilization, (int, float)) and utilization >= cap:
                return name
    for limit in usage.get("limits") or ():
        if not isinstance(limit, dict) or not limit.get("is_active"):
            continue
        cap = CLAUDE_HARD_CAPS.get("five_hour" if limit.get("kind") == "session" else "seven_day", 100.0)
        percent = limit.get("percent")
        if isinstance(percent, (int, float)) and percent >= cap:
            return str(limit.get("kind") or "limit")
    return None


# Deployment prefixes identifying the two balanced providers. Everything else
# (swift-1.5 on the LAN box) is outside this balance and always available.
_GO_MODEL_PREFIX = "opencode_go/"
_CLAUDE_MODEL_PREFIX = "openai/claude-"


def _deployment_provider(deployment: dict[str, Any]) -> str | None:
    params = deployment.get("litellm_params") or {}
    model = str(params.get("model") or "")
    if model.startswith(_GO_MODEL_PREFIX):
        return "go"
    if model.startswith(_CLAUDE_MODEL_PREFIX):
        return "claude"
    return None


class ProviderBalanceFilter(CustomLogger):
    """Excludes a provider's deployments once its usage window is exhausted.

    GoAllowanceClassifier._cap_tier (above) already steers the *tier* toward
    the zero-cost pool as Go usage climbs, but that only prevents a NEW paid
    pick; it does not stop a tier that still resolves entirely to one
    exhausted provider, nor a direct call to a model bypassing the router
    entirely. This filter is the hard backstop for both: once a provider's
    window is actually exceeded (not just close), its deployments are dropped
    from the candidate list for every request, and the configured
    `router_settings.fallbacks` chain (already relied on for dead
    deployments) carries the request the rest of the way, ending on local
    swift-1.5 or a free Go model if both providers are exhausted at once.

    The unlimited free Go models (model_name ending in "-free") are exempt:
    the Go account's rolling/weekly/monthly caps do not apply to them.
    """

    async def async_filter_deployments(
        self,
        model: str,
        healthy_deployments: list,
        messages: Any,
        request_kwargs: dict | None = None,
        parent_otel_span: Any = None,
    ) -> list[dict]:
        if not healthy_deployments or model.endswith("-free"):
            return healthy_deployments

        go_usage = None
        claude_usage = None
        kept: list[dict] = []
        dropped_windows: dict[str, str] = {}
        for deployment in healthy_deployments:
            provider = _deployment_provider(deployment)
            if provider == "go":
                if go_usage is None:
                    go_usage = await _go_usage_cache.get()
                window = _go_window_exhausted(go_usage)
                if window:
                    dropped_windows["go"] = window
                    continue
            elif provider == "claude":
                if claude_usage is None:
                    claude_usage = await _claude_usage_cache.get()
                window = _claude_window_exhausted(claude_usage)
                if window:
                    dropped_windows["claude"] = window
                    continue
            kept.append(deployment)

        if not dropped_windows:
            return healthy_deployments
        if not kept:
            log.warning(
                "provider-balance: every deployment for %s excluded (%s); handing off to the fallback chain",
                model,
                dropped_windows,
            )
            return []
        log.warning(
            "provider-balance: excluded %d/%d deployment(s) for %s (%s)",
            len(healthy_deployments) - len(kept),
            len(healthy_deployments),
            model,
            dropped_windows,
        )
        return kept


provider_balance_filter = ProviderBalanceFilter()


class GoAllowanceClassifier:
    def __init__(self) -> None:
        self._classifier_down_until = 0.0

    async def _usage_snapshot(self) -> dict[str, Any] | None:
        return await _go_usage_cache.get()

    async def _classifier_tier(self, task: str) -> str | None:
        api_base = os.environ.get("CLIPROXY_API_BASE")
        api_key = os.environ.get("CLIPROXY_API_KEY")
        if not api_base or not api_key:
            return None
        if time.monotonic() < self._classifier_down_until:
            self._log_classifier_skip()
            return None
        claude_window = _claude_window_exhausted(await _claude_usage_cache.get())
        if claude_window:
            log.warning(
                "claude-haiku-5-5 classifier skipped (Claude Pro %s window exhausted); using heuristic",
                claude_window,
            )
            return None
        try:
            response = await litellm.acompletion(
                model="openai/claude-haiku-5-5",
                api_base=api_base,
                api_key=api_key,
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": task},
                ],
                max_tokens=32,
                temperature=0,
                timeout=CLASSIFIER_TIMEOUT_SECONDS,
                # A dead/rate-limited classifier needs one attempt, not a retry
                # chain; the heuristic below is the fallback.
                num_retries=0,
            )
            content = response.choices[0].message.content or ""
        except Exception as exc:  # noqa: BLE001 — classifier failure falls back to the heuristic
            self._classifier_down_until = time.monotonic() + CLASSIFIER_COOLDOWN_SECONDS
            log.warning(
                "claude-haiku-5-5 classifier failed (%s); using heuristic and skipping it for %ss",
                exc,
                CLASSIFIER_COOLDOWN_SECONDS,
            )
            return None
        self._classifier_down_until = 0.0
        upper = content.upper()
        for tier in ("REASONING", "COMPLEX", "MEDIUM", "SIMPLE"):
            if tier in upper:
                return tier
        return None

    def _log_classifier_skip(self) -> None:
        remaining = max(0, int(self._classifier_down_until - time.monotonic()))
        log.warning("claude-haiku-5-5 classifier skipped (down, %ss left); using heuristic", remaining)

    async def classify(self, context: Any) -> str | None:
        task = _task_text(context)
        if not task:
            return "SIMPLE"

        local = heuristic_tier(task)
        tier = local
        remote: str | None = None
        if local != "SIMPLE":  # obvious simple never waits for the remote classifier
            remote = await self._classifier_tier(task)
            tier = _lower(local, remote) if remote else local

        usage = await self._usage_snapshot()
        capped = _cap_tier(tier, usage)
        windows = {
            name: ((usage or {}).get(name) or {}).get("percent", "?")
            for name in ("rolling", "weekly", "monthly")
        }
        log.warning(  # warning level: litellm's stdout handler drops INFO
            "smart-router classify heuristic=%s classifier=%s tier=%s capped=%s windows=%s",
            local,
            remote or "-",
            tier,
            capped,
            windows,
        )
        return capped


go_allowance_classifier = GoAllowanceClassifier()
