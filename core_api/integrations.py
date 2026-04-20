from __future__ import annotations

import logging
import os
import time
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import requests

logger = logging.getLogger(__name__)

DEFAULT_RUNTIME_CONFIG: dict[str, dict[str, Any]] = {
    "distributed_rate_limiter": {
        "enabled": False,
        "mode": "queue_busy",
        "route": "/api/submissions/submit/",
        "timeout_ms": 500,
        "fail_open": True,
    },
    "queue_throttle": {
        "allow_when_depth_at_or_below": 0,
        "fallback_to_drf": True,
    },
}
_runtime_cache: dict[str, Any] = {
    "expires_at": 0.0,
    "payload": deepcopy(DEFAULT_RUNTIME_CONFIG),
}


@dataclass(slots=True)
class ExternalRateLimitResult:
    allowed: bool
    applied: bool
    retry_after_seconds: int = 0
    degraded: bool = False
    local_fallback: bool = False
    policy_name: str | None = None


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _get_runtime_config_params() -> tuple[str, dict[str, str], dict[str, Any], float]:
    base_url = os.getenv("CONFIG_CONTROL_BASE_URL", "").strip().rstrip("/")
    headers = {
        "X-User-Id": os.getenv("CONFIG_CONTROL_USER_ID", "judge-vortex"),
        "X-Role": os.getenv("CONFIG_CONTROL_ROLE", "reader"),
    }
    params = {
        "version": "resolved",
        "environment": os.getenv("CONFIG_CONTROL_ENVIRONMENT", "prod"),
        "target": os.getenv("CONFIG_CONTROL_TARGET", "judge-vortex"),
        "client_id": os.getenv("CONFIG_CONTROL_CLIENT_ID", "judge-vortex-web"),
    }
    timeout_seconds = _env_float("CONFIG_CONTROL_TIMEOUT_SECONDS", 0.75)
    return base_url, headers, params, timeout_seconds


def get_runtime_config(*, force_refresh: bool = False) -> dict[str, Any]:
    ttl_seconds = max(_env_float("CONFIG_CONTROL_CACHE_TTL_SECONDS", 15.0), 1.0)
    if not force_refresh and _runtime_cache["expires_at"] > time.monotonic():
        return deepcopy(_runtime_cache["payload"])

    base_url, headers, params, timeout_seconds = _get_runtime_config_params()
    config_name = os.getenv("CONFIG_CONTROL_RUNTIME_CONFIG_NAME", "judge-vortex.runtime")
    if not base_url:
        return deepcopy(DEFAULT_RUNTIME_CONFIG)

    try:
        response = requests.get(
            f"{base_url}/configs/{config_name}",
            headers=headers,
            params=params,
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        runtime_value = payload.get("value")
        if not isinstance(runtime_value, dict):
            raise ValueError("ConfigControl returned a non-object runtime payload.")
        merged = _deep_merge(DEFAULT_RUNTIME_CONFIG, runtime_value)
        _runtime_cache["payload"] = merged
        _runtime_cache["expires_at"] = time.monotonic() + ttl_seconds
        return deepcopy(merged)
    except Exception as exc:
        logger.warning("config_control.runtime_fetch_failed %s", exc)
        if _runtime_cache["payload"]:
            return deepcopy(_runtime_cache["payload"])
        return deepcopy(DEFAULT_RUNTIME_CONFIG)


def extract_client_ip(request) -> str | None:
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def evaluate_submission_rate_limit(
    request,
    *,
    config: dict[str, Any],
) -> ExternalRateLimitResult | None:
    limiter_config = config.get("distributed_rate_limiter") or {}
    if not isinstance(limiter_config, dict):
        return None
    if not bool(limiter_config.get("enabled", False)):
        return None

    base_url = os.getenv("RATE_LIMITER_BASE_URL", "").strip().rstrip("/")
    service_token = os.getenv("RATE_LIMITER_SERVICE_TOKEN", "").strip()
    if not base_url or not service_token:
        return None

    timeout_ms = limiter_config.get("timeout_ms")
    timeout_seconds = max((timeout_ms or _env_int("RATE_LIMITER_TIMEOUT_MS", 500)) / 1000, 0.1)
    route = str(limiter_config.get("route") or request.path)
    payload = {
        "route": route,
        "user_id": str(request.user.id) if getattr(request.user, "is_authenticated", False) else None,
        "ip_address": extract_client_ip(request),
        "tenant_id": str(request.data.get("room_id")) if request.data.get("room_id") else None,
        "api_key": None,
    }

    try:
        response = requests.post(
            f"{base_url}/internal/evaluate",
            headers={"X-Service-Token": service_token},
            json=payload,
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        body = response.json()
    except Exception as exc:
        logger.warning("distributed_rate_limiter.request_failed %s", exc)
        if _env_bool("RATE_LIMITER_FAIL_OPEN", bool(limiter_config.get("fail_open", True))):
            return None
        return ExternalRateLimitResult(allowed=False, applied=True, degraded=True)

    policy = body.get("policy")
    policy_name = policy.get("name") if isinstance(policy, dict) else None
    return ExternalRateLimitResult(
        allowed=bool(body.get("allowed", True)),
        applied=bool(body.get("applied", False)),
        retry_after_seconds=max(int(body.get("retry_after_seconds", 0) or 0), 0),
        degraded=bool(body.get("degraded", False)),
        local_fallback=bool(body.get("local_fallback", False)),
        policy_name=policy_name,
    )
