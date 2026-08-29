import re
import logging
import aiohttp
import asyncio
from typing import Literal, TypedDict, Tuple, Any
from fastapi import Request

from avexie.config import OLLAMA_BASE_URLS

log = logging.getLogger(__name__)

class RouteDecision(TypedDict):
    lane: Literal["reasoning", "coding", "vision"]
    model_tag: str            # e.g. "gpt-oss-120b", "qwen3-coder-30b"
    signal: str                # what triggered the route — for audit/debug, e.g. "attachment=image/png"
    confidence: Literal["deterministic", "classified"]

# Lane to model mapping
LANE_MODELS = {
    "vision": "gpt-oss-120b",
    "coding": "qwen3-coder-30b",
    "reasoning": "gpt-oss-120b"
}

# Verbs signaling a coding task
CODING_KEYWORDS = re.compile(
    r"\b(fix|refactor|debug|correct|optimize|patch|rewrite|rewrite_code|implement|coding|script)\b",
    re.IGNORECASE
)

# Markdown code block regex
CODE_BLOCK_PATTERN = re.compile(r"```[a-zA-Z0-9#\+\-]*\n", re.MULTILINE)


async def unload_ollama_model(request: Request, model_id: str):
    """Sends a request to the Ollama backend to unload a model (keep_alive=0)."""
    try:
        from avexie.models.config import Config
        ollama_urls = await Config.get('ollama.base_urls') or OLLAMA_BASE_URLS or ['http://localhost:11434']
        url = ollama_urls[0]
        
        # Strip provider model prefix if any
        actual_model = model_id
        if ":" in model_id:
            actual_model = model_id.split(":")[-1]
            
        payload = {"model": actual_model, "keep_alive": 0, "prompt": ""}
        log.info(f"Unloading Ollama model {model_id} from GPU (keep_alive=0)")
        
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{url}/api/generate", json=payload) as r:
                await r.read()
    except Exception as e:
        log.warning(f"Failed to unload Ollama model {model_id}: {e}")


async def classify_via_local_ollama(query: str, request: Request = None) -> RouteDecision:
    """Classifies an ambiguous query using the local resident model only."""
    try:
        from avexie.models.config import Config
        ollama_urls = None
        if request and hasattr(request, 'app'):
            ollama_urls = await Config.get('ollama.base_urls')
        ollama_urls = ollama_urls or OLLAMA_BASE_URLS or ['http://localhost:11434']
        url = ollama_urls[0]
    except Exception:
        url = 'http://localhost:11434'

    system_prompt = (
        "Classify the query into exactly one word: 'reasoning', 'coding', or 'vision'. "
        "Return ONLY the word, with no code fences, markup, explanation, or leading/trailing spaces."
    )
    
    # Run classification on the default resident model
    resident_model = LANE_MODELS["reasoning"]
    payload = {
        "model": resident_model,
        "prompt": f"System: {system_prompt}\nUser Query: {query}\nClassification:",
        "stream": False,
        "keep_alive": "5m",  # Keep the general model loaded
        "options": {
            "temperature": 0.0,
            "num_predict": 5
        }
    }
    
    lane = "reasoning"
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{url}/api/generate", json=payload) as r:
                r.raise_for_status()
                res = await r.json()
                raw_response = res.get("response", "").strip().lower()
                
                # Direct match checking
                if "coding" in raw_response:
                    lane = "coding"
                elif "vision" in raw_response:
                    lane = "vision"
                else:
                    lane = "reasoning"
    except Exception as e:
        log.warning(f"Local Ollama classification failed, defaulting to reasoning: {e}")
        lane = "reasoning"

    return RouteDecision(
        lane=lane,
        model_tag=LANE_MODELS[lane],
        signal="classified_by_local_llm",
        confidence="classified"
    )


async def model_router_filter(
    request: Request,
    body: dict,
    user: Any,
    model: Any,
    extra_params: dict
) -> Tuple[dict, Any]:
    """
    Model routing filter implementing deterministic-signal rules and local-classifier fallback.
    Mutates body['model'] and manages Ollama keep_alive / model hot-swapping.
    """
    # 1. Check for attachments (MIME type starting with image/ or video/)
    files = body.get('files', [])
    has_media_attachment = False
    attachment_mime = ""
    for f in files:
        mime = f.get('meta', {}).get('content_type') or f.get('content_type') or ""
        if mime.startswith('image/') or mime.startswith('video/'):
            has_media_attachment = True
            attachment_mime = mime
            break

    # 2. Extract last user message for parsing
    messages = body.get('messages', [])
    last_user_message = ""
    if messages:
        for msg in reversed(messages):
            if msg.get('role') == 'user':
                content = msg.get('content', '')
                if isinstance(content, list):
                    for part in content:
                        if part.get('type') == 'text':
                            last_user_message += part.get('text', '')
                else:
                    last_user_message = content
                break

    # 3. Code block & Keyword analysis
    has_code_block = False
    has_coding_keyword = False
    keyword_match = None

    if last_user_message:
        if CODE_BLOCK_PATTERN.search(last_user_message):
            has_code_block = True
        keyword_match = CODING_KEYWORDS.search(last_user_message)
        if keyword_match:
            has_coding_keyword = True

    # 4. Route evaluation
    decision = None
    if has_media_attachment:
        decision = RouteDecision(
            lane="vision",
            model_tag=LANE_MODELS["vision"],
            signal=f"attachment={attachment_mime}",
            confidence="deterministic"
        )
    elif has_code_block:
        decision = RouteDecision(
            lane="coding",
            model_tag=LANE_MODELS["coding"],
            signal="code_block_detected",
            confidence="deterministic"
        )
    elif has_coding_keyword and keyword_match:
        decision = RouteDecision(
            lane="coding",
            model_tag=LANE_MODELS["coding"],
            signal=f"keyword={keyword_match.group(1).lower()}",
            confidence="deterministic"
        )
    else:
        # Fall back to local Ollama classifier
        decision = await classify_via_local_ollama(last_user_message, request)

    # Log/Save decision in metadata for auditable tracing (in-memory only)
    log.info(
        f"[ROUTE DECISION] lane={decision['lane']} | model_tag={decision['model_tag']} | "
        f"signal={decision['signal']} | confidence={decision['confidence']}"
    )

    metadata = extra_params.get('__metadata__') or {}
    metadata['route_decision'] = decision
    extra_params['__route_decision__'] = decision

    target_model_id = decision['model_tag']
    body['model'] = target_model_id
    body['keep_alive'] = '5m'  # Standard resource retention window
    metadata['selected_model_id'] = target_model_id

    # 5. Resolve target model object structure
    app_models = getattr(request.app.state, 'MODELS', {}) if request and hasattr(request, 'app') else {}
    target_model = None

    if target_model_id in app_models:
        target_model = app_models[target_model_id]
    else:
        # search for matching tags (e.g. with tag:latest)
        for key, val in app_models.items():
            if target_model_id in key or key in target_model_id:
                target_model = val
                target_model_id = key
                body['model'] = target_model_id
                metadata['selected_model_id'] = target_model_id
                break

    if not target_model:
        # construct dummy model object for execution pipeline consistency
        target_model = {
            'id': target_model_id,
            'name': target_model_id,
            'owned_by': 'ollama',
            'info': {'meta': {'capabilities': {'builtin_tools': True, 'file_context': True}}}
        }

    extra_params['__model__'] = target_model

    # 6. Model hotswap: Unload previous model if different
    previous_model_id = None
    if isinstance(model, dict):
        previous_model_id = model.get('id')
    elif hasattr(model, 'id'):
        previous_model_id = model.id

    if previous_model_id and previous_model_id != target_model_id:
        ollama_models = getattr(request.app.state, 'OLLAMA_MODELS', {}) if request and hasattr(request, 'app') else {}
        if previous_model_id in ollama_models or any(previous_model_id in k for k in ollama_models.keys()):
            asyncio.create_task(unload_ollama_model(request, previous_model_id))

    return body, target_model
