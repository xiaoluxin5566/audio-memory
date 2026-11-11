from __future__ import annotations

from .performance import timed_stage, span, emit

import asyncio
from copy import deepcopy
import json
import math
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from pydantic import BaseModel, PrivateAttr

from .usage import usage_record

CORPORATE_STRUCTURED_OUTPUT_VERSION = 2
NATIVE_JSON_SYSTEM = "只返回一个合法JSON对象，不使用函数或代码围栏。完整遵守所给JSON Schema及原有来源、证据与语义规则，JSON结束后不追加任何内容。JSON_SCHEMA:\n"

CHAT_MODEL = "volcengine-deepseek-v4_1-flash"
CHAT_ENDPOINT = (
    "http://api-hub.inner.chj.cloud/llm-gateway/v1/chat/completions/" + CHAT_MODEL
)
EMBEDDING_MODEL = "Qwen/Qwen3-Embedding-4B"
CORPORATE_EMBEDDING_MODEL = "kivy-qwen3-embedding-8b"
CORPORATE_EMBEDDING_ENDPOINT = (
    "http://api-hub.inner.chj.cloud/llm-gateway/v1/embeddings/"
    + CORPORATE_EMBEDDING_MODEL
)
TASKS = {
    "event_query": "Retrieve events relevant to this query.",
    "entity_query": "Retrieve candidates referring to the same real-world entity.",
    "property_query": "Retrieve semantically equivalent properties.",
}


def parse_model_object(text):
    """Accept only a JSON object, optionally inside one complete code fence."""
    payload = text.strip()
    lines = payload.splitlines()
    if (
        len(lines) >= 3
        and lines[0].lower() in {"```json", "```"}
        and lines[-1] == "```"
    ):
        payload = "\n".join(lines[1:-1])
    try:
        # Some gateway outputs contain literal line breaks inside JSON strings.
        # Escape only string whitespace; do not infer fields, quotes or values.
        encoded, quoted, escaped = [], False, False
        for character in payload:
            if quoted and not escaped and character in '\n\r\t':
                encoded.append(json.dumps(character)[1:-1])
                continue
            encoded.append(character)
            if escaped:
                escaped = False
            elif quoted and character == '\\':
                escaped = True
            elif character == '"':
                quoted = not quoted
        def unique(pairs):
            values={}
            for key,value in pairs:
                if key in values:raise ValueError('duplicate_model_json_key')
                values[key]=value
            return values
        data = json.loads(''.join(encoded),object_pairs_hook=unique)
    except (ValueError, TypeError) as error:
        raise ValueError("invalid_model_json") from error
    if not isinstance(data, dict):
        raise ValueError("invalid_model_json")  # noqa: TRY004 - invalid response value, handled with other JSON contract failures
    return data


class ModelOutputTruncated(ValueError):
    """Rejected partial output plus diagnostics; never treated as a valid result.

    Request metadata is allowlisted: no credentials or input messages.
    Callers choose whether to persist private response data in their local audit.
    """
    def __init__(self, response_data, request_id, body):
        super().__init__('model_output_truncated')
        self.response_data = deepcopy(response_data)
        self.usage = deepcopy(response_data.get('usage'))
        self.request_id = request_id
        self.request_metadata = {k:deepcopy(body[k]) for k in
            ('model','max_tokens','enable_thinking','thinking_budget','reasoning_effort','temperature','response_format') if k in body}


class ChatResult(BaseModel):
    # Parsing may normalize the text envelope; private audit retains the response.
    _structured_response: dict | None = PrivateAttr(default=None)
    text: str
    tool_calls: list[dict]
    finish_reason: str
    usage: dict
    assistant_message: dict


class EmbeddingBatch(BaseModel):
    indexed_vectors: list[list[float]]
    usage: dict
    provider_revision: str


async def request(
    client,
    endpoint,
    body,
    headers,
    retries,
    *,
    corporate=False,
    audit=None,
    purpose="unknown",
    provider="unknown",
    timeout=120,
):
    for attempt in range(retries + 1):
        actual_headers = dict(headers)
        if corporate:
            actual_headers["BCS-APIHub-RequestId"] = str(uuid4())
        attempt_start = time.monotonic()
        recorded = False
        try:
            with span("http_attempt", purpose=purpose, attempt=attempt+1, request_id=actual_headers.get("BCS-APIHub-RequestId"), payload_bytes=len(json.dumps(body,ensure_ascii=False,separators=(",",":")).encode())):
                response = await client.post(endpoint, json=body, headers=actual_headers, timeout=timeout)
                emit("http_status", purpose=purpose, attempt=attempt+1, status=response.status_code)
            if audit is not None:
                try:
                    usage = response.json().get("usage")
                except ValueError:
                    usage = None
                await audit(
                    {
                        "purpose": purpose,
                        "provider": provider,
                        "request_id": actual_headers.get("BCS-APIHub-RequestId"),
                        "status": str(response.status_code),
                        "usage_json": usage or {"total_tokens": None, "cost": None},
                        "elapsed_ms": (time.monotonic() - attempt_start) * 1000,
                    }
                )
                recorded = True
            response.raise_for_status()
            return response.json(), actual_headers.get("BCS-APIHub-RequestId")
        except (httpx.TransportError, httpx.HTTPStatusError) as error:
            if audit is not None and not recorded:
                await audit(
                    {
                        "purpose": purpose,
                        "provider": provider,
                        "request_id": actual_headers.get("BCS-APIHub-RequestId"),
                        "status": "transport_error",
                        "usage_json": {"total_tokens": None, "cost": None},
                        "error_code": type(error).__name__,
                        "elapsed_ms": (time.monotonic() - attempt_start) * 1000,
                    }
                )
            response = getattr(error, "response", None)
            if attempt == retries or (
                response is not None
                and response.status_code != 429
                and response.status_code < 500
            ):
                raise
            retry_after = (
                response.headers.get("retry-after", "") if response is not None else ""
            )
            delay = (
                float(retry_after)
                if retry_after.replace(".", "", 1).isdigit()
                else 2**attempt
            )
            with span("transport_backoff", purpose=purpose, attempt=attempt+1):
                await asyncio.sleep(delay)


class CorporateChatClient:
    def __init__(
        self,
        token: str,
        client: httpx.AsyncClient,
        *,
        audit=None,
        max_tokens=None,
        thinking_budget=None,
        retries=None,
        thinking_purposes=None,
        purpose_max_tokens=None,
        purpose_reasoning_effort=None,
        purpose_read_timeouts=None,
    ):
        self.token, self.client, self.audit = token, client, audit
        self.thinking_purposes = set(thinking_purposes) if thinking_purposes is not None else {'react', 'synthesis', 'fact_adjudication', 'segmentation'}
        self.purpose_max_tokens = {"source_interpretation": 4096, "read": 8192, "extraction": 8192, "extraction_review": 4096, "fact_context_review": 8192, "fact_adjudication": 8192, "fact_adjudication_bounded": 8192, "fact_grounding_review": 4096, "fact_evidence_selection": 4096, "clause_coverage_review": 8192}
        self.purpose_max_tokens.update(purpose_max_tokens or {})
        self.purpose_reasoning_effort = dict(purpose_reasoning_effort or {})
        self.purpose_read_timeouts = dict(purpose_read_timeouts or {})
        if any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
               for value in self.purpose_read_timeouts.values()):
            raise ValueError('invalid_purpose_read_timeout')
        self.max_tokens, self.thinking_budget, self.retries = (
            max_tokens,
            thinking_budget,
            retries,
        )

    @timed_stage("chat_generation")
    async def complete(self, messages, tools=None, purpose="react") -> ChatResult:
        if not self.token:
            raise ValueError("configuration_not_ready:corporate_token")
        if purpose == "react":
            from .query_context import compact_read_messages
            messages = compact_read_messages(messages)
        structured = purpose in {"segmentation", "extraction", "resolution"}
        if structured and not tools:
            messages = [
                {
                    "role": "system",
                    "content": "只输出一个合法JSON对象，不要Markdown代码块、前言或额外文字。遵守后续输出结构与证据约束。",
                },
                *messages,
            ]
        result_tool=None
        if purpose!='react'and isinstance(tools,list)and len(tools)==1:
            tool=tools[0];function=tool.get('function',{})
            if tool.get('type')=='function'and function.get('name')=='submit_structured_result'and isinstance(function.get('parameters'),dict):result_tool=function
        native_json=bool(result_tool)or(structured and not tools)
        if result_tool:
            from .structured_output import RETURN_SYSTEM
            schema = result_tool['parameters']
            root_hint = ''
            if schema.get('additionalProperties') is False:
                root_hint = ('允许的顶层字段： ' + json.dumps(list(schema.get('properties', {})), ensure_ascii=False)
                             + '。仅返回这些字段对应的业务数据，不输出Schema定义或额外外壳；接口控制参数不属于结果数据。\n')
            messages=[dict(role='system',content=root_hint+NATIVE_JSON_SYSTEM+json.dumps(schema,ensure_ascii=False,separators=(',',':'))),
                      *[m for m in messages if not(m.get('role')=='system'and m.get('content')==RETURN_SYSTEM)]]
        body = {
            "model": CHAT_MODEL,
            "messages": messages,
            "enable_thinking": purpose in self.thinking_purposes,
            "parallel_tool_calls": False,
        }
        output_limit = self.purpose_max_tokens.get(purpose, self.max_tokens)
        if output_limit is not None:
            body["max_tokens"] = min(output_limit,self.max_tokens) if self.max_tokens is not None else output_limit
        if body["enable_thinking"] and self.thinking_budget is not None:
            body["thinking_budget"] = self.thinking_budget
        if body['enable_thinking'] and purpose in self.purpose_reasoning_effort:
            body['reasoning_effort'] = self.purpose_reasoning_effort[purpose]
        if purpose in {"fact_grounding_review","source_interpretation"}:body["temperature"]=0
        if purpose in {"fact_adjudication", "fact_adjudication_bounded"}:
            body["temperature"] = 0
            if body["enable_thinking"]:
                body["thinking_budget"] = self.thinking_budget or 4096
                body["reasoning_effort"] = "low"
        if native_json:
            # Revalidated on this fixed deployment 2026-10-26, with and without
            # thinking: HTTP200, exact synthetic JSON. This is data submission,
            # not an executable agent tool. Local strict validators still run.
            body['response_format']={'type':'json_object'}
        elif tools:
            body["tools"] = tools
        start = time.monotonic()
        default_timeout = 240 if purpose in {'extraction', 'extraction_review', 'fact_context_review','fact_adjudication'} and body['enable_thinking'] else 120
        timeout = (httpx.Timeout(default_timeout, read=self.purpose_read_timeouts[purpose])
                   if purpose in self.purpose_read_timeouts else default_timeout)
        # Agent transport retries cannot conceal autonomous action retries.
        data, request_id = await request(
            self.client,
            CHAT_ENDPOINT,
            body,
            {"X-CHJ-GWToken": self.token, "Content-Type": "application/json"},
            0 if purpose == "react" else (self.retries if self.retries is not None else 2),
            corporate=True,
            audit=self.audit,
            purpose=purpose,
            provider="corporate",
            timeout=timeout,
        )
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length":
            raise ModelOutputTruncated(data, request_id, body)
        message = choice["message"]
        calls = message.get("tool_calls") or []
        text = message.get("content") or ""
        if not text and not calls:
            raise ValueError("empty_model_response")
        if len({c.get("id") for c in calls}) != len(calls):
            raise ValueError("duplicate_tool_call_id")
        for call in calls:
            if (
                not call.get("id")
                or call.get("type") != "function"
                or not call.get("function", {}).get("name")
            ):
                raise ValueError("invalid_tool_call")
        if structured and not calls and not result_tool:
            # Preserve the established exact-fence wrapper contract for direct
            # consumers; duplicate keys are rejected before canonicalization.
            text=json.dumps(parse_model_object(text),ensure_ascii=False)
        return ChatResult(
            text=text,
            tool_calls=calls,
            finish_reason=choice.get("finish_reason", "unknown"),
            assistant_message=dict(message, role="assistant"),
            usage=usage_record(
                data,
                purpose=purpose,
                provider="corporate",
                request_id=request_id,
                elapsed_ms=(time.monotonic() - start) * 1000,
            ),
        )


class EmbeddingClient:
    def __init__(
        self,
        key,
        client,
        *,
        provider,
        endpoint,
        revision,
        dimension=2560,
        audit=None,
        model=EMBEDDING_MODEL,
        corporate=False,
        retries=2,
    ):
        if corporate and (
            provider != "corporate_apihub"
            or endpoint != CORPORATE_EMBEDDING_ENDPOINT
            or model != CORPORATE_EMBEDDING_MODEL
        ):
            raise ValueError("corporate_embedding_endpoint_required")
        if dimension is None:
            if not corporate:
                raise ValueError("embedding_dimension_required")
        elif (
            isinstance(dimension, bool)
            or not isinstance(dimension, int)
            or not 1 <= dimension <= (4096 if corporate else 65536)
        ):
            raise ValueError("invalid_embedding_dimension")
        self.model, self.corporate, self.retries = model, corporate, retries
        self.audit = audit
        (
            self.key,
            self.client,
            self.provider,
            self.endpoint,
            self.revision,
            self.dimension,
        ) = key, client, provider, endpoint, revision, dimension

    @timed_stage("embedding")
    async def embed(self, texts: list[str], task: str) -> EmbeddingBatch:
        if not self.key or not self.provider or not self.endpoint or not self.revision:
            raise ValueError("configuration_not_ready:embedding")
        if not texts or any(not t or len(t.encode("utf-8")) > 8000 for t in texts):
            raise ValueError("embedding_input_out_of_range")
        if task not in {*TASKS, "document"}:
            raise ValueError("unknown_embedding_task")
        inputs = (
            [f"Instruct: {TASKS[task]}\nQuery: {t}" for t in texts]
            if task in TASKS
            else texts
        )
        body = {"input": inputs, "encoding_format": "float"}
        if not self.corporate:
            body.update(model=self.model, dimensions=self.dimension)
        headers = (
            {"X-CHJ-GWToken": self.key, "Content-Type": "application/json"}
            if self.corporate
            else {"Authorization": f"Bearer {self.key}"}
        )
        data, request_id = await request(
            self.client,
            self.endpoint,
            body,
            headers,
            self.retries,
            corporate=self.corporate,
            audit=self.audit,
            purpose=task,
            provider=self.provider,
        )
        rows = data.get("data", [])
        if len(rows) != len(texts) or sorted(r.get("index", -1) for r in rows) != list(
            range(len(texts))
        ):
            raise ValueError("invalid_embedding_rows")
        vectors = []
        dimension = self.dimension
        for row in sorted(rows, key=lambda r: r["index"]):
            vector = row["embedding"]
            if dimension is None:
                dimension = len(vector)
            if not 1 <= dimension <= (4096 if self.corporate else 65536):
                raise ValueError("invalid_embedding_dimension_or_value")
            if len(vector) != dimension or any(
                isinstance(x, bool)
                or not isinstance(x, (int, float))
                or not math.isfinite(x)
                for x in vector
            ):
                raise ValueError("invalid_embedding_dimension_or_value")
            norm = math.sqrt(sum(x * x for x in vector))
            if norm == 0 or not math.isfinite(norm):
                raise ValueError("invalid_embedding_zero_vector")
            vectors.append([float(x / norm) for x in vector])
        self.dimension = dimension
        return EmbeddingBatch(
            indexed_vectors=vectors,
            usage=usage_record(
                data, purpose=task, provider=self.provider, request_id=request_id
            ),
            provider_revision=self.revision,
        )


def embedding_configuration(deployment, *, allow_discovery=False):
    """Resolve explicit deployment and credential slot before reading secrets.

    The corporate HTTP exception is limited to the exact documented gateway.
    Public deployments always retain their independent credential slot.
    """
    provider = deployment.get("provider", "")
    endpoint = deployment.get("endpoint", "")
    corporate = provider == "corporate_apihub"
    model = deployment.get(
        "model", CORPORATE_EMBEDDING_MODEL if corporate else EMBEDDING_MODEL
    )
    parts = urlsplit(endpoint)
    if (
        not provider
        or not deployment.get("revision")
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or (
            corporate
            and (
                endpoint != CORPORATE_EMBEDDING_ENDPOINT
                or model != CORPORATE_EMBEDDING_MODEL
            )
        )
        or (not corporate and (parts.scheme != "https" or model != EMBEDDING_MODEL))
    ):
        raise ValueError("embedding_deployment_configuration_required")
    dimension = deployment.get("dimension", None if corporate else 2560)
    if dimension is None and not (corporate and allow_discovery):
        raise ValueError("embedding_dimension_required")
    if dimension is not None and (
        isinstance(dimension, bool)
        or not isinstance(dimension, int)
        or not 1 <= dimension <= (4096 if corporate else 65536)
    ):
        raise ValueError("invalid_embedding_dimension")
    return "corporate" if corporate else "embedding", {
        "provider": provider,
        "endpoint": endpoint,
        "revision": deployment["revision"],
        "dimension": dimension,
        "model": model,
        "corporate": corporate,
    }
