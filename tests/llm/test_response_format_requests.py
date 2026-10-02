"""Offline request-format contracts: what each path really puts on the wire.

The streaming callback paths once sent a hand-wrapped ``model_json_schema()``
that OpenAI rejected (HTTP 400, strict mode needs every property required).
Fake-chat tests could not see that, so these tests intercept the real HTTP
request body at the transport.
"""

import json
import tempfile
from pathlib import Path

import pytest
from test_openai_chat import _error, _managed

from kodocs.agent.types import ModelAnswer
from kodocs.llm.openai_chat import (
    FatalLLMError,
    callback_response_format,
    to_strict_json_schema,
)
from kodocs.ui.streaming import FinalStreamingChat

FROZEN = Path(__file__).parent / "fixtures" / "frozen_request_payloads.json"


def assert_strict(schema, path="$"):
    """OpenAI strict mode: closed objects and every property required ($defs too)."""
    if isinstance(schema, dict):
        if schema.get("type") == "object" or "properties" in schema:
            assert schema.get("additionalProperties") is False, path
            required = set(schema.get("required", []))
            assert set(schema.get("properties", {})) <= required, (
                f"{path}: not required: "
                f"{sorted(set(schema.get('properties', {})) - required)}"
            )
        for key, value in schema.items():
            assert_strict(value, f"{path}.{key}")
    elif isinstance(schema, list):
        for index, value in enumerate(schema):
            assert_strict(value, f"{path}[{index}]")


def send(**kwargs):
    """Run one invoke and return the JSON body of the request actually sent."""
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return _error(400, "stop after capture")

    managed, client = _managed(Path(tempfile.mkdtemp()), handler)
    wrapper = kwargs.pop("wrapper", None)
    try:
        target = wrapper(managed) if wrapper else managed
        with pytest.raises(FatalLLMError):
            target.invoke("질문", **kwargs)
    finally:
        client.close()
    assert len(bodies) == 1
    return bodies[0]


def schema_of(body):
    return body["text"]["format"]["schema"]


@pytest.fixture(scope="module")
def paths():

    def nothing(_):
        return None

    return {
        "m6": send(response_schema=ModelAnswer),
        "m7": send(response_schema=ModelAnswer, stream=True),
        "ui": send(
            response_schema=ModelAnswer,
            wrapper=lambda managed: FinalStreamingChat(managed, nothing, nothing),
        ),
        "A": send(response_schema=ModelAnswer),  # frozen M6 request, nothing added
        "B": send(
            response_schema=ModelAnswer,
            stream=True,
            on_text_delta=nothing,
            schema_style="langchain",
        ),
        "C": send(
            response_schema=ModelAnswer,
            stream=True,
            on_text_delta=nothing,
            schema_style="sdk",
        ),
    }


def test_validator_rejects_the_raw_model_json_schema_that_caused_the_400():
    raw = ModelAnswer.model_json_schema()
    assert raw["$defs"]["Citation"]["required"] == ["kind"]
    with pytest.raises(AssertionError):
        assert_strict(raw)


def test_library_schemas_satisfy_the_strict_validator():
    assert_strict(to_strict_json_schema(ModelAnswer))
    for style in ("sdk", "langchain"):
        assert_strict(callback_response_format(ModelAnswer, style)["json_schema"])


def test_every_path_sends_a_strict_schema_and_never_the_raw_one(paths):
    raw = ModelAnswer.model_json_schema()
    for name, body in paths.items():
        assert_strict(schema_of(body))
        assert schema_of(body) != raw, name
        assert body["text"]["format"]["strict"] is True, name
        assert body["text"]["format"]["type"] == "json_schema", name


def test_schemas_equal_the_installed_library_functions(paths):
    from langchain_openai.chat_models.base import _convert_to_openai_response_format

    sdk = to_strict_json_schema(ModelAnswer)
    langchain = _convert_to_openai_response_format(
        ModelAnswer.model_json_schema(), strict=True
    )["json_schema"]["schema"]
    assert sdk != langchain  # the two frozen paths really differ
    for name in ("m6", "A", "C", "ui"):  # M6 contract: SDK responses.parse schema
        assert schema_of(paths[name]) == sdk, name
    for name in ("m7", "B"):  # M7 contract: streamed pydantic-class schema
        assert schema_of(paths[name]) == langchain, name


def test_callback_paths_match_the_frozen_requests_they_stand_for(paths):
    assert paths["B"] == paths["m7"]  # B is M7 on the wire, byte for byte
    assert paths["A"] == paths["m6"]
    assert paths["ui"]["stream"] is True and paths["C"]["stream"] is True
    for key in ("stream",):  # C/UI = M6 request with streaming switched on
        assert {k: v for k, v in paths["C"].items() if k != key} == {
            k: v for k, v in paths["m6"].items() if k != key
        }
        assert paths["ui"] == paths["C"]


def test_frozen_m6_and_m7_requests_are_byte_identical_to_before_the_fix(paths):
    frozen = json.loads(FROZEN.read_text(encoding="utf-8"))
    for name, key in (("m6", "m6_nonstream"), ("m7", "m7_stream")):
        assert json.dumps(paths[name], sort_keys=True, ensure_ascii=False) == (
            json.dumps(frozen[key], sort_keys=True, ensure_ascii=False)
        ), name


def test_unknown_schema_style_is_rejected():
    with pytest.raises(ValueError):
        callback_response_format(ModelAnswer, "handmade")  # type: ignore[arg-type]
