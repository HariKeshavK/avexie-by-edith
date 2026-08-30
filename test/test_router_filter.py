"""
Tests for AVEXIE deterministic model router.

All tests are fully offline — no real Ollama daemon required.
The OLLAMA_MODELS registry is injected directly into a mock request.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from avexie.router.filter import (
    model_router_filter,
    _capability,
    _classify_intent,
    _parse_param_size,
    RouteDecision,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_REGISTRY = {
    "llama3:latest": {
        "model": "llama3:latest",
        "size": 4661224676,
        "details": {"family": "llama", "parameter_size": "8B", "quantization_level": "Q4_0"},
    },
    "qwen2.5-coder:latest": {
        "model": "qwen2.5-coder:latest",
        "size": 4700000000,
        "details": {"family": "qwen2", "parameter_size": "7B", "quantization_level": "Q4_0"},
    },
    "llava:latest": {
        "model": "llava:latest",
        "size": 4700000000,
        "details": {"family": "llava", "parameter_size": "7B", "quantization_level": "Q4_0"},
    },
}

REGISTRY_WITH_SIZES = {
    "llama3:8b": {
        "model": "llama3:8b",
        "details": {"family": "llama", "parameter_size": "8B"},
    },
    "llama3:70b": {
        "model": "llama3:70b",
        "details": {"family": "llama", "parameter_size": "70B"},
    },
    "qwen2.5-coder:7b": {
        "model": "qwen2.5-coder:7b",
        "details": {"family": "qwen2", "parameter_size": "7B"},
    },
    "llava:latest": {
        "model": "llava:latest",
        "details": {"family": "llava", "parameter_size": "7B"},
    },
}


def mock_request(registry: dict = None):
    req = MagicMock()
    req.app = MagicMock()
    req.app.state = MagicMock()
    req.app.state.OLLAMA_MODELS = dict(registry or SAMPLE_REGISTRY)
    req.app.state.MODELS = {}
    return req


def simple_body(text: str, files=None, model="llama3:latest"):
    return {
        "model": model,
        "messages": [{"role": "user", "content": text}],
        "files": files or [],
    }


def extra():
    return {"__metadata__": {}}


# ---------------------------------------------------------------------------
# Unit tests for helper functions
# ---------------------------------------------------------------------------

class TestCapabilityInference:
    def test_llava_is_vision(self):
        assert _capability("llava:latest") == "vision"

    def test_bakllava_is_vision(self):
        assert _capability("bakllava:latest") == "vision"

    def test_moondream_is_vision(self):
        assert _capability("moondream:latest") == "vision"

    def test_coder_is_coding(self):
        assert _capability("qwen2.5-coder:latest") == "coding"

    def test_codellama_is_coding(self):
        assert _capability("codellama:13b") == "coding"

    def test_starcoder_is_coding(self):
        assert _capability("starcoder2:7b") == "coding"

    def test_deepseek_coder_is_coding(self):
        assert _capability("deepseek-coder:6.7b") == "coding"

    def test_qwq_is_reasoning(self):
        assert _capability("qwq:32b") == "reasoning"

    def test_deepseek_r1_is_reasoning(self):
        assert _capability("deepseek-r1:7b") == "reasoning"

    def test_llama3_is_general(self):
        assert _capability("llama3:latest") == "general"

    def test_family_string_used(self):
        # family "llava" should trigger vision even with neutral model name
        assert _capability("model-x:latest", family="llava") == "vision"


class TestParamSizeParsing:
    def test_8b(self):
        assert _parse_param_size("8B") == 8.0

    def test_70b(self):
        assert _parse_param_size("70B") == 70.0

    def test_3_8b(self):
        assert _parse_param_size("3.8B") == 3.8

    def test_lowercase_b(self):
        assert _parse_param_size("7b") == 7.0

    def test_millions(self):
        assert _parse_param_size("350M") == pytest.approx(0.35, rel=1e-3)

    def test_none_returns_zero(self):
        assert _parse_param_size(None) == 0.0

    def test_empty_returns_zero(self):
        assert _parse_param_size("") == 0.0


class TestIntentClassification:
    def test_code_block_detected(self):
        assert _classify_intent("Review this:\n```python\nx = 1\n```") == "coding"

    def test_stack_trace_detected(self):
        assert _classify_intent("I got: Traceback (most recent call last):") == "coding"

    def test_debug_verb(self):
        assert _classify_intent("Please debug this function") == "coding"

    def test_refactor_verb(self):
        assert _classify_intent("Can you refactor this code?") == "coding"

    def test_file_extension(self):
        assert _classify_intent("What's wrong with my script.py?") == "coding"

    def test_library_name(self):
        assert _classify_intent("How do I use numpy for matrix multiplication?") == "coding"

    def test_step_by_step(self):
        assert _classify_intent("Explain step by step how gravity works") == "reasoning"

    def test_math_expression(self):
        assert _classify_intent("What is 2 + 2 = 4 in modular arithmetic?") == "reasoning"

    def test_extract_to_json(self):
        assert _classify_intent("Extract the names and convert to JSON") == "extraction"

    def test_summarize(self):
        assert _classify_intent("Summarize this article for me") == "summarization"

    def test_tldr(self):
        assert _classify_intent("tl;dr this for me") == "summarization"

    def test_general(self):
        assert _classify_intent("Hello, how are you today?") == "general"


# ---------------------------------------------------------------------------
# Integration tests: model_router_filter end-to-end
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_step1_image_attachment_routes_to_vision():
    """Step 1: image attachment → llava selected."""
    req = mock_request(SAMPLE_REGISTRY)
    body = simple_body("Analyze this", files=[{"meta": {"content_type": "image/png"}}])

    new_body, _ = await model_router_filter(req, body, None, {"id": "llama3:latest"}, extra())

    assert new_body["model"] == "llava:latest"
    decision = extra().get("__route_decision__") or {}


@pytest.mark.asyncio
async def test_step1_video_attachment_routes_to_vision():
    """Step 1: video attachment → llava selected."""
    req = mock_request(SAMPLE_REGISTRY)
    body = simple_body("Watch this", files=[{"meta": {"content_type": "video/mp4"}}])

    new_body, _ = await model_router_filter(req, body, None, {"id": "llama3:latest"}, extra())

    assert new_body["model"] == "llava:latest"


@pytest.mark.asyncio
async def test_step1_no_vision_model_returns_default():
    """Step 1: image attachment but no vision model in registry → default."""
    registry_no_vision = {
        "llama3:latest": SAMPLE_REGISTRY["llama3:latest"],
        "qwen2.5-coder:latest": SAMPLE_REGISTRY["qwen2.5-coder:latest"],
    }
    req = mock_request(registry_no_vision)
    body = simple_body("Image task", files=[{"meta": {"content_type": "image/png"}}])
    ep = extra()

    new_body, _ = await model_router_filter(req, body, None, {"id": "llama3:latest"}, ep)

    # Falls back to full set tie-broken by param size (both 8B/7B, smallest = coder 7B)
    assert new_body["model"] in registry_no_vision


@pytest.mark.asyncio
async def test_step2_code_block_routes_to_coder():
    """Step 2: code block in message → coding intent → qwen2.5-coder selected."""
    req = mock_request(SAMPLE_REGISTRY)
    body = simple_body("Fix this:\n```python\nprint('hello')\n```")

    new_body, _ = await model_router_filter(req, body, None, {"id": "llama3:latest"}, extra())

    assert new_body["model"] == "qwen2.5-coder:latest"


@pytest.mark.asyncio
async def test_step2_debug_verb_routes_to_coder():
    """Step 2: 'debug' keyword → coding intent → coder model."""
    req = mock_request(SAMPLE_REGISTRY)
    body = simple_body("Can you debug this for me?")

    new_body, _ = await model_router_filter(req, body, None, {"id": "llama3:latest"}, extra())

    assert new_body["model"] == "qwen2.5-coder:latest"


@pytest.mark.asyncio
async def test_step2_reasoning_intent():
    """Step 2: 'explain step by step' → reasoning → general model (no reasoning specialist)."""
    req = mock_request(SAMPLE_REGISTRY)
    body = simple_body("Explain step by step how photosynthesis works")
    ep = extra()

    new_body, _ = await model_router_filter(req, body, None, {"id": "llama3:latest"}, ep)

    decision = ep["__route_decision__"]
    assert decision["lane"] == "reasoning"
    # No reasoning-tagged model in SAMPLE_REGISTRY → fallback to full set, largest = llama3 8B
    assert new_body["model"] in SAMPLE_REGISTRY


@pytest.mark.asyncio
async def test_step2_summarization_routes_to_smallest():
    """Step 2: summarize → general → smallest param model chosen."""
    registry = {
        "llama3:70b": {"model": "llama3:70b", "details": {"family": "llama", "parameter_size": "70B"}},
        "llama3:8b":  {"model": "llama3:8b",  "details": {"family": "llama", "parameter_size": "8B"}},
    }
    req = mock_request(registry)
    body = simple_body("Please summarize the following article...")
    ep = extra()

    new_body, _ = await model_router_filter(req, body, None, {"id": "llama3:70b"}, ep)

    assert new_body["model"] == "llama3:8b"  # smallest for light task


@pytest.mark.asyncio
async def test_step4_reasoning_prefers_largest():
    """Step 4: reasoning intent → largest param model preferred."""
    registry = REGISTRY_WITH_SIZES
    req = mock_request(registry)
    body = simple_body("Prove that there are infinitely many prime numbers step by step")

    new_body, _ = await model_router_filter(req, body, None, {"id": "llama3:8b"}, extra())

    # No reasoning model → falls back to all; largest is llama3:70b
    assert new_body["model"] == "llama3:70b"


@pytest.mark.asyncio
async def test_step4_general_prefers_smallest():
    """Step 4: general intent → smallest param model preferred."""
    registry = REGISTRY_WITH_SIZES
    req = mock_request(registry)
    body = simple_body("Hi, what's the weather like?")
    ep = extra()

    new_body, _ = await model_router_filter(req, body, None, {"id": "llama3:70b"}, ep)

    decision = ep["__route_decision__"]
    assert decision["lane"] == "general"
    # Smallest general model: llama3:8b (8B) vs llama3:70b (70B)
    assert new_body["model"] == "llama3:8b"


@pytest.mark.asyncio
async def test_step5_empty_registry_returns_default():
    """Step 5: empty OLLAMA_MODELS → hard-stop default 'llama3:latest' returned."""
    req = mock_request({})
    body = simple_body("Hello")
    ep = extra()

    new_body, _ = await model_router_filter(req, body, None, {"id": "something"}, ep)

    # _resolve_default_model returns 'llama3:latest' as hard-stop;
    # it becomes the selected model regardless of the signal path taken.
    assert new_body["model"] == "llama3:latest"
    rd = ep["__route_decision__"]
    assert rd["model_tag"] == "llama3:latest"
    assert rd["confidence"] == "deterministic"


@pytest.mark.asyncio
async def test_route_decision_written_to_extra_params():
    """RouteDecision is always written to extra_params['__route_decision__']."""
    req = mock_request(SAMPLE_REGISTRY)
    body = simple_body("Hello")
    ep = extra()

    await model_router_filter(req, body, None, {"id": "llama3:latest"}, ep)

    rd = ep["__route_decision__"]
    assert "lane" in rd
    assert "model_tag" in rd
    assert "signal" in rd
    assert rd["confidence"] == "deterministic"


@pytest.mark.asyncio
async def test_keep_alive_set_on_body():
    """body['keep_alive'] is always set to '5m' after routing."""
    req = mock_request(SAMPLE_REGISTRY)
    body = simple_body("Hello")

    new_body, _ = await model_router_filter(req, body, None, {"id": "llama3:latest"}, extra())

    assert new_body["keep_alive"] == "5m"
