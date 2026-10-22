"""Tests for the Ollama provider with a fake transport and a local test server."""

import http.server
import json
import threading
import time
from collections.abc import Iterator

import pytest

from neurobrau_cli.conversation import Message
from neurobrau_cli.providers import ollama_provider
from neurobrau_cli.providers.base import ErrorKind, GenerationOptions, ProviderError
from neurobrau_cli.providers.ollama_provider import (
    OllamaHTTPError,
    OllamaProvider,
    OllamaProviderError,
)

from fakes import CLOUD_TAG, QWEN_TAG, FakeOllama

EMBED_TAG = {"name": "nomic-embed-text:latest", "capabilities": ["embedding"]}
OLD_TAG = {"name": "llama3.2:3b"}  # older Ollama: no capabilities in /api/tags
NAMED_CLOUD_TAG = {"name": "gemma4:31b-cloud", "capabilities": ["completion"]}


def make_provider(fake: FakeOllama) -> OllamaProvider:
    return OllamaProvider(base_url="http://127.0.0.1:11434", transport=fake)


def chat(provider: OllamaProvider, *texts: str, **options: object) -> object:
    messages = [Message("user" if i % 2 == 0 else "assistant", t) for i, t in enumerate(texts)]
    return provider.generate(messages, GenerationOptions(**{"model": "qwen3.5:4b", **options}))


# ----- configuration -----


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "http://127.0.0.1:11434"),
        ("", "http://127.0.0.1:11434"),
        ("127.0.0.1", "http://127.0.0.1:11434"),
        ("0.0.0.0:11500", "http://127.0.0.1:11500"),
        ("gpu-box:8080", "http://gpu-box:8080"),
        ("https://ollama.example.org/", "https://ollama.example.org:11434"),
        ("[::1]:11434", "http://[::1]:11434"),
    ],
)
def test_resolve_base_url(value: str | None, expected: str) -> None:
    env = {} if value is None else {"OLLAMA_HOST": value}

    assert ollama_provider.resolve_base_url(env) == expected


# ----- discovery and local/cloud filtering -----


def test_discovery_distinguishes_local_and_cloud_models() -> None:
    fake = FakeOllama(models=[QWEN_TAG, CLOUD_TAG, NAMED_CLOUD_TAG, EMBED_TAG, OLD_TAG])
    provider = make_provider(fake)

    models = {model.id: model for model in provider.discover()}

    assert not models["qwen3.5:4b"].remote
    assert models["qwen3.5:4b"].details == "4.2B Q4_K_M"
    assert models["glm-4.7:cloud"].remote  # remote_host / remote_model
    assert models["gemma4:31b-cloud"].remote  # recognized by its tag alone
    assert models["llama3.2:3b"].capabilities is None


def test_list_models_returns_only_local_chat_models_in_ollama_order() -> None:
    fake = FakeOllama(models=[QWEN_TAG, CLOUD_TAG, NAMED_CLOUD_TAG, EMBED_TAG, OLD_TAG])

    ids = [model.id for model in make_provider(fake).list_models()]

    assert ids == ["qwen3.5:4b", "llama3.2:3b"]


def test_discovery_uses_one_bounded_request_and_caches() -> None:
    fake = FakeOllama()
    provider = make_provider(fake)

    provider.list_models()
    provider.list_models()
    provider.default_model()

    assert fake.requests == [("GET", "/api/tags", None, ollama_provider.DISCOVERY_TIMEOUT)]
    provider.list_models(refresh=True)
    assert fake.paths == ["/api/tags", "/api/tags"]


def test_default_model_is_first_local_model() -> None:
    fake = FakeOllama(models=[CLOUD_TAG, QWEN_TAG])

    assert make_provider(fake).default_model() == "qwen3.5:4b"


def test_no_local_models() -> None:
    provider = make_provider(FakeOllama(models=[CLOUD_TAG]))

    with pytest.raises(OllamaProviderError, match="No local Ollama models"):
        provider.default_model()
    availability = provider.check_availability()
    assert not availability.available
    assert "ollama pull" in availability.message


def test_check_availability_reports_connection_error() -> None:
    def down(*args: object) -> object:
        raise OllamaProviderError("Could not connect to Ollama", ErrorKind.CONNECTION)

    availability = OllamaProvider(transport=down).check_availability()

    assert availability == ollama_provider.Availability(False, "Could not connect to Ollama")


@pytest.mark.parametrize(
    "data", [[], {"models": "nope"}, {"models": [42]}, {"models": [{"size": 1}]}]
)
def test_invalid_tags_response(data: object) -> None:
    provider = OllamaProvider(transport=lambda *args: data)

    with pytest.raises(OllamaProviderError, match="Invalid response from Ollama"):
        provider.list_models()


# ----- chat requests -----


def test_chat_request_sends_history_without_streaming_and_with_thinking_off() -> None:
    fake = FakeOllama()
    provider = make_provider(fake)

    result = chat(provider, "Hi", "Hello!", "How are you?", reasoning_effort="none")

    assert result.text == "local reply 1"
    method, path, payload, timeout = fake.requests[-1]
    assert (method, path, timeout) == ("POST", "/api/chat", ollama_provider.CHAT_TIMEOUT)
    assert payload == {
        "model": "qwen3.5:4b",
        "messages": [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
            {"role": "user", "content": "How are you?"},
        ],
        "stream": False,
        "think": False,
    }


@pytest.mark.parametrize(("effort", "think"), [(None, False), ("none", False), ("low", True)])
def test_think_follows_reasoning_effort(effort: str | None, think: bool) -> None:
    fake = FakeOllama()

    chat(make_provider(fake), "Hi", reasoning_effort=effort)

    assert fake.chat_payloads[0]["think"] is think


def test_think_is_omitted_for_models_without_thinking() -> None:
    tag = {**QWEN_TAG, "capabilities": ["completion"]}
    fake = FakeOllama(models=[tag])

    chat(make_provider(fake), "Hi", reasoning_effort="high")

    assert "think" not in fake.chat_payloads[0]


def test_num_predict_is_sent_for_max_output_tokens() -> None:
    fake = FakeOllama()

    chat(make_provider(fake), "Hi", max_output_tokens=64)

    assert fake.chat_payloads[0]["options"] == {"num_predict": 64}


def test_capabilities_come_from_show_only_when_tags_lack_them() -> None:
    fake = FakeOllama(models=[OLD_TAG], show={"llama3.2:3b": ["completion", "thinking"]})
    provider = make_provider(fake)

    provider.generate([Message("user", "a")], GenerationOptions("llama3.2:3b"))
    provider.generate([Message("user", "b")], GenerationOptions("llama3.2:3b"))

    assert fake.paths == ["/api/tags", "/api/show", "/api/chat", "/api/chat"]
    assert fake.chat_payloads[0]["think"] is False


def test_tags_capabilities_avoid_show_requests() -> None:
    fake = FakeOllama()

    chat(make_provider(fake), "Hi")

    assert "/api/show" not in fake.paths


@pytest.mark.parametrize("model", ["glm-4.7:cloud", "gemma4:31b-cloud"])
def test_cloud_models_are_refused_without_contacting_them(model: str) -> None:
    fake = FakeOllama(models=[QWEN_TAG, CLOUD_TAG, NAMED_CLOUD_TAG])

    with pytest.raises(OllamaProviderError, match="cloud model"):
        make_provider(fake).generate([Message("user", "Hi")], GenerationOptions(model))

    assert fake.paths == ["/api/tags"]  # no /api/show, no /api/chat


def test_missing_model_is_never_downloaded() -> None:
    fake = FakeOllama()

    with pytest.raises(OllamaProviderError, match="not installed locally.*ollama pull mistral"):
        make_provider(fake).generate([Message("user", "Hi")], GenerationOptions("mistral:7b"))

    assert fake.paths == ["/api/tags", "/api/tags"]  # one refresh, then give up
    assert all("pull" not in path for path in fake.paths)


def test_model_installed_after_discovery_is_found_by_refresh() -> None:
    fake = FakeOllama(models=[QWEN_TAG])
    provider = make_provider(fake)
    provider.list_models()
    fake.models = [QWEN_TAG, {**QWEN_TAG, "name": "new:1b", "model": "new:1b"}]

    provider.generate([Message("user", "Hi")], GenerationOptions("new:1b"))

    assert fake.chat_payloads[0]["model"] == "new:1b"


def test_chat_404_explains_how_to_install() -> None:
    fake = FakeOllama(replies=[OllamaHTTPError(404, "model 'qwen3.5:4b' not found")])

    with pytest.raises(OllamaProviderError, match="was not found.*ollama pull qwen3.5:4b"):
        chat(make_provider(fake), "Hi")


def test_other_http_errors_pass_through() -> None:
    fake = FakeOllama(replies=[OllamaHTTPError(500, "out of memory")])

    with pytest.raises(OllamaProviderError, match=r"\(HTTP 500\): out of memory") as exc_info:
        chat(make_provider(fake), "Hi")

    assert exc_info.value.kind is ErrorKind.API


# ----- response parsing -----


def test_usage_comes_from_eval_counts() -> None:
    fake = FakeOllama(replies=[{
        "message": {"role": "assistant", "content": "OK"},
        "prompt_eval_count": 17,
        "eval_count": 2,
    }])

    result = chat(make_provider(fake), "Hi")

    assert result.usage == ollama_provider.TokenUsage(17, 2, 19)


def test_partial_or_missing_usage() -> None:
    assert ollama_provider.parse_chat_response(
        {"message": {"content": "x"}, "eval_count": 3}
    ).usage == ollama_provider.TokenUsage(None, 3, None)
    assert ollama_provider.parse_chat_response({"message": {"content": "x"}}).usage is None


def test_reasoning_is_kept_separate_from_answer() -> None:
    result = ollama_provider.parse_chat_response(
        {"message": {"content": "5", "thinking": "2 plus 3 is 5."}}
    )

    assert (result.text, result.reasoning) == ("5", "2 plus 3 is 5.")


def test_inline_think_block_is_split_off() -> None:
    result = ollama_provider.parse_chat_response(
        {"message": {"content": "<think>\nLet me add.\n</think>\n\nThe answer is 5."}}
    )

    assert (result.text, result.reasoning) == ("The answer is 5.", "Let me add.")


@pytest.mark.parametrize(
    "data", [[], {}, {"message": "hi"}, {"message": {"role": "assistant"}}, {"message": {"content": 5}}]
)
def test_invalid_chat_response(data: object) -> None:
    with pytest.raises(OllamaProviderError, match="Invalid response from Ollama"):
        ollama_provider.parse_chat_response(data)


# ----- real HTTP transport against a local test server -----


class Handler(http.server.BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, bytes, float]] = {}

    def do_GET(self) -> None:
        self.respond()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        Handler.last_body = json.loads(self.rfile.read(length) or b"null")
        self.respond()

    def respond(self) -> None:
        status, body, delay = self.routes[self.path]
        time.sleep(delay)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def server() -> Iterator[str]:
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_transport_sends_json_and_parses_reply(server: str) -> None:
    Handler.routes = {"/api/chat": (200, b'{"message": {"content": "hi"}}', 0)}

    data = ollama_provider.urllib_transport("POST", f"{server}/api/chat", {"a": 1}, 2)

    assert data == {"message": {"content": "hi"}}
    assert Handler.last_body == {"a": 1}


def test_transport_reports_ollama_error_message(server: str) -> None:
    Handler.routes = {"/api/chat": (404, b'{"error": "model \'x\' not found"}', 0)}

    with pytest.raises(OllamaHTTPError) as exc_info:
        ollama_provider.urllib_transport("POST", f"{server}/api/chat", {}, 2)

    assert exc_info.value.status == 404
    assert exc_info.value.detail == "model 'x' not found"


def test_transport_rejects_invalid_json(server: str) -> None:
    Handler.routes = {"/api/tags": (200, b"<html>not json</html>", 0)}

    with pytest.raises(OllamaProviderError, match="not valid JSON"):
        ollama_provider.urllib_transport("GET", f"{server}/api/tags", None, 2)


def test_transport_timeout(server: str) -> None:
    Handler.routes = {"/api/tags": (200, b"{}", 0.6)}

    with pytest.raises(ProviderError) as exc_info:
        ollama_provider.urllib_transport("GET", f"{server}/api/tags", None, 0.2)

    assert exc_info.value.kind is ErrorKind.TIMEOUT


def test_transport_connection_refused() -> None:
    with pytest.raises(ProviderError, match="Is the Ollama service running") as exc_info:
        ollama_provider.urllib_transport("GET", "http://127.0.0.1:1/api/tags", None, 1)

    assert exc_info.value.kind is ErrorKind.CONNECTION


def test_transport_ignores_proxies_for_local_server(
    server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.delenv("no_proxy", raising=False)
    Handler.routes = {"/api/tags": (200, b'{"models": []}', 0)}

    assert ollama_provider.urllib_transport("GET", f"{server}/api/tags", None, 2) == {"models": []}


# ----- Task 007: thinking metadata, limits, answer style -----

ON_OFF_SHOW = {"capabilities": ["completion", "thinking"],
               "thinking": {"values": [False, True], "default": True}}
LEVEL_SHOW = {"capabilities": ["completion", "thinking"],
              "thinking": {"values": ["low", "medium", "high"], "default": "medium"}}
GPT_OSS_TAG = {"name": "gpt-oss:20b", "capabilities": ["completion", "thinking"],
               "details": {"context_length": 131072}}


def provider_with_show(show: dict, models: list[dict] | None = None) -> tuple[OllamaProvider, FakeOllama]:
    fake = FakeOllama(models=models)
    original = fake.__call__

    def transport(method: str, url: str, payload: dict | None, timeout: float) -> object:
        if url.endswith("/api/show"):
            fake.requests.append((method, "/api/show", payload, timeout))
            return show
        return original(method, url, payload, timeout)

    return OllamaProvider(base_url="http://127.0.0.1:11434", transport=transport), fake


def test_on_off_thinking_from_show_metadata() -> None:
    provider, fake = provider_with_show(ON_OFF_SHOW)

    support = provider.reasoning_support("qwen3.5:4b")
    provider.reasoning_support("qwen3.5:4b")

    assert support == ollama_provider.ReasoningSupport(("off", "on"), True, "off", False)
    assert fake.paths.count("/api/show") == 1  # cached


def test_named_thinking_levels() -> None:
    provider, fake = provider_with_show(LEVEL_SHOW, models=[GPT_OSS_TAG])

    support = provider.reasoning_support("gpt-oss:20b")
    chat_result = provider.generate(
        [Message("user", "Hi")], GenerationOptions("gpt-oss:20b", "high", reasoning_explicit=True)
    )

    assert support.values == ("low", "medium", "high")
    assert (support.default, support.allows_default) == (None, True)
    assert fake.chat_payloads[0]["think"] == "high"
    assert chat_result.text == "local reply 1"


def test_unsupported_level_is_an_error_when_explicit() -> None:
    provider, fake = provider_with_show(LEVEL_SHOW, models=[GPT_OSS_TAG])

    with pytest.raises(OllamaProviderError, match="does not support thinking level 'xhigh'"):
        provider.generate([Message("user", "Hi")],
                          GenerationOptions("gpt-oss:20b", "xhigh", reasoning_explicit=True))

    assert fake.chat_payloads == []


def test_thinking_capability_without_metadata_gives_on_off() -> None:
    provider, _ = provider_with_show({"capabilities": ["completion", "thinking"]})

    assert provider.reasoning_support("qwen3.5:4b").values == ("off", "on")


def test_model_without_thinking_has_no_reasoning() -> None:
    tag = {**QWEN_TAG, "capabilities": ["completion"]}
    provider, _ = provider_with_show({"capabilities": ["completion"]}, models=[tag])

    support = provider.reasoning_support("qwen3.5:4b")

    assert (support.known, support.values) == (True, ())


def test_explicit_thinking_on_model_without_thinking_is_an_error() -> None:
    tag = {**QWEN_TAG, "capabilities": ["completion"]}
    fake = FakeOllama(models=[tag])

    with pytest.raises(OllamaProviderError, match="does not support thinking"):
        make_provider(fake).generate(
            [Message("user", "Hi")], GenerationOptions("qwen3.5:4b", "on", reasoning_explicit=True)
        )
    assert fake.chat_payloads == []


def test_on_maps_to_think_true() -> None:
    fake = FakeOllama()

    chat(make_provider(fake), "Hi", reasoning_effort="on", reasoning_explicit=True)

    assert fake.chat_payloads[0]["think"] is True
    assert "/api/show" not in fake.paths  # off/on need no metadata request


def test_reasoning_support_never_queries_cloud_models() -> None:
    fake = FakeOllama()

    with pytest.raises(OllamaProviderError, match="cloud model"):
        make_provider(fake).reasoning_support("glm-4.7:cloud")

    assert "/api/show" not in fake.paths


def test_max_output_limit_is_context_length_from_tags() -> None:
    fake = FakeOllama(models=[GPT_OSS_TAG, QWEN_TAG])
    provider = make_provider(fake)

    assert provider.max_output_limit("gpt-oss:20b") == 131072
    assert provider.max_output_limit("qwen3.5:4b") is None
    assert fake.paths == ["/api/tags"]


def test_answer_style_is_a_system_message_for_this_request_only() -> None:
    fake = FakeOllama()

    chat(make_provider(fake), "Hi", "Hello!", "More?", instructions="Be brief.")

    assert fake.chat_payloads[0]["messages"] == [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello!"},
        {"role": "user", "content": "More?"},
    ]
