from __future__ import annotations

import itertools
import json
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from app.llm.gateway import ChatMessage, EndpointConfig, ModelClient, ModelError, extract_json
from tests.fakes import FakeLLM


async def _nosleep(_: float) -> None:
    return None


def _ep(**kw: Any) -> EndpointConfig:
    base: dict[str, Any] = {
        "provider": "vllm",
        "base_url": "http://llm:8000",
        "model": "m",
        "max_retries": 2,
    }
    return EndpointConfig(**{**base, **kw})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('<think>{"no": 0}</think>\n{"a": 1}', {"a": 1}),
        ('Sure! Here it is: {"a": {"b": "}"}} hope that helps', {"a": {"b": "}"}}),
    ],
)
def test_extract_json(raw: str, expected: dict[str, Any]) -> None:
    assert extract_json(raw) == expected


def test_extract_json_failure() -> None:
    with pytest.raises(ValueError):
        extract_json("no json here")
    with pytest.raises(ValueError):
        extract_json('{"a": 1')


@pytest.mark.parametrize(
    ("url", "api_base", "native"),
    [
        ("http://h:8000", "http://h:8000/v1", "http://h:8000"),
        ("http://h:8000/v1/", "http://h:8000/v1", "http://h:8000"),
        ("http://h:11434/", "http://h:11434/v1", "http://h:11434"),
    ],
)
def test_url_normalisation(url: str, api_base: str, native: str) -> None:
    ep = _ep(base_url=url)
    assert ep.api_base == api_base
    assert ep.native_root == native


async def test_chat_retries_transient_errors() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "hi"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            },
        )

    client = ModelClient(_ep(), transport=httpx.MockTransport(handler), sleep=_nosleep)
    result = await client.chat([ChatMessage("user", "x")])
    assert result.content == "hi"
    assert result.usage.prompt_tokens == 3
    assert len(attempts) == 3


async def test_chat_does_not_retry_client_errors() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(400, json={"error": {"message": "bad model"}})

    client = ModelClient(_ep(), transport=httpx.MockTransport(handler), sleep=_nosleep)
    with pytest.raises(ModelError, match="bad model"):
        await client.chat([ChatMessage("user", "x")])
    assert len(attempts) == 1


async def test_chat_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = ModelClient(_ep(max_retries=1), transport=httpx.MockTransport(handler), sleep=_nosleep)
    with pytest.raises(ModelError, match="unreachable") as info:
        await client.chat([ChatMessage("user", "x")])
    assert info.value.retryable


async def test_api_key_header_and_payload() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    client = ModelClient(_ep(api_key="sk-local"), transport=httpx.MockTransport(handler))
    await client.chat([ChatMessage("user", "x")], temperature=0.2, max_tokens=50)
    assert seen["auth"] == "Bearer sk-local"
    assert seen["body"]["model"] == "m"
    assert seen["body"]["max_tokens"] == 50
    assert seen["body"]["stream"] is False


class Out(BaseModel):
    answer: int


async def test_structured_repairs_invalid_output() -> None:
    replies = iter(['{"answer": "not a number"}', '{"answer": 42}'])
    convo_lengths: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        convo_lengths.append(len(body["messages"]))
        assert body["response_format"] == {"type": "json_object"}
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": next(replies)}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    client = ModelClient(_ep(), transport=httpx.MockTransport(handler))
    result = await client.structured([ChatMessage("user", "x")], Out)
    assert result.value.answer == 42
    assert result.attempts == 2
    assert result.usage.prompt_tokens == 20 and result.usage.calls == 2
    assert convo_lengths[1] == convo_lengths[0] + 2  # bad answer + correction fed back


async def test_single_leading_system_message() -> None:
    """Templates like Mistral/Gemma reject system messages that aren't first."""
    sent: list[list[dict[str, str]]] = []
    replies = iter(['{"answer": "x"}', '{"answer": 7}'])

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["messages"])
        return httpx.Response(200, json={"choices": [{"message": {"content": next(replies)}}]})

    client = ModelClient(_ep(), transport=httpx.MockTransport(handler))
    await client.structured(
        [ChatMessage("system", "You are the supervisor."), ChatMessage("user", "q")], Out
    )
    for messages in sent:
        roles = [m["role"] for m in messages]
        assert roles.count("system") == 1 and roles[0] == "system"
        assert "You are the supervisor." in messages[0]["content"]
        assert "JSON Schema" in messages[0]["content"]
        # user/assistant strictly alternate after the system message
        assert all(a != b for a, b in itertools.pairwise(roles[1:]))
    assert [m["role"] for m in sent[1]] == ["system", "user", "assistant", "user"]


async def test_structured_gives_up() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "nope"}}]})

    client = ModelClient(_ep(), transport=httpx.MockTransport(handler))
    with pytest.raises(ModelError, match="valid structured output"):
        await client.structured([ChatMessage("user", "x")], Out, repair_attempts=1)


async def test_structured_json_schema_mode() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"answer": 1}'}}]})

    ep = _ep(structured_output="json_schema")
    client = ModelClient(ep, transport=httpx.MockTransport(handler))
    await client.structured([ChatMessage("user", "x")], Out)
    assert seen["response_format"]["type"] == "json_schema"
    assert seen["response_format"]["json_schema"]["name"] == "Out"


@pytest.mark.parametrize("provider", ["ollama", "vllm"])
async def test_health(provider: str) -> None:
    fake = FakeLLM(models=["m", "other"])
    client = ModelClient(_ep(provider=provider), transport=fake.transport())
    health = await client.health()
    assert health.ok and health.model_available is True
    assert health.reply == "OK"
    assert health.models == ["m", "other"]


async def test_health_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    health = await ModelClient(_ep(), transport=httpx.MockTransport(handler)).health()
    assert not health.ok
    assert "ConnectError" in (health.error or "")
