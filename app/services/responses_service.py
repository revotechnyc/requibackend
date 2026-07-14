"""
OpenAI Responses API service — prompt-driven chat with vector store context injection.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

from app.core.config import settings

logger = logging.getLogger(__name__)

SONIA_SYSTEM_INSTRUCTION = (
    "Your name is Sonia. You are a warm, helpful Healthcare Compliance Assistant. "
    "When this turn includes explicit attached or selected document text, treat that text as "
    "the authoritative source — never say the file is missing from a library or ask the user "
    "to re-upload it unless the provided document body is empty. "
    "When no explicit document context is provided, search the knowledge base before answering. "
    "For very long page-by-page or whole-document review requests, complete a coherent batch "
    "in one reply (as much as fits), then invite the user to reply Continue for the next batch — "
    "do not invent that the document is unavailable."
)

# Injected only when Intelligence attaches / selects documents for the turn.
DOCUMENT_TURN_DEVELOPER_INSTRUCTION = (
    "DOCUMENT CONTEXT RULES FOR THIS TURN:\n"
    "- One or more documents were loaded into this message as explicit text sections.\n"
    "- The files ARE available. Do not say you cannot see them, that they are not in the library, "
    "or ask for a re-upload unless a loaded document body is literally empty.\n"
    "- Cite using the document title and Section N (or quotes from the provided text). "
    "Only claim PDF page numbers when the text itself states a page number.\n"
    "- Chat attachments are not the same as an OpenAI vector-store library file; that distinction "
    "must never be used as a reason to refuse analysis.\n"
    "- If the user requests an exhaustive multi-page review that cannot fit in one reply, "
    "deliver the first coherent batch in the exact format they asked for, then end with a short "
    "line inviting them to reply \"Continue\" for the next sections."
)


async def mock_chat_stream_for_testing(
    user_message: str,
    delay_ms: int = 0,
) -> AsyncGenerator[str, None]:
    """Simulated SSE for MOCK_CHAT_STREAM=true. No sleeps unless delay_ms > 0."""
    delay_s = max(0, delay_ms) / 1000.0

    async def pause() -> None:
        if delay_s > 0:
            await asyncio.sleep(delay_s)

    preview = (user_message or "").strip()[:200] or "(empty message)"

    yield f"data: {json.dumps({'type': 'phase', 'phase': 'searching'})}\n\n"
    yield f"data: {json.dumps({'type': 'phase', 'phase': 'search_complete', 'sources': 2, 'filenames': ['HIPAA_Security_Rule.pdf', 'FWA_Policy.docx']})}\n\n"

    reasoning = (
        "Scanning the knowledge base for HIPAA administrative safeguards, "
        "workforce training requirements, and audit documentation…"
    )
    yield f"data: {json.dumps({'type': 'reasoning_start'})}\n\n"
    for chunk in reasoning.split(" "):
        yield f"data: {json.dumps({'type': 'reasoning', 'content': chunk + ' '})}\n\n"
        await pause()
    yield f"data: {json.dumps({'type': 'reasoning_done'})}\n\n"

    answer = (
        f"**[Test stream]** Response to your question:\n\n"
        f"> {preview}\n\n"
        "Based on typical **HIPAA §164.308** administrative safeguards:\n\n"
        "1. **Security management** — risk analysis and risk management policies.\n"
        "2. **Workforce security** — authorization, supervision, and termination procedures.\n"
        "3. **Training** — security awareness for all workforce members.\n\n"
        "_Mock stream. Set MOCK_CHAT_STREAM=false for live OpenAI._"
    )

    chunk_size = 32
    for i in range(0, len(answer), chunk_size):
        piece = answer[i : i + chunk_size]
        yield f"data: {json.dumps({'type': 'token', 'content': piece})}\n\n"
        await pause()

    yield f"data: {json.dumps({'type': 'done', 'content': answer, 'tokens_used': len(answer.split())})}\n\n"


def _sse_delta(payload: dict) -> str:
    """Extract text delta from OpenAI stream JSON payloads."""
    for key in ("delta", "text", "content"):
        val = payload.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


def _extract_output_text_from_response(payload: dict) -> str:
    resp = payload.get("response", payload)
    parts: List[str] = []
    for item in resp.get("output", []):
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    parts.append(part.get("text", ""))
    return "".join(parts)


async def _sse_event(event: dict) -> str:
    """Let the event loop flush each chunk to the client (avoid one TCP dump)."""
    await asyncio.sleep(0)
    return f"data: {json.dumps(event)}\n\n"


async def _stream_answer_chunks(
    text: str,
    chunk_size: int = 28,
    pace_ms: int = 12,
) -> AsyncGenerator[str, None]:
    """Emit token SSE when OpenAI only returns full text on completed."""
    pace_s = max(0, pace_ms) / 1000.0
    for i in range(0, len(text), chunk_size):
        piece = text[i : i + chunk_size]
        yield await _sse_event({"type": "token", "content": piece})
        if pace_s > 0:
            await asyncio.sleep(pace_s)


def _user_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "input_text":
                parts.append(item.get("text", ""))
        return " ".join(parts).strip()
    return str(content) if content is not None else ""


class ResponsesService:
    """OpenAI Responses API with stored prompt + vector store search."""

    def __init__(
        self,
        system_instruction: Optional[str] = None,
        request_id: Optional[str] = None,
        skip_vector_search: bool = False,
        document_source_labels: Optional[List[str]] = None,
    ):
        self.api_key = settings.openai_api_key
        self.base_url = "https://api.openai.com/v1/responses"
        vs_id = (settings.openai_vector_store_id or "").strip()
        self.vector_store_id = vs_id
        self.vs_search_url = (
            f"https://api.openai.com/v1/vector_stores/{vs_id}/search" if vs_id else ""
        )
        self.model = settings.openai_responses_model
        self.prompt_id = settings.openai_prompt_id
        self.prompt_version = settings.openai_prompt_version
        self.system_instruction = system_instruction
        self.request_id = request_id or str(uuid.uuid4())[:8]
        self.skip_vector_search = skip_vector_search
        self.document_source_labels = document_source_labels or []

    async def _search_vector_store(self, query: str, max_results: int = 5) -> List[Dict]:
        if not self.vs_search_url:
            return []

        import httpx

        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.vs_search_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={"query": query, "max_num_results": max_results},
                timeout=30,
            )
            if response.status_code != 200:
                logger.warning(
                    "[%s] Vector store search failed: %s",
                    self.request_id,
                    response.status_code,
                )
                return []
            return response.json().get("data", [])

    def _build_context_message(self, results: List[Dict]) -> Optional[str]:
        if not results:
            return None

        parts = [
            "The following are relevant excerpts from the organization's knowledge base. "
            "Use them as your primary source to answer the user's question. "
            "Cite the source filename and relevance score where applicable.",
        ]
        for i, r in enumerate(results):
            filename = r.get("filename", "unknown")
            score = r.get("score", 0)
            content_parts = r.get("content", [])
            text = " ".join(
                c.get("text", "") for c in content_parts if c.get("type") == "text"
            )
            if len(text) > 3000:
                text = text[:3000] + "..."
            parts.append(
                f"\n--- Source {i + 1}: {filename} (relevance: {score:.2f}) ---\n{text}"
            )
        return "\n".join(parts)

    async def _inject_context(
        self, messages: List[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], List[Dict]]:
        last_user_msg = None
        last_user_idx = -1
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                last_user_msg = _user_content_text(messages[i].get("content", ""))
                last_user_idx = i
                break

        if not last_user_msg:
            msgs = list(messages)
            if self.system_instruction:
                msgs.insert(0, {"role": "developer", "content": self.system_instruction})
            return msgs, []

        if self.skip_vector_search:
            msgs = list(messages)
            if self.system_instruction:
                msgs.insert(0, {"role": "developer", "content": self.system_instruction})
            return msgs, []

        results = await self._search_vector_store(last_user_msg)

        if not results:
            logger.info(
                json.dumps(
                    {
                        "event": "vector_search",
                        "request_id": self.request_id,
                        "query_preview": last_user_msg[:100],
                        "num_results": 0,
                        "vs_id": self.vector_store_id,
                    }
                )
            )
            msgs = list(messages)
            if self.system_instruction:
                msgs.insert(0, {"role": "developer", "content": self.system_instruction})
            return msgs, []

        context_text = self._build_context_message(results)
        msgs = list(messages)
        if last_user_idx >= 0 and context_text:
            msgs.insert(last_user_idx, {"role": "developer", "content": context_text})

        if self.system_instruction:
            msgs.insert(0, {"role": "developer", "content": self.system_instruction})

        logger.info(
            json.dumps(
                {
                    "event": "vector_search",
                    "request_id": self.request_id,
                    "query_preview": last_user_msg[:100],
                    "num_results": len(results),
                    "filenames": [r.get("filename", "?") for r in results],
                    "scores": [round(r.get("score", 0), 3) for r in results],
                    "vs_id": self.vector_store_id,
                    "context_injected": True,
                }
            )
        )

        return msgs, results

    def _build_request(
        self,
        messages: List[Dict[str, Any]],
        stream: bool = False,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "model": self.model,
            "input": messages,
            "stream": stream,
            "reasoning": {"summary": "auto"},
            "include": ["reasoning.encrypted_content", "web_search_call.action.sources"],
        }
        if self.prompt_id:
            body["prompt"] = {
                "id": self.prompt_id,
                "version": self.prompt_version or "1",
            }
        return body

    async def chat(self, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        import httpx

        enriched_messages, search_results = await self._inject_context(messages)
        body = self._build_request(enriched_messages, stream=False)

        logger.info(
            json.dumps(
                {
                    "event": "responses_request",
                    "request_id": self.request_id,
                    "prompt_id": self.prompt_id,
                    "model": self.model,
                    "vector_chunks_found": len(search_results),
                }
            )
        )

        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=120,
            )
            data = response.json()

        if response.status_code != 200:
            error_msg = data.get("error", {}).get("message", str(data))
            logger.error(
                json.dumps(
                    {
                        "event": "responses_error",
                        "request_id": self.request_id,
                        "status_code": response.status_code,
                        "error": error_msg,
                    }
                )
            )
            return {
                "content": f"Error: {error_msg}",
                "role": "assistant",
                "tokens_used": 0,
                "error": error_msg,
            }

        result = self._parse_response(data)
        logger.info(
            json.dumps(
                {
                    "event": "responses_response",
                    "request_id": self.request_id,
                    "tokens_used": result.get("tokens_used"),
                }
            )
        )
        return result

    async def chat_stream(
        self, messages: List[Dict[str, Any]]
    ) -> AsyncGenerator[str, None]:
        import httpx
        import time as _time

        yield await _sse_event({"type": "phase", "phase": "searching"})
        t_search_start = _time.time()

        enriched_messages, search_results = await self._inject_context(messages)

        t_search_done = _time.time()
        logger.info(
            json.dumps(
                {
                    "event": "vector_search_timing",
                    "request_id": self.request_id,
                    "search_duration_ms": round((t_search_done - t_search_start) * 1000),
                    "results": len(search_results),
                }
            )
        )

        filenames = (
            self.document_source_labels
            if self.skip_vector_search and self.document_source_labels
            else [r.get("filename", "unknown") for r in search_results]
        )
        yield await _sse_event(
            {
                "type": "phase",
                "phase": "search_complete",
                "sources": len(filenames),
                "filenames": filenames,
            }
        )

        body = self._build_request(enriched_messages, stream=True)
        full_content = ""

        logger.info(
            json.dumps(
                {
                    "event": "responses_stream_request",
                    "request_id": self.request_id,
                    "prompt_id": self.prompt_id,
                    "model": self.model,
                    "vector_chunks_found": len(search_results),
                }
            )
        )

        t_openai_start = _time.time()

        async with httpx.AsyncClient() as client:
            async with client.stream(
                "POST",
                self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=300,
            ) as response:
                if response.status_code != 200:
                    error_data = await response.aread()
                    try:
                        err = json.loads(error_data)
                        msg = err.get("error", {}).get("message", str(error_data))
                    except Exception:
                        msg = str(error_data)
                    logger.error(
                        json.dumps(
                            {
                                "event": "responses_stream_error",
                                "request_id": self.request_id,
                                "status_code": response.status_code,
                                "error": msg,
                            }
                        )
                    )
                    yield await _sse_event({"type": "error", "error": msg})
                    return

                current_event = ""
                token_events_emitted = 0
                async for line in response.aiter_lines():
                    if line.startswith("event: "):
                        current_event = line[7:].strip()
                    elif line.startswith("data: "):
                        raw = line[6:]
                        if raw.strip() == "[DONE]":
                            continue
                        if current_event == "response.created":
                            yield await _sse_event({"type": "phase", "phase": "processing"})
                        elif current_event == "response.in_progress":
                            yield await _sse_event({"type": "phase", "phase": "generating"})
                        elif current_event in (
                            "response.reasoning_summary_part.added",
                            "response.reasoning_part.added",
                        ):
                            yield await _sse_event({"type": "reasoning_start"})
                        elif current_event in (
                            "response.reasoning_summary_text.delta",
                            "response.reasoning_text.delta",
                        ):
                            try:
                                payload = json.loads(raw)
                                delta = _sse_delta(payload)
                                if delta:
                                    yield await _sse_event(
                                        {"type": "reasoning", "content": delta}
                                    )
                            except json.JSONDecodeError:
                                pass
                        elif current_event in (
                            "response.reasoning_summary_text.done",
                            "response.reasoning_text.done",
                        ):
                            yield await _sse_event({"type": "reasoning_done"})
                        elif current_event == "response.output_item.added":
                            yield await _sse_event({"type": "output_start"})
                        elif current_event in (
                            "response.output_text.delta",
                            "response.content_part.delta",
                            "response.text.delta",
                        ):
                            try:
                                payload = json.loads(raw)
                                delta = _sse_delta(payload)
                                if delta:
                                    full_content += delta
                                    token_events_emitted += 1
                                    yield await _sse_event(
                                        {"type": "token", "content": delta}
                                    )
                            except json.JSONDecodeError:
                                pass
                        elif current_event == "response.completed":
                            try:
                                payload = json.loads(raw)
                                resp = payload.get("response", payload)
                                usage = resp.get("usage", {})
                                tokens = usage.get("total_tokens", 0)
                                if not full_content:
                                    full_content = _extract_output_text_from_response(payload)
                                if full_content and token_events_emitted == 0:
                                    async for chunk_evt in _stream_answer_chunks(
                                        full_content
                                    ):
                                        yield chunk_evt
                                        token_events_emitted += 1
                                yield await _sse_event(
                                    {
                                        "type": "done",
                                        "content": full_content,
                                        "tokens_used": tokens,
                                    }
                                )
                            except json.JSONDecodeError:
                                yield await _sse_event(
                                    {
                                        "type": "done",
                                        "content": full_content,
                                        "tokens_used": 0,
                                    }
                                )
                            return
                        elif current_event == "response.error":
                            try:
                                payload = json.loads(raw)
                                err_msg = payload.get("error", {}).get("message", str(payload))
                            except Exception:
                                err_msg = raw
                            yield await _sse_event({"type": "error", "error": err_msg})
                            return
                        elif current_event.endswith(".delta"):
                            try:
                                payload = json.loads(raw)
                                delta = _sse_delta(payload)
                                if not delta:
                                    continue
                                if "reasoning" in current_event:
                                    yield await _sse_event(
                                        {"type": "reasoning", "content": delta}
                                    )
                                else:
                                    full_content += delta
                                    token_events_emitted += 1
                                    yield await _sse_event(
                                        {"type": "token", "content": delta}
                                    )
                            except json.JSONDecodeError:
                                pass

    @staticmethod
    def _parse_response(data: dict) -> dict:
        content = ""
        for item in data.get("output", []):
            if item.get("type") == "message":
                for part in item.get("content", []):
                    if part.get("type") == "output_text":
                        content += part.get("text", "")

        usage = data.get("usage", {})
        return {
            "content": content,
            "role": "assistant",
            "tokens_used": usage.get("total_tokens", 0),
            "response_id": data.get("id"),
        }
