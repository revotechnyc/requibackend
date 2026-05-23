"""
OpenAI Realtime API — WebRTC session config for Requi Sonia Assistant (Marin voice).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from typing import Any, Dict, Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

OPENAI_REALTIME_CALLS_URL = "https://api.openai.com/v1/realtime/calls"
OPENAI_REALTIME_CLIENT_SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"

# Realtime SDP negotiation can be slow; retry transient gateway errors.
REALTIME_HTTP_TIMEOUT = 120.0
REALTIME_MAX_RETRIES = 3
REALTIME_RETRY_DELAY_SEC = 2.0

# Prevent duplicate parallel OpenAI negotiations (e.g. React Strict Mode double-mount).
_negotiate_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def is_realtime_voice_configured() -> bool:
    return bool((settings.openai_voice_prompt_id or "").strip() and settings.openai_api_key)


def build_realtime_session_config() -> Dict[str, Any]:
    """Session for Requi Sonia — must match the model bound to the stored prompt template."""
    prompt_id = (settings.openai_voice_prompt_id or "").strip()
    version = (settings.openai_voice_prompt_version or "7").strip() or "7"
    voice = (settings.openai_realtime_voice or "marin").strip() or "marin"
    # Playground prompt pmpt_69f7... is tied to gpt-realtime-1.5 (not gpt-realtime / gpt-4o-realtime-preview).
    model = (settings.openai_realtime_model or "gpt-realtime-1.5").strip()

    body: Dict[str, Any] = {
        "type": "realtime",
        "model": model,
        "prompt": {"id": prompt_id, "version": version},
        "output_modalities": ["audio"],
        "audio": {
            "output": {"voice": voice},
            "input": {
                # Required for user speech to appear as text in the UI / history.
                "transcription": {
                    "model": "gpt-4o-mini-transcribe",
                    "language": "en",
                },
                # semantic_vad chunks utterances; create_response is False so the client
                # triggers exactly one response.create per finalized user transcript (prevents
                # iOS / multi-segment VAD from generating 2–3 assistant replies per phrase).
                "turn_detection": {
                    "type": "semantic_vad",
                    "eagerness": "low",
                    "create_response": False,
                    "interrupt_response": False,
                },
            },
        },
    }
    return body


def validate_sdp_offer(offer_sdp: str) -> str:
    """
    Normalize and validate client SDP before forwarding to OpenAI.

    Do NOT use str.strip() on the whole SDP — it removes the trailing newline
    required by RFC 4566 and causes OpenAI to return "failed to unmarshal SDP: EOF".
    """
    if not offer_sdp or not offer_sdp.strip():
        raise ValueError("SDP offer is empty")

    sdp = offer_sdp.replace("\r\n", "\n").replace("\r", "\n")
    if not sdp.lstrip().startswith("v=0"):
        raise ValueError("SDP offer is invalid (missing v=0 line)")
    if "m=audio" not in sdp:
        raise ValueError("SDP offer must include an audio media section (m=audio)")

    if not sdp.endswith("\n"):
        sdp += "\n"
    return sdp.replace("\n", "\r\n")


def extract_sdp_answer(raw: str) -> str:
    """
    OpenAI may return SDP followed by JSON events (e.g. session.created) in one body.
    WebRTC requires SDP only — trailing JSON breaks setRemoteDescription.
    """
    text = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise ValueError("OpenAI returned an empty SDP answer")

    # Pure JSON error or session payload
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
            nested = data.get("sdp") if isinstance(data, dict) else None
            if isinstance(nested, str) and nested.strip().startswith("v=0"):
                return validate_sdp_offer(nested)
        except json.JSONDecodeError:
            pass
        raise ValueError("OpenAI returned JSON instead of an SDP answer")

    lines: list[str] = []
    for line in text.split("\n"):
        if line.strip().startswith("{") or line.strip().startswith("["):
            break
        lines.append(line)

    sdp_only = "\n".join(lines).strip()
    if "{" in text and sdp_only:
        logger.info("Stripped trailing non-SDP content from OpenAI realtime answer")

    return validate_sdp_offer(sdp_only)


def _openai_auth_headers(safety_identifier: Optional[str] = None) -> Dict[str, str]:
    headers = {"Authorization": f"Bearer {settings.openai_api_key}"}
    if safety_identifier:
        headers["OpenAI-Safety-Identifier"] = safety_identifier
    return headers


def _parse_openai_failure(response: httpx.Response, session_json: Dict[str, Any]) -> ValueError:
    detail = (response.text or "")[:500]
    err_code = ""
    try:
        err_body = response.json()
        err = err_body.get("error", {}) if isinstance(err_body, dict) else {}
        err_code = str(err.get("code", ""))
        detail = str(err.get("message", detail))
    except json.JSONDecodeError:
        if "Gateway time-out" in detail or response.status_code == 504:
            detail = "OpenAI gateway timeout"

    if err_code == "model_not_found" or "does not exist or you do not have access" in detail:
        model_name = session_json.get("model", "unknown")
        return ValueError(
            f"Realtime model '{model_name}' is not available for your OpenAI API key. "
            "Set OPENAI_REALTIME_MODEL=gpt-realtime-1.5 in the backend .env and restart the API."
        )

    if response.status_code in (502, 503, 504) or "Gateway time-out" in detail or "gateway timeout" in detail.lower():
        return ValueError(
            "OpenAI live voice is temporarily unavailable (gateway timeout). "
            "Please wait a moment and try Live again."
        )

    return ValueError(f"OpenAI Realtime API error ({response.status_code}): {detail}")


async def _create_ephemeral_realtime_token(
    client: httpx.AsyncClient,
    session_json: Dict[str, Any],
    *,
    safety_identifier: Optional[str] = None,
) -> str:
    headers = {**_openai_auth_headers(safety_identifier), "Content-Type": "application/json"}
    response = await client.post(
        OPENAI_REALTIME_CLIENT_SECRETS_URL,
        headers=headers,
        json={"session": session_json},
    )
    if response.status_code >= 400:
        raise _parse_openai_failure(response, session_json)

    data = response.json()
    token = data.get("value") if isinstance(data, dict) else None
    if not token:
        raise ValueError("OpenAI did not return an ephemeral Realtime token")
    return str(token)


async def _exchange_sdp_ephemeral(
    client: httpx.AsyncClient,
    sdp: str,
    ephemeral_token: str,
    session_json: Dict[str, Any],
) -> str:
    """POST raw SDP with ephemeral token (OpenAI-recommended browser/server flow)."""
    response = await client.post(
        OPENAI_REALTIME_CALLS_URL,
        headers={
            "Authorization": f"Bearer {ephemeral_token}",
            "Content-Type": "application/sdp",
        },
        content=sdp.encode("utf-8"),
    )
    if response.status_code not in (200, 201):
        raise _parse_openai_failure(response, session_json)

    return extract_sdp_answer(response.text or "")


async def _exchange_sdp_unified(
    client: httpx.AsyncClient,
    sdp: str,
    session_str: str,
    session_json: Dict[str, Any],
    *,
    safety_identifier: Optional[str] = None,
) -> str:
    """Fallback: single multipart call with API key + session JSON."""
    response = await client.post(
        OPENAI_REALTIME_CALLS_URL,
        headers=_openai_auth_headers(safety_identifier),
        files={
            "sdp": (None, sdp),
            "session": (None, session_str),
        },
    )
    if response.status_code not in (200, 201):
        raise _parse_openai_failure(response, session_json)

    return extract_sdp_answer(response.text or "")


async def negotiate_webrtc_call(
    offer_sdp: str,
    *,
    safety_identifier: Optional[str] = None,
) -> str:
    """
    POST SDP offer to OpenAI Realtime; returns SDP answer text.

    Uses ephemeral token + raw SDP first (faster, matches OpenAI docs), then
    multipart unified interface as fallback. Retries transient 502/503/504 errors.
    """
    if not is_realtime_voice_configured():
        raise ValueError("Realtime voice is not configured (OPENAI_VOICE_PROMPT_ID)")

    sdp = validate_sdp_offer(offer_sdp)
    session_json = build_realtime_session_config()
    session_str = json.dumps(session_json, separators=(",", ":"))
    lock_key = safety_identifier or "anonymous"

    logger.info(
        "OpenAI realtime connect: sdp_bytes=%s session_bytes=%s model=%s user=%s",
        len(sdp.encode("utf-8")),
        len(session_str.encode("utf-8")),
        session_json.get("model"),
        lock_key,
    )

    async with _negotiate_locks[lock_key]:
        return await _negotiate_webrtc_call_locked(
            sdp, session_json, session_str, safety_identifier=safety_identifier
        )


async def _negotiate_webrtc_call_locked(
    sdp: str,
    session_json: Dict[str, Any],
    session_str: str,
    *,
    safety_identifier: Optional[str] = None,
) -> str:
    last_error: Optional[Exception] = None

    async with httpx.AsyncClient(timeout=REALTIME_HTTP_TIMEOUT) as client:
        # Unified multipart (API key + sdp + session) returns a clean SDP answer per OpenAI docs.
        for attempt in range(1, REALTIME_MAX_RETRIES + 1):
            try:
                answer = await _exchange_sdp_unified(
                    client, sdp, session_str, session_json, safety_identifier=safety_identifier
                )
                logger.warning(
                    "OpenAI realtime OK via unified multipart (attempt %s) answer_bytes=%s prefix=%r",
                    attempt,
                    len(answer.encode("utf-8")),
                    answer[:60],
                )
                return answer
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                logger.warning(
                    "Realtime unified attempt %s network error: %r",
                    attempt,
                    exc,
                )
            except ValueError as exc:
                msg = str(exc)
                if "gateway timeout" in msg.lower() or "temporarily unavailable" in msg.lower():
                    last_error = exc
                    logger.warning("Realtime unified attempt %s: %s", attempt, msg)
                else:
                    raise
            if attempt < REALTIME_MAX_RETRIES:
                await asyncio.sleep(REALTIME_RETRY_DELAY_SEC * attempt)

        # Fallback: ephemeral token + raw SDP
        for attempt in range(1, REALTIME_MAX_RETRIES + 1):
            try:
                token = await _create_ephemeral_realtime_token(
                    client, session_json, safety_identifier=safety_identifier
                )
                answer = await _exchange_sdp_ephemeral(client, sdp, token, session_json)
                logger.warning(
                    "OpenAI realtime OK via ephemeral (attempt %s) answer_bytes=%s prefix=%r",
                    attempt,
                    len(answer.encode("utf-8")),
                    answer[:60],
                )
                return answer
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                logger.warning(
                    "Realtime ephemeral attempt %s network error: %r",
                    attempt,
                    exc,
                )
            except ValueError as exc:
                msg = str(exc)
                if "gateway timeout" in msg.lower() or "temporarily unavailable" in msg.lower():
                    last_error = exc
                    logger.warning("Realtime ephemeral attempt %s: %s", attempt, msg)
                else:
                    raise
            if attempt < REALTIME_MAX_RETRIES:
                await asyncio.sleep(REALTIME_RETRY_DELAY_SEC * attempt)

    if isinstance(last_error, ValueError):
        raise last_error
    raise ValueError(
        "OpenAI live voice is temporarily unavailable (network timeout). "
        "Please try again in a moment."
    ) from last_error
