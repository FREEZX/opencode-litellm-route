"""Budget-aware classifier for the LiteLLM smart-router.

OpenCode keeps the real allowance; LiteLLM's own dollar estimates do not mirror
it, so this plugin polls the key-authenticated usage endpoint and caps the tier
before a paid Go model is chosen.

Policy (lean: spend as little plan allowance as possible):
  * SIMPLE is the zero-cost pool: local swift-1.5 + the unlimited free Go models.
  * Obvious simple asks answer from that pool with no classifier call at all.
  * Everything else is classified by local swift-1.5 (free). If it fails or
    times out, the local heuristic tier is used.
  * When swift and the heuristic disagree, take the LOWER tier.
  * Windows cap the tier. Rolling and weekly bind first on agent traffic (rolling
    cap = 20% of the monthly bucket, weekly = 50%), so their thresholds are the
    tightest. A limited window or a window past its cap forces the zero-cost pool.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import litellm

log = logging.getLogger("LiteLLM Router")

USAGE_URL = os.environ.get("OPENCODE_GO_USAGE_URL", "https://opencode.ai/zen/go/v1/usage")
USAGE_TTL_SECONDS = 60
CLASSIFIER_TIMEOUT_SECONDS = 8
# When the local swift endpoint is unreachable, do not pay its timeout on every
# turn: skip it for a while and classify with the free heuristic instead.
SWIFT_COOLDOWN_SECONDS = 120

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


class GoAllowanceClassifier:
    def __init__(self) -> None:
        self._usage: dict[str, Any] | None = None
        self._usage_at = 0.0
        self._swift_down_until = 0.0

    async def _usage_snapshot(self) -> dict[str, Any] | None:
        now = time.monotonic()
        if self._usage is not None and now - self._usage_at < USAGE_TTL_SECONDS:
            return self._usage
        api_key = os.environ.get("OPENCODE_API_KEY")
        if not api_key:
            return self._usage
        try:
            import asyncio
            import json
            import urllib.request

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

            self._usage = await asyncio.to_thread(_fetch)
            self._usage_at = now
        except Exception as exc:  # noqa: BLE001 — a usage outage must not fail the request
            log.warning("OpenCode Go usage fetch failed (%s); tier not capped", exc)
        return self._usage

    async def _swift_tier(self, task: str) -> str | None:
        api_base = os.environ.get("STRATA_API_BASE")
        if not api_base:
            return None
        if time.monotonic() < self._swift_down_until:
            self._log_swift_skip()
            return None
        try:
            response = await litellm.acompletion(
                model="openai/swift-1.5-iq3_xxs",
                api_base=api_base,
                api_key=os.environ.get("STRATA_API_KEY") or "sk-local",
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": task},
                ],
                max_tokens=32,
                temperature=0,
                # swift-1.5 is a reasoning model. With thinking on, its reasoning
                # consumes the whole classifier budget and content comes back
                # None (finish_reason=length), silently disabling this classifier.
                chat_template_kwargs={"enable_thinking": False},
                timeout=CLASSIFIER_TIMEOUT_SECONDS,
                # A dead swift box needs one attempt, not a retry chain; the
                # heuristic below is the fallback.
                num_retries=0,
            )
            content = response.choices[0].message.content or ""
        except Exception as exc:  # noqa: BLE001 — classifier failure falls back to the heuristic
            self._swift_down_until = time.monotonic() + SWIFT_COOLDOWN_SECONDS
            log.warning(
                "swift-1.5 classifier failed (%s); using heuristic and skipping swift for %ss",
                exc,
                SWIFT_COOLDOWN_SECONDS,
            )
            return None
        self._swift_down_until = 0.0
        upper = content.upper()
        for tier in ("REASONING", "COMPLEX", "MEDIUM", "SIMPLE"):
            if tier in upper:
                return tier
        return None

    def _log_swift_skip(self) -> None:
        remaining = max(0, int(self._swift_down_until - time.monotonic()))
        log.warning("swift-1.5 classifier skipped (down, %ss left); using heuristic", remaining)

    async def classify(self, context: Any) -> str | None:
        task = _task_text(context)
        if not task:
            return "SIMPLE"

        local = heuristic_tier(task)
        tier = local
        swift: str | None = None
        if local != "SIMPLE":  # obvious simple never waits for the slow local classifier
            swift = await self._swift_tier(task)
            tier = _lower(local, swift) if swift else local

        usage = await self._usage_snapshot()
        capped = _cap_tier(tier, usage)
        windows = {
            name: ((usage or {}).get(name) or {}).get("percent", "?")
            for name in ("rolling", "weekly", "monthly")
        }
        log.warning(  # warning level: litellm's stdout handler drops INFO
            "smart-router classify heuristic=%s swift=%s tier=%s capped=%s windows=%s",
            local,
            swift or "-",
            tier,
            capped,
            windows,
        )
        return capped


go_allowance_classifier = GoAllowanceClassifier()
