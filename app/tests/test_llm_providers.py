"""Offline tests for app/llm_providers.py — the DeepSeek/Anthropic abstraction
behind the AI evaluation step.

No network: httpx.post is monkeypatched, so these lock in the wire-format
handling (which is the part that actually breaks when a provider changes
something) without spending money or needing a key.

The two providers differ ONLY in wire format, and the failure modes worth
pinning are the provider-specific ones: a JSON-*string* payload instead of a
parsed object (OpenAI-style tool calls), a model that answers in plain content
instead of calling the tool, and error bodies that need translating into
something actionable (401 vs 402 vs a rejected model name).
"""
import json

import pytest

from app import llm_providers as lp

SCHEMA = {
    "name": "submit_evaluation",
    "description": "Submit a structured fit evaluation for this job posting.",
    "input_schema": {
        "type": "object",
        "properties": {
            "match_score": {"type": "integer"},
            "recommendation": {"type": "string", "enum": ["apply", "consider", "skip"]},
        },
        "required": ["match_score", "recommendation"],
    },
}
GOOD = {"match_score": 77, "recommendation": "consider"}


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code != 200:
            raise AssertionError("should not be called directly")


def _tool_call_response(args: dict | str, finish_reason="tool_calls"):
    raw = args if isinstance(args, str) else json.dumps(args)
    return FakeResponse(200, {
        "choices": [{
            "finish_reason": finish_reason,
            "message": {
                "tool_calls": [{
                    "type": "function",
                    "function": {"name": "submit_evaluation", "arguments": raw},
                }]
            },
        }]
    })


# --- schema translation ----------------------------------------------------
def test_tool_definition_wraps_anthropic_schema_openai_style():
    tool = lp._tool_definition(SCHEMA)
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "submit_evaluation"
    # The JSON Schema is passed through unchanged: one source of truth.
    assert tool["function"]["parameters"] is SCHEMA["input_schema"]


# --- DeepSeek wire format --------------------------------------------------
def test_deepseek_parses_tool_call_arguments_json_string(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(url=url, payload=json, headers=headers)
        return _tool_call_response(GOOD)

    monkeypatch.setattr(lp.httpx, "post", fake_post)
    provider = lp.DeepSeekProvider("sk-" + "x" * 40, model="deepseek-chat")
    result = provider.complete("sys", "user", SCHEMA, 512)

    assert result == GOOD
    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["headers"]["Authorization"].startswith("Bearer sk-")
    assert captured["payload"]["model"] == "deepseek-chat"
    assert captured["payload"]["max_tokens"] == 512
    assert captured["payload"]["temperature"] == 0  # scoring must not drift
    assert captured["payload"]["tool_choice"]["function"]["name"] == "submit_evaluation"


def test_deepseek_falls_back_to_plain_content_json(monkeypatch):
    # Some models answer with the JSON in content rather than a tool call.
    monkeypatch.setattr(lp.httpx, "post", lambda *a, **k: FakeResponse(200, {
        "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(GOOD)}}]
    }))
    assert lp.DeepSeekProvider("k" * 30).complete("s", "u", SCHEMA, 256) == GOOD


def test_deepseek_tolerates_fenced_json(monkeypatch):
    monkeypatch.setattr(lp.httpx, "post", lambda *a, **k: FakeResponse(200, {
        "choices": [{"message": {"content": "```json\n" + json.dumps(GOOD) + "\n```"}}]
    }))
    assert lp.DeepSeekProvider("k" * 30).complete("s", "u", SCHEMA, 256) == GOOD


def test_deepseek_returns_none_when_nothing_usable(monkeypatch):
    # This is what feeds ai_evaluate's retry-with-more-tokens path.
    monkeypatch.setattr(lp.httpx, "post", lambda *a, **k: FakeResponse(200, {
        "choices": [{"finish_reason": "length", "message": {"content": "Sure! Here is"}}]
    }))
    assert lp.DeepSeekProvider("k" * 30).complete("s", "u", SCHEMA, 16) is None


def test_deepseek_drops_forced_tool_choice_if_model_rejects_it(monkeypatch):
    seen = []

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.append(dict(json))
        if "tool_choice" in json:
            return FakeResponse(400, text='{"error":{"message":"tool_choice is not supported"}}')
        return _tool_call_response(GOOD)

    monkeypatch.setattr(lp.httpx, "post", fake_post)
    result = lp.DeepSeekProvider("k" * 30).complete("s", "u", SCHEMA, 256)

    assert result == GOOD
    assert "tool_choice" in seen[0] and "tool_choice" not in seen[1]


@pytest.mark.parametrize("status,body,fragment", [
    (401, "unauthorized", "rejected the API key"),
    (402, "insufficient balance", "insufficient balance"),
    (429, "slow down", "rate limit"),
    (400, '{"error":"model not found"}', "list-models"),
])
def test_deepseek_errors_are_actionable(monkeypatch, status, body, fragment):
    monkeypatch.setattr(lp.httpx, "post", lambda *a, **k: FakeResponse(status, text=body))
    with pytest.raises(lp.ProviderError) as exc:
        lp.DeepSeekProvider("k" * 30, model="nope").complete("s", "u", SCHEMA, 64)
    assert fragment in str(exc.value)


# --- provider selection ----------------------------------------------------
def _clear(monkeypatch):
    for var in ("DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "LLM_PROVIDER"):
        monkeypatch.delenv(var, raising=False)


def test_autodetects_by_key_and_prefers_deepseek(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-" + "d" * 40)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-" + "a" * 40)
    assert lp.build_provider().name == "deepseek"


def test_explicit_choice_beats_autodetect(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-" + "d" * 40)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-" + "a" * 40)
    assert lp.build_provider("anthropic").name == "anthropic"
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    assert lp.build_provider().name == "anthropic"


def test_placeholder_keys_are_treated_as_unset(monkeypatch):
    # .env.example ships ANTHROPIC_API_KEY=skr. Non-empty, so a naive truthiness
    # check selects Anthropic and the run dies later with a 401 instead of here.
    _clear(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "skr")
    with pytest.raises(lp.ProviderError) as exc:
        lp.build_provider()
    assert "placeholder" in str(exc.value)
    assert "skr" not in str(exc.value)  # never echo a key back


def test_selecting_a_provider_without_its_key_explains_what_to_set(monkeypatch):
    _clear(monkeypatch)
    with pytest.raises(lp.ProviderError) as exc:
        lp.build_provider("deepseek")
    assert "DEEPSEEK_API_KEY" in str(exc.value)


def test_unknown_provider_name_is_rejected(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "gpt-5")
    with pytest.raises(lp.ProviderError) as exc:
        lp.build_provider()
    assert "Unknown LLM_PROVIDER" in str(exc.value)


# --- thinking mode ---------------------------------------------------------
# Diagnosed against live deepseek-flash on 2026-09-21. With thinking ON, a
# forced tool_choice is rejected with HTTP 400 "Thinking mode does not support
# this tool_choice"; the retry then drops the force, and the model spends its
# entire output budget on reasoning_content before writing the tool call. At
# max_tokens=1536 that killed 5 of 68 postings on a real run — all of them the
# longest adverts. Thinking is therefore OFF by default.
def test_thinking_is_disabled_by_default(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(payload=json)
        return _tool_call_response(GOOD)

    monkeypatch.setattr(lp.httpx, "post", fake_post)
    monkeypatch.delenv("DEEPSEEK_THINKING", raising=False)
    lp.DeepSeekProvider("k" * 30).complete("s", "u", SCHEMA, 512)

    assert captured["payload"]["thinking"] == {"type": "disabled"}
    # ...and the forced tool call is still sent, which is only legal BECAUSE
    # thinking is off. That combination is what makes the output reliable.
    assert captured["payload"]["tool_choice"]["function"]["name"] == "submit_evaluation"


def test_thinking_can_be_turned_back_on(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(payload=json)
        return _tool_call_response(GOOD)

    monkeypatch.setattr(lp.httpx, "post", fake_post)
    monkeypatch.setenv("DEEPSEEK_THINKING", "true")
    lp.DeepSeekProvider("k" * 30).complete("s", "u", SCHEMA, 512)
    assert "thinking" not in captured["payload"]

    # An explicit argument beats the environment.
    monkeypatch.setenv("DEEPSEEK_THINKING", "true")
    lp.DeepSeekProvider("k" * 30, thinking=False).complete("s", "u", SCHEMA, 512)
    assert captured["payload"]["thinking"] == {"type": "disabled"}


@pytest.mark.parametrize("raw,expected", [
    ("1", True), ("true", True), ("YES", True), ("on", True),
    ("0", False), ("false", False), ("off", False), ("nonsense", False),
])
def test_env_flag_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("DSH_TEST_FLAG", raw)
    assert lp._env_flag("DSH_TEST_FLAG") is expected


def test_env_flag_missing_uses_the_default(monkeypatch):
    """Absent must mean the default, not False — otherwise a missing variable
    would silently turn thinking back on."""
    monkeypatch.delenv("DSH_TEST_FLAG", raising=False)
    assert lp._env_flag("DSH_TEST_FLAG", default=True) is True
    monkeypatch.setenv("DSH_TEST_FLAG", "  ")
    assert lp._env_flag("DSH_TEST_FLAG", default=True) is True


def test_truncated_tool_call_reports_why_rather_than_a_bare_none(monkeypatch):
    """A tool call cut off mid-JSON used to surface as 'returned no
    submit_evaluation object', which says nothing about the cause. The
    diagnosis took an afternoon; the message should not."""
    monkeypatch.setattr(lp.httpx, "post", lambda *a, **k: FakeResponse(200, {
        "choices": [{"finish_reason": "length", "message": {
            "tool_calls": [{"function": {"name": "submit_evaluation",
                                         "arguments": '{"match_score": 77, "recomm'}}],
            "reasoning_content": "x" * 5000,
        }}]
    }))
    provider = lp.DeepSeekProvider("k" * 30)
    assert provider.complete("s", "u", SCHEMA, 512) is None
    assert provider.last_failure
    assert "max_tokens" in provider.last_failure or "reasoning" in provider.last_failure
    assert "length" in provider.last_failure


def test_a_thinking_model_400_retries_without_the_force(monkeypatch):
    """The real live failure: forcing tool_choice against a thinking model is
    a 400. The retry must drop the force rather than fail the job."""
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(dict(json))
        if "tool_choice" in json:
            return FakeResponse(400, {"error": {"message": "Thinking mode does not support this tool_choice"}},
                                text="Thinking mode does not support this tool_choice")
        return _tool_call_response(GOOD)

    monkeypatch.setattr(lp.httpx, "post", fake_post)
    provider = lp.DeepSeekProvider("k" * 30)
    assert provider.complete("s", "u", SCHEMA, 512) == GOOD
    assert len(calls) == 2
    assert "tool_choice" in calls[0] and "tool_choice" not in calls[1]
    assert provider._force_tool_choice is False


# --- LocalOpenAIProvider (llama-server / Qwen3-Coder) ----------------------
# The drafting half of the CV tailoring pipeline. Verified live against
# llama-server b10909 on 2026-09: a forced tool_choice IS honoured, so these
# tests pin the wire shape rather than a retry path.

def test_local_provider_posts_to_the_openai_compatible_path(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(url=url, payload=json, headers=headers)
        return _tool_call_response(GOOD)

    monkeypatch.setattr(lp.httpx, "post", fake_post)
    provider = lp.LocalOpenAIProvider(model="qwen3-coder-30b-a3b",
                                      base_url="http://127.0.0.1:8080")
    assert provider.complete("sys", "user", SCHEMA, 900) == GOOD

    # /v1 is part of the base path — llama-server serves the OpenAI-compatible
    # API there, not at the root.
    assert captured["url"] == "http://127.0.0.1:8080/v1/chat/completions"
    assert captured["payload"]["model"] == "qwen3-coder-30b-a3b"
    # An Authorization header is still sent: llama-server ignores it, but the
    # endpoint expects it present and omitting it breaks some clients.
    assert captured["headers"]["Authorization"] == "Bearer local-no-auth"
    assert captured["payload"]["tool_choice"]["function"]["name"] == "submit_evaluation"


def test_local_provider_never_sends_a_thinking_key(monkeypatch):
    """`thinking` is a DeepSeek-only body key. Qwen3-Coder is an instruct model
    and llama-server reports no reasoning support; sending the key risks a 400
    on an unknown field for no benefit."""
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(payload=json)
        return _tool_call_response(GOOD)

    monkeypatch.setattr(lp.httpx, "post", fake_post)
    lp.LocalOpenAIProvider().complete("s", "u", SCHEMA, 200)
    assert "thinking" not in captured["payload"]


def test_local_provider_reports_a_context_overflow_as_such(monkeypatch):
    """The local model's real constraint is its 32k slot window, not its
    willingness to call a tool. That must be named, not reported as a generic
    400 — the fix is a different model or a shorter prompt, not a retry."""
    monkeypatch.setattr(lp.httpx, "post", lambda *a, **k: FakeResponse(
        400, {"error": "exceed_context_size_error"},
        text="exceed_context_size_error (41123 tokens) exceeds the available context size (32768 tokens)"))
    provider = lp.LocalOpenAIProvider()
    with pytest.raises(lp.ProviderError) as exc:
        provider.complete("s", "u", SCHEMA, 200)
    assert "too long" in str(exc.value)
    assert "32768" in str(exc.value)


def test_local_provider_reports_a_missing_model_as_such(monkeypatch):
    monkeypatch.setattr(lp.httpx, "post", lambda *a, **k: FakeResponse(
        404, {"error": "model not found"}, text="model not found"))
    provider = lp.LocalOpenAIProvider(model="wrong-name")
    with pytest.raises(lp.ProviderError) as exc:
        provider.complete("s", "u", SCHEMA, 200)
    assert "wrong-name" in str(exc.value)
    assert "LOCAL_LLM_MODEL" in str(exc.value)


def test_local_provider_builds_from_the_local_choice():
    provider = lp.build_provider("local")
    assert provider.name == "local"
    assert provider.model == lp.LOCAL_LLM_DEFAULT_MODEL
    assert provider.base_url == lp.LOCAL_LLM_BASE_URL


def test_check_local_available_names_the_fix_when_nothing_is_listening(monkeypatch):
    """A tailoring run against a stopped server must say 'start llama-server',
    not raise a connection error from inside a subprocess the UI is awaiting."""
    def refuse(url, timeout=None):
        raise lp.httpx.ConnectError("connection refused")

    monkeypatch.setattr(lp.httpx, "get", refuse)
    problem = lp.check_local_available("http://127.0.0.1:8080")
    assert problem is not None
    assert "bootstrap_local_ai.sh" in problem
    assert "CV_DRAFT_PROVIDER=deepseek" in problem


def test_check_local_available_is_silent_when_the_server_answers(monkeypatch):
    monkeypatch.setattr(lp.httpx, "get",
                        lambda url, timeout=None: FakeResponse(200, {"data": [{"id": "qwen3-coder-30b-a3b"}]}))
    assert lp.check_local_available("http://127.0.0.1:8080") is None
