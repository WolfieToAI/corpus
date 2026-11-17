"""The LLM integration against a fake OpenAI-compatible server (no network, no provider)."""

from __future__ import annotations

import json

import httpx
import pytest
from openai import AsyncOpenAI

from corpus.core.generation import ChatCompletionsGenerator, GenerationResult, RefusalError, Turn
from corpus.core.prompting import Source
from corpus.core.rewriting import LLMQueryRewriter
from corpus.evaluation.answer_eval import LLMJudge
from corpus.evaluation.dataset import Question

SOURCE = Source(1, "d", "manual.md", 1, 1, "A full charge takes about 4 hours.", 1.0)


def _chunk(content: str | None = None, finish: str | None = None, usage: bool = False) -> str:
    payload: dict[str, object] = {
        "id": "c1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "test-model",
        "choices": []
        if usage
        else [
            {"index": 0, "delta": {"content": content} if content else {}, "finish_reason": finish}
        ],
    }
    if usage:
        payload["usage"] = {"prompt_tokens": 120, "completion_tokens": 9, "total_tokens": 129}
    return f"data: {json.dumps(payload)}\n\n"


def _client(handler) -> AsyncOpenAI:  # type: ignore[no-untyped-def]
    return AsyncOpenAI(
        api_key="test",
        base_url="https://llm.test/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_generator_streams_tokens_and_records_usage() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        body = (
            _chunk("About 4 ")
            + _chunk("hours [1].")
            + _chunk(finish="stop")
            + _chunk(usage=True)
            + "data: [DONE]\n\n"
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    generator = ChatCompletionsGenerator(_client(handler), model="test-model", max_tokens=300)
    result = GenerationResult()
    history = [Turn("user", "Hi"), Turn("assistant", "Hello")]
    text = "".join(
        [t async for t in generator.stream_answer("How long?", [SOURCE], history, result)]
    )

    assert text == "About 4 hours [1]."
    assert (result.model, result.stop_reason) == ("test-model", "stop")
    assert (result.input_tokens, result.output_tokens) == (120, 9)
    # Request shape: system prompt first, then history, then the question with its sources.
    messages = seen["messages"]
    assert isinstance(messages, list)
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert "<source" in messages[-1]["content"] and seen["stream"] is True


async def test_content_filter_becomes_a_refusal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = _chunk(finish="content_filter") + "data: [DONE]\n\n"
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    generator = ChatCompletionsGenerator(_client(handler), model="m", max_tokens=10)
    with pytest.raises(RefusalError):
        _ = [t async for t in generator.stream_answer("q", [SOURCE], [], GenerationResult())]


def _completion(content: str, finish: str = "stop") -> dict[str, object]:
    return {
        "id": "x",
        "object": "chat.completion",
        "created": 0,
        "model": "m",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish,
            }
        ],
    }


async def test_rewriter_and_its_safe_fallback() -> None:
    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion('"What is the R7 warranty?"'))

    def filtered(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion("", finish="content_filter"))

    history = [Turn("user", "R5 warranty?"), Turn("assistant", "2 years [1].")]
    assert await LLMQueryRewriter(_client(ok), model="m").rewrite("And the R7?", history) == (
        "What is the R7 warranty?"
    )
    rewriter = LLMQueryRewriter(_client(filtered), model="m")
    assert await rewriter.rewrite("And the R7?", history) == "And the R7?"


async def test_judge_parses_json_and_rejects_malformed_output() -> None:
    question = Question("q1", "paraphrase", "How long?", ("4 hours",), "About 4 hours.")
    grade = {"faithful": True, "correct": True, "citations_valid": False, "explanation": "no [n]"}

    def good(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["response_format"] == {"type": "json_object"}
        return httpx.Response(200, json=_completion(json.dumps(grade)))

    def bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion("I think it is fine."))

    verdict = await LLMJudge(_client(good), "m").judge(question, [SOURCE], "About 4 hours.")
    assert verdict is not None and verdict.citations_valid is False
    assert await LLMJudge(_client(bad), "m").judge(question, [SOURCE], "x") is None
