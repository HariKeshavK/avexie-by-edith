import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import Request

from avexie.router.filter import model_router_filter, RouteDecision

# Helper to construct a mock request object
def mock_request():
    req = MagicMock(spec=Request)
    req.app = MagicMock()
    req.app.state = MagicMock()
    req.app.state.MODELS = {}
    req.app.state.OLLAMA_MODELS = {}
    return req


@pytest.mark.asyncio
async def test_image_attachment_deterministic():
    req = mock_request()
    body = {
        "model": "original-model",
        "messages": [{"role": "user", "content": "Analyze this."}],
        "files": [{"meta": {"content_type": "image/png"}}]
    }
    extra_params = {"__metadata__": {}}
    model = {"id": "original-model"}

    new_body, target_model = await model_router_filter(req, body, None, model, extra_params)

    assert new_body["model"] == "gpt-oss-120b"
    assert extra_params["__route_decision__"]["lane"] == "vision"
    assert extra_params["__route_decision__"]["confidence"] == "deterministic"
    assert "attachment=image/png" in extra_params["__route_decision__"]["signal"]


@pytest.mark.asyncio
async def test_video_attachment_deterministic():
    req = mock_request()
    body = {
        "model": "original-model",
        "messages": [{"role": "user", "content": "Watch this."}],
        "files": [{"meta": {"content_type": "video/mp4"}}]
    }
    extra_params = {"__metadata__": {}}
    model = {"id": "original-model"}

    new_body, target_model = await model_router_filter(req, body, None, model, extra_params)

    assert new_body["model"] == "gpt-oss-120b"
    assert extra_params["__route_decision__"]["lane"] == "vision"
    assert extra_params["__route_decision__"]["confidence"] == "deterministic"
    assert "attachment=video/mp4" in extra_params["__route_decision__"]["signal"]


@pytest.mark.asyncio
async def test_code_block_deterministic():
    req = mock_request()
    body = {
        "model": "original-model",
        "messages": [{"role": "user", "content": "Review this:\n```python\ndef test():\n    pass\n```"}],
        "files": []
    }
    extra_params = {"__metadata__": {}}
    model = {"id": "original-model"}

    new_body, target_model = await model_router_filter(req, body, None, model, extra_params)

    assert new_body["model"] == "qwen3-coder-30b"
    assert extra_params["__route_decision__"]["lane"] == "coding"
    assert extra_params["__route_decision__"]["confidence"] == "deterministic"
    assert extra_params["__route_decision__"]["signal"] == "code_block_detected"


@pytest.mark.asyncio
async def test_coding_verb_deterministic():
    req = mock_request()
    body = {
        "model": "original-model",
        "messages": [{"role": "user", "content": "Please refactor this function to be faster."}],
        "files": []
    }
    extra_params = {"__metadata__": {}}
    model = {"id": "original-model"}

    new_body, target_model = await model_router_filter(req, body, None, model, extra_params)

    assert new_body["model"] == "qwen3-coder-30b"
    assert extra_params["__route_decision__"]["lane"] == "coding"
    assert extra_params["__route_decision__"]["confidence"] == "deterministic"
    assert extra_params["__route_decision__"]["signal"] == "keyword=refactor"


@pytest.mark.asyncio
@patch("avexie.router.filter.classify_via_local_ollama")
async def test_ambiguous_text_classifier_fallback(mock_classify):
    req = mock_request()
    # Mocking classification output
    mock_classify.return_value = RouteDecision(
        lane="coding",
        model_tag="qwen3-coder-30b",
        signal="classified_by_local_llm",
        confidence="classified"
    )

    body = {
        "model": "original-model",
        "messages": [{"role": "user", "content": "How do I build a treehouse?"}],
        "files": []
    }
    extra_params = {"__metadata__": {}}
    model = {"id": "original-model"}

    new_body, target_model = await model_router_filter(req, body, None, model, extra_params)

    # Verify classify_via_local_ollama was called
    mock_classify.assert_called_once_with("How do I build a treehouse?", req)
    assert new_body["model"] == "qwen3-coder-30b"
    assert extra_params["__route_decision__"]["lane"] == "coding"
    assert extra_params["__route_decision__"]["confidence"] == "classified"
    assert extra_params["__route_decision__"]["signal"] == "classified_by_local_llm"


@pytest.mark.asyncio
@patch("aiohttp.ClientSession.post")
async def test_local_ollama_client_success(mock_post):
    # Mock HTTP response from Ollama generate endpoint returning 'coding'
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.raise_for_status = MagicMock()
    mock_response.json = AsyncMock(return_value={"response": "coding"})
    
    mock_context = MagicMock()
    mock_context.__aenter__ = AsyncMock(return_value=mock_response)
    mock_context.__aexit__ = AsyncMock(return_value=None)
    mock_post.return_value = mock_context

    req = mock_request()
    body = {
        "model": "original-model",
        "messages": [{"role": "user", "content": "How do I sort a list?"}],
        "files": []
    }
    extra_params = {"__metadata__": {}}
    model = {"id": "original-model"}

    new_body, target_model = await model_router_filter(req, body, None, model, extra_params)

    assert new_body["model"] == "qwen3-coder-30b"
    assert extra_params["__route_decision__"]["lane"] == "coding"
    assert extra_params["__route_decision__"]["confidence"] == "classified"
