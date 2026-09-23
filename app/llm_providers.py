"""LLM providers for the pipeline's model-calling steps.

Three providers, one interface:

  DeepSeekProvider    — DeepSeek's OpenAI-compatible API, over plain httpx.
  LocalOpenAIProvider — a local llama-server (Qwen3-Coder), same wire format.
  AnthropicProvider   — Claude, via the official anthropic SDK (the original).

Used by two callers: app/ai_evaluate.py (one call per posting, scoring it
against profile.yaml) and app/cv_tailor.py (two calls per posting — a local
draft, then a DeepSeek verification of that draft).

Every provider is asked to return the SAME structured object for a given
caller, so everything downstream — validation, the retry logic, the DB write,
the CSV — is provider-agnostic. Only the wire format differs:

  Anthropic: tools=[{name, description, input_schema}] + tool_choice, and the
    answer arrives as a `tool_use` content block.
  DeepSeek/OpenAI: tools=[{type: "function", function: {name, description,
    parameters}}] + tool_choice, and the answer arrives as
    choices[0].message.tool_calls[0].function.arguments — a JSON *string*.

Why httpx for DeepSeek instead of the openai SDK: the repo already depends on
httpx everywhere else (ats_clients, aggregator_clients, the diagnostic
scripts), and this is one POST to one endpoint. Adding the openai package to
get an identical HTTP call would be pure dependency weight.
"""
import json
import os
import re

import httpx

DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
# The model IDs DeepSeek's API actually serves are NOT the long-standing
# `deepseek-chat` alias any more: this key reports `deepseek-flash` and
# `deepseek-v4-pro` (checked with --list-models, 2026-09). `deepseek-chat`
# therefore 400s with "model not found", which is why the default below is the
# cheap/fast tier — scoring a posting against a 13 KB profile is not a
# reasoning-heavy task, and this runs once per candidate.
#
# Do not trust this default blindly on a new key: run
#     python -m app.ai_evaluate --list-models
# and override with DEEPSEEK_MODEL (or --model) to match what your account
# offers. The provider turns a rejected model name into an error that says so.
DEEPSEEK_DEFAULT_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
ANTHROPIC_DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")

# The local llama-server route. Same OpenAI-compatible wire format as DeepSeek,
# so it shares that implementation through OpenAICompatProvider below; only the
# base URL, the model id and the absence of an API key differ.
#
# The default port matches "Local LLMs/local-ai/llama-server.flags" (--port 8080,
# -a qwen3-coder-30b-a3b). llama-server ignores the Authorization header, but the
# OpenAI-compatible endpoint still expects one to be present, so a placeholder is
# sent rather than omitted.
LOCAL_LLM_BASE_URL = os.environ.get("LOCAL_LLM_BASE_URL", "http://127.0.0.1:8080")
LOCAL_LLM_DEFAULT_MODEL = os.environ.get("LOCAL_LLM_MODEL", "qwen3-coder-30b-a3b")

TIMEOUT = 120.0  # generous for a cloud call: single long generations, not chat turns

# The local model needs its own, much longer ceiling, and one budget is wrong for
# both providers. DeepSeek answers a scoring call in ~4s and is never near 120s;
# a local 30B writes at ~34 tokens/sec (measured), so a 4096-token CV draft is
# ~120s of DECODING alone before prompt processing — meaning the 120s ceiling
# aborted real drafts with a ReadTimeout. Set generously; a timeout that fires on
# a legitimate slow generation is worse than a long wait, because the work is
# lost and has to be paid for again.
LOCAL_LLM_TIMEOUT = float(os.environ.get("LOCAL_LLM_TIMEOUT", "600"))

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


class ProviderError(RuntimeError):
    """A provider call failed in a way the caller should surface verbatim."""


# Values that people leave in .env after copying the template. They are
# non-empty, so a naive `if os.environ.get(KEY)` treats them as configured and
# auto-detection then picks a provider that is guaranteed to 401 — which is a
# confusing way to find out. .env.example in this repo literally ships
# `ANTHROPIC_API_KEY=skr`, so this is not hypothetical.
_PLACEHOLDER_VALUES = {
    "skr", "sk-...", "sk-ant-...", "your-key-here", "changeme", "xxx", "todo",
}


def _key_problem(value: str | None) -> str | None:
    """None if the value looks usable, else 'missing' or 'placeholder'."""
    if not value or not value.strip():
        return "missing"
    stripped = value.strip()
    if stripped.lower() in _PLACEHOLDER_VALUES or len(stripped) < 20:
        return "placeholder"
    return None


def _require_key(env_name: str, value: str | None, provider: str) -> str:
    problem = _key_problem(value)
    if problem == "missing":
        raise ProviderError(f"{provider} was selected but {env_name} is not set.")
    if problem == "placeholder":
        raise ProviderError(
            f"{env_name} still holds a placeholder value ({len(value.strip())} characters), not a "
            f"real key. Put your actual {provider} key in .env, or unset it so the other provider "
            f"is used."
        )
    return value.strip()


def _tool_definition(schema: dict) -> dict:
    """Anthropic tool schema -> OpenAI-style function definition.

    The input_schema is passed through unchanged: both APIs use JSON Schema for
    the parameters, so there is one source of truth for the evaluation shape."""
    return {
        "type": "function",
        "function": {
            "name": schema["name"],
            "description": schema["description"],
            "parameters": schema["input_schema"],
        },
    }


def _env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean-ish environment variable. Absent or unparseable falls
    back to `default` rather than to False, so a typo cannot silently flip a
    feature off."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _loads_json_object(raw: str) -> dict | None:
    """Parse a model's JSON, tolerating ```json fences some models add even
    when a tool call was requested."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        try:
            parsed = json.loads(_FENCE_RE.sub("", raw))
        except (TypeError, ValueError):
            return None
    return parsed if isinstance(parsed, dict) else None


class DeepSeekProvider:
    """DeepSeek chat-completions with a forced function call.

    THINKING MODE IS OFF BY DEFAULT, and that is a correctness fix rather than
    a preference. Diagnosed 2026-09-21 against live deepseek-flash:

      * with thinking ON, a forced `tool_choice` is rejected outright —
        HTTP 400 "Thinking mode does not support this tool_choice";
      * the code then retried WITHOUT the force, which is what it does for
        models that dislike forcing. That "worked" but the model spent its
        whole output budget on `reasoning_content` (4,800-6,400 characters per
        job) before writing any tool call. At max_tokens=1536 the reasoning
        could consume everything, so the tool call never arrived and the job
        failed with "returned no submit_evaluation object" — 5 of 68 postings
        on a real run, all of them the longest, most relevant adverts.
      * with thinking OFF, the forced tool call is accepted and honoured, the
        response is ~600 completion tokens and 3.7s instead of ~1,700 and 9.3s.

    So turning it off fixes the failures, makes the run ~2.5x faster and ~3x
    cheaper, and gives temperature=0 something to actually mean — a reasoning
    pass reintroduces run-to-run variance into a step whose whole job is to
    score the same posting the same way twice.

    Set DEEPSEEK_THINKING=true to turn it back on for deeper reasoning. Raise
    max_tokens with it (the reasoning is not free), and expect the 400-then-
    retry path below to be taken on every call.
    """

    name = "deepseek"

    def __init__(self, api_key: str, model: str = DEEPSEEK_DEFAULT_MODEL,
                 base_url: str = DEEPSEEK_BASE_URL, thinking: bool | None = None):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.thinking = _env_flag("DEEPSEEK_THINKING", default=False) if thinking is None else thinking
        self._force_tool_choice = True  # flipped off if the model rejects forcing
        # Filled in when a response could not be used, so the caller's error
        # message can say WHY (truncation, reasoning eating the budget) instead
        # of the bare "returned no submit_evaluation object" that took an
        # afternoon to trace back to thinking mode.
        self.last_failure: str | None = None

    def list_models(self) -> list[str]:
        resp = httpx.get(f"{self.base_url}/models",
                         headers={"Authorization": f"Bearer {self.api_key}"},
                         timeout=30.0)
        resp.raise_for_status()
        data = resp.json()
        return sorted(m.get("id", "") for m in data.get("data", []) if m.get("id"))

    def _extra_payload(self) -> dict:
        """DeepSeek-specific body keys, kept out of the shared request shape so
        the local provider is not sent a `thinking` object it has never heard
        of (llama-server would ignore it, but a 400 on an unknown field is a
        documented llama.cpp behaviour for some builds)."""
        return {"thinking": {"type": "disabled"}} if not self.thinking else {}

    def _post(self, payload: dict) -> httpx.Response:
        return openai_compat_post(self.base_url, self.api_key, payload)

    def _truncation_hint(self, choice: dict, tool_name: str) -> str:
        finish = choice.get("finish_reason")
        message = choice.get("message") or {}
        reasoning = len(message.get("reasoning_content") or "")
        parts = [f"DeepSeek returned no usable {tool_name} object (finish_reason={finish!r})"]
        if reasoning:
            parts.append(f"{reasoning} chars of reasoning_content were produced first, which is "
                         f"the usual cause: the reasoning consumes max_tokens before the tool call "
                         f"is written. Thinking mode is off by default — check DEEPSEEK_THINKING.")
        if finish == "length":
            parts.append("the response was cut off at max_tokens")
        return ". ".join(parts)

    def _describe_error(self, resp: httpx.Response) -> str:
        body = (resp.text or "")[:300]
        if resp.status_code == 401:
            return ("DeepSeek rejected the API key (401). Check DEEPSEEK_API_KEY in .env. "
                    f"Body: {body}")
        if resp.status_code == 402:
            return (f"DeepSeek says the account has insufficient balance (402) — top up at "
                    f"platform.deepseek.com. Body: {body}")
        if resp.status_code == 429:
            return f"DeepSeek rate limit (429). Body: {body}"
        if resp.status_code == 400 and "model" in body.lower():
            return (f"DeepSeek rejected model {self.model!r} (400) — run "
                    f"`python -m app.ai_evaluate --list-models` to see what your key offers. "
                    f"Body: {body}")
        return f"DeepSeek HTTP {resp.status_code}: {body}"

    def complete(self, system: str, user: str, schema: dict, max_tokens: int) -> dict | None:
        return openai_compat_complete(self, system, user, schema, max_tokens)


def openai_compat_post(base_url: str, api_key: str, payload: dict,
                       timeout: float | None = None) -> httpx.Response:
    """One POST to an OpenAI-compatible /chat/completions endpoint.

    Shared by DeepSeek and the local llama-server, because the request shape is
    identical and duplicating it is how the two drift apart. The `timeout` differs
    because the two are an order of magnitude apart in speed — see LOCAL_LLM_TIMEOUT."""
    try:
        return httpx.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout if timeout is not None else TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise ProviderError(
            f"Request to {base_url} failed: {type(exc).__name__}: {exc}"
            + (f" (it did not finish within {timeout:.0f}s)" if isinstance(exc, httpx.TimeoutException)
               and timeout is not None else ""))


def openai_compat_complete(provider, system: str, user: str, schema: dict,
                           max_tokens: int) -> dict | None:
    """The forced-function-call protocol, implemented once for every
    OpenAI-compatible provider.

    `provider` must supply: model, base_url, api_key (may be a placeholder),
    `_extra_payload()`, `_post()`, `_describe_error()` and `_truncation_hint()`,
    plus a `_force_tool_choice` attribute and a `last_failure` slot.
    """
    payload = {
        "model": provider.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": 0,  # scoring/selection should not drift between identical runs
        "tools": [_tool_definition(schema)],
        **provider._extra_payload(),
    }
    if provider._force_tool_choice:
        payload["tool_choice"] = {
            "type": "function",
            "function": {"name": schema["name"]},
        }

    resp = provider._post(payload)

    # Some models/accounts reject a forced tool_choice. That is not worth
    # failing a whole run over: drop the force and let the model choose,
    # which in practice still returns the single tool it was offered.
    # Note this is the path a thinking model takes on EVERY call.
    if resp.status_code == 400 and "tool_choice" in resp.text and provider._force_tool_choice:
        provider._force_tool_choice = False
        payload.pop("tool_choice", None)
        resp = provider._post(payload)

    if resp.status_code != 200:
        raise ProviderError(provider._describe_error(resp))

    data = resp.json()
    try:
        choice = data["choices"][0]
    except (KeyError, IndexError, TypeError):
        raise ProviderError(f"Unexpected response shape from {provider.name}: {str(data)[:300]}")

    message = choice.get("message") or {}
    for call in message.get("tool_calls") or []:
        fn = (call.get("function") or {})
        if fn.get("name") == schema["name"]:
            parsed = _loads_json_object(fn.get("arguments"))
            if parsed is not None:
                return parsed
            # A tool call whose arguments are not valid JSON is almost
            # always truncation. Record why and return None rather than
            # raising: the caller retries once with a bigger max_tokens,
            # which is exactly the right response to truncation.
            provider.last_failure = provider._truncation_hint(choice, schema["name"])
            return None
    # Fallback: a model that answered with the JSON in plain content
    # instead of a tool call.
    parsed = _loads_json_object(message.get("content"))
    if parsed is not None:
        return parsed
    provider.last_failure = provider._truncation_hint(choice, schema["name"])
    return None


class LocalOpenAIProvider:
    """A local llama-server (default: Qwen3-Coder-30B-A3B on 127.0.0.1:8080).

    Used by app/cv_tailor.py as the drafting half of a two-model pipeline: it
    SELECTS facts from cv/master.yaml and rewrites them in the posting's
    vocabulary, then DeepSeek verifies that selection against the master. The
    local model is good at that mechanical half and free to run; the
    judgement-heavy verification is not handed to it.

    Verified against live llama-server b10909 (2026-09), because a
    "tool calling" claim from a local model is worth nothing unchecked:

      * a forced `tool_choice` IS honoured — finish_reason "tool_calls", valid
        JSON arguments, ~5.7s for a small schema. So the 400-then-retry path
        above is not expected here.
      * `--jinja` is what makes the tool-call template work; without it the
        server rejects the request rather than silently ignoring the tool.
      * context is the real constraint, not capability: the slot is 32,768
        tokens (see llama-server.flags -c 98304 --kv-unified-per-slot 32768).
        cv/master.yaml is ~40 KB ≈ 12k tokens, so the tailoring prompt fits
        with room for a normal posting but not for a batch of them.
      * no `thinking` key is sent: Qwen3-Coder is an instruct model and
        llama-server reports "supports_reasoning_effort": false.

    The API key is a placeholder by construction — llama-server does not
    authenticate (see the Local LLMs/README "apiKeyEnv" note), but the
    OpenAI-compatible endpoint still wants the header present.
    """

    name = "local"

    def __init__(self, model: str = LOCAL_LLM_DEFAULT_MODEL,
                 base_url: str = LOCAL_LLM_BASE_URL, api_key: str = "local-no-auth"):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._force_tool_choice = True
        self.last_failure: str | None = None

    def list_models(self) -> list[str]:
        resp = httpx.get(f"{self.base_url}/v1/models", timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        return sorted(m.get("id", "") for m in data.get("data", []) if m.get("id"))

    def _extra_payload(self) -> dict:
        return {}

    def _post(self, payload: dict) -> httpx.Response:
        return openai_compat_post(self.base_url + "/v1", self.api_key, payload,
                                  timeout=LOCAL_LLM_TIMEOUT)

    def _truncation_hint(self, choice: dict, tool_name: str) -> str:
        finish = choice.get("finish_reason")
        parts = [f"{self.model} returned no usable {tool_name} object "
                 f"(finish_reason={finish!r})"]
        if finish == "length":
            parts.append("the response hit max_tokens. The slot window is 32768 tokens and the "
                         "master CV alone is ~12k of them, so a long posting can leave little "
                         "room to write into — raise max_tokens, or lower CV_DRAFT_MAX_TOKENS "
                         "expectations by tailoring a shorter posting")
        else:
            parts.append("the model answered without calling the tool. Retrying with "
                         "tool_choice unforced usually recovers this")
        return ". ".join(parts)

    def _describe_error(self, resp: httpx.Response) -> str:
        body = (resp.text or "")[:300]
        if resp.status_code == 400 and "context" in body.lower():
            return (f"{self.model} rejected the prompt as too long for its slot window (400). "
                    f"The window is 32768 tokens (--kv-unified-per-slot in llama-server.flags); "
                    f"the master CV is ~12k of that. Body: {body}")
        if resp.status_code == 400 and "tool" in body.lower():
            return (f"{self.model} rejected the tool definition (400) — llama-server needs "
                    f"--jinja for tool calling. Body: {body}")
        if resp.status_code == 404:
            return (f"{self.model} is not loaded on {self.base_url} (404). Check the --alias in "
                    f"llama-server.flags matches LOCAL_LLM_MODEL. Body: {body}")
        return f"{self.name} HTTP {resp.status_code} from {self.base_url}: {body}"

    def complete(self, system: str, user: str, schema: dict, max_tokens: int) -> dict | None:
        return openai_compat_complete(self, system, user, schema, max_tokens)



class AnthropicProvider:
    """Claude with a forced tool call — the original implementation, unchanged
    in behaviour so existing setups keep working."""

    name = "anthropic"

    def __init__(self, api_key: str, model: str = ANTHROPIC_DEFAULT_MODEL):
        self.api_key = api_key
        self.model = model

    def complete(self, system: str, user: str, schema: dict, max_tokens: int) -> dict | None:
        try:
            import anthropic
        except ImportError:
            raise ProviderError("Missing dependency for the Anthropic provider: "
                                "pip install anthropic (or set LLM_PROVIDER=deepseek)")

        client = anthropic.Anthropic(api_key=self.api_key)
        resp = client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            tools=[schema],
            tool_choice={"type": "tool", "name": schema["name"]},
            messages=[{"role": "user", "content": user}],
        )
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == schema["name"]:
                return block.input if isinstance(block.input, dict) else None
        return None


def build_provider(explicit: str | None = None):
    """Pick a provider from (in order): the `explicit` argument, LLM_PROVIDER,
    then whichever API key is actually present.

    Auto-detecting on the key is deliberate: the alternative is a confusing
    "LLM_PROVIDER not set" error for someone who has already put a key in
    .env, and the two providers are not interchangeable enough to guess
    wrongly without saying so — the caller prints the choice either way."""
    choice = (explicit or os.environ.get("LLM_PROVIDER") or "").strip().lower()

    deepseek_key = os.environ.get("DEEPSEEK_API_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    deepseek_ok = _key_problem(deepseek_key) is None
    anthropic_ok = _key_problem(anthropic_key) is None

    if not choice:
        if deepseek_ok:
            choice = "deepseek"
        elif anthropic_ok:
            choice = "anthropic"
        else:
            # Say precisely what's wrong with each key rather than "no provider
            # configured", which sends people looking in the wrong place.
            detail = []
            for env_name, value in (("DEEPSEEK_API_KEY", deepseek_key),
                                    ("ANTHROPIC_API_KEY", anthropic_key)):
                problem = _key_problem(value)
                if problem:
                    detail.append(f"{env_name}: {problem}")
            raise ProviderError(
                "No usable LLM provider key. Set DEEPSEEK_API_KEY (recommended) in .env — "
                "see .env.example. Status: " + "; ".join(detail)
            )

    if choice == "deepseek":
        return DeepSeekProvider(_require_key("DEEPSEEK_API_KEY", deepseek_key, "DeepSeek"))
    if choice == "anthropic":
        return AnthropicProvider(_require_key("ANTHROPIC_API_KEY", anthropic_key, "Anthropic"))
    if choice == "local":
        return LocalOpenAIProvider()
    raise ProviderError(f"Unknown LLM_PROVIDER {choice!r} — expected 'deepseek', 'anthropic' "
                        f"or 'local'.")


def check_local_available(base_url: str = LOCAL_LLM_BASE_URL, timeout: float = 3.0) -> str | None:
    """None if a local llama-server answers, else a sentence explaining why not.

    A tailoring run against a stopped local server should fail with "start
    llama-server" rather than a connection-refused traceback from inside a
    subprocess the web UI is waiting on. Callers use this to fail fast and
    usefully, and the UI surfaces the returned sentence verbatim."""
    try:
        resp = httpx.get(f"{base_url.rstrip('/')}/v1/models", timeout=timeout)
    except httpx.HTTPError as exc:
        return (f"No local model server on {base_url} ({type(exc).__name__}). Start it with "
                f"`cd \"Local LLMs/local-ai\" && ./bootstrap_local_ai.sh`, or set "
                f"CV_DRAFT_PROVIDER=deepseek to draft in the cloud instead.")
    if resp.status_code != 200:
        return (f"The local model server at {base_url} answered HTTP {resp.status_code}. "
                f"Body: {(resp.text or '')[:200]}")
    return None
