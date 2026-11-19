"""Gateway tool calling: native, streamed, json fallback and text-encoded calls."""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.llm import tool_calling as tc
from app.llm.gateway import ChatMessage, EndpointConfig, ModelClient

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "linux_top_processes",
            "description": "Top processes",
            "parameters": {
                "type": "object",
                "properties": {"resource_id": {"type": "string"}},
                "required": ["resource_id"],
            },
        },
    }
]
MSGS = [ChatMessage("system", "You diagnose."), ChatMessage("user", "Why is CPU high?")]


def _ep(**kw: Any) -> EndpointConfig:
    return EndpointConfig(
        **{"provider": "vllm", "base_url": f"http://llm-{id(kw)}:8000", "model": "m", **kw}
    )


def _reply(message: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"role": "assistant", **message}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


async def test_native_tool_calls() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        return _reply(
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "linux_top_processes",
                            "arguments": '{"resource_id": "app-01"}',
                        },
                    }
                ],
            }
        )

    client = ModelClient(_ep(tool_calling="native"), transport=httpx.MockTransport(handler))
    turn = await tc.chat_tools(client, MSGS, TOOLS, [])
    assert seen[0]["tools"] == TOOLS and seen[0]["tool_choice"] == "auto"
    assert turn.mode == "native" and not turn.final
    assert [(c.name, c.arguments) for c in turn.calls] == [
        ("linux_top_processes", {"resource_id": "app-01"})
    ]


async def test_streamed_tool_call_deltas_are_joined() -> None:
    chunks = [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c9", "function": {"name": "linux_top", "arguments": '{"resou'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "_processes", "arguments": 'rce_id": "x"}'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 3}},
    ]  # fmt: skip
    stream = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=stream, headers={"content-type": "text/event-stream"})

    client = ModelClient(_ep(tool_calling="native"), transport=httpx.MockTransport(handler))
    turn = await tc.chat_tools(client, MSGS, TOOLS, [])
    assert turn.calls[0].id == "c9"
    assert turn.calls[0].name == "linux_top_processes"
    assert turn.calls[0].arguments == {"resource_id": "x"}
    assert turn.finish_reason == "tool_calls"


async def test_auto_falls_back_to_json_when_tools_are_rejected() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "tools" in body:
            return httpx.Response(400, json={"error": {"message": "tools not supported"}})
        return _reply(
            {"content": '{"tool": "linux_top_processes", "arguments": {"resource_id": "a"}}'}
        )

    ep = _ep(tool_calling="auto", max_retries=0)
    client = ModelClient(ep, transport=httpx.MockTransport(handler))
    turn = await tc.chat_tools(client, MSGS, TOOLS, [])
    assert turn.mode == "json" and turn.calls[0].arguments == {"resource_id": "a"}
    system = bodies[-1]["messages"][0]["content"]
    assert "Reply with exactly ONE JSON object" in system and "linux_top_processes" in system
    # remembered: the next call goes straight to json mode
    await tc.chat_tools(client, MSGS, TOOLS, [])
    assert "tools" not in bodies[-1]
    assert tc.effective_mode(client) == "json"


async def test_text_encoded_calls_and_final_answers() -> None:
    parsed = tc.parse_text_action(
        'Let me check.<tool_call>{"name": "linux_top_processes", "arguments": {"resource_id": "a"}}</tool_call>'
    )
    assert [c.name for c in parsed.calls] == ["linux_top_processes"]
    assert tc.parse_text_action('{"final": "CPU is high because of worker.py"}').final
    assert tc.parse_text_action("I am done, nothing else to check.").final
    bad = tc.parse_text_action('{"tool": "x", "arguments": "not json"}')
    assert bad.calls[0].error == "arguments are not valid JSON"


async def test_native_history_uses_tool_messages() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return _reply({"content": "CPU is saturated by worker.py."})

    client = ModelClient(_ep(tool_calling="native"), transport=httpx.MockTransport(handler))
    history = [
        tc.ToolExchange(
            calls=[tc.ToolCall("c1", "linux_top_processes", {"resource_id": "a"})],
            results=["E1: python worker.py 97% CPU"],
        )
    ]
    turn = await tc.chat_tools(client, MSGS, TOOLS, history)
    roles = [m["role"] for m in bodies[0]["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    assert bodies[0]["messages"][2]["tool_calls"][0]["function"]["name"] == "linux_top_processes"
    assert bodies[0]["messages"][3]["tool_call_id"] == "c1"
    assert turn.final and turn.content.startswith("CPU is saturated")


async def test_json_mode_history_and_final() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return _reply({"content": '<think>enough</think>{"final": "done"}'})

    client = ModelClient(_ep(tool_calling="json"), transport=httpx.MockTransport(handler))
    history = [
        tc.ToolExchange([tc.ToolCall("c1", "linux_top_processes", {"resource_id": "a"})], ["ok"])
    ]
    turn = await tc.chat_tools(client, MSGS, TOOLS, history)
    assert turn.final and turn.mode == "json"
    msgs = bodies[0]["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[2]["role"] == "assistant" and '"tool": "linux_top_processes"' in msgs[2]["content"]
    assert msgs[3]["content"].startswith("Result of linux_top_processes")
    assert bodies[0]["response_format"] == {"type": "json_object"}
