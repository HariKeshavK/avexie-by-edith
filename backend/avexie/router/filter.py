"""
AVEXIE Model Router Filter
==========================
Deterministic 5-step routing engine driven by app.state.OLLAMA_MODELS.

Step 1 — Hard filter:  image/video attachment → keep only vision-capable models.
Step 2 — Intent:       classify prompt text into one of five intent buckets.
Step 3 — Narrow:       keep only models whose capability matches the intent.
Step 4 — Tie-break:    large param-count for heavy tasks; small for light tasks.
Step 5 — Validate:     chosen id must be a live key in OLLAMA_MODELS.

No LLM classifier call is made during routing.  All decisions are deterministic.
Side-effect-free except the optional Ollama keep_alive unload call.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Literal, Tuple, TypedDict

import aiohttp

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class RouteDecision(TypedDict):
    lane: Literal["reasoning", "coding", "vision", "extraction", "summarization", "general"]
    model_tag: str          # live model_id as returned by Ollama /api/tags
    signal: str             # human-readable debug string — do NOT parse programmatically
    confidence: Literal["deterministic"]

# ---------------------------------------------------------------------------
# Capability inference — purely from model name / details.family
# ---------------------------------------------------------------------------

_VISION_RE    = re.compile(r"llava|bakllava|vision|moondream", re.I)
_CODING_RE    = re.compile(r"coder|codellama|starcoder|deepseek.?coder", re.I)
_REASONING_RE = re.compile(r"\br1\b|deepseek.?r1|\bo1\b|qwq|reasoning", re.I)

def _capability(model_id: str, family: str = "") -> str:
    src = f"{model_id} {family}"
    if _VISION_RE.search(src):
        return "vision"
    if _CODING_RE.search(src):
        return "coding"
    if _REASONING_RE.search(src):
        return "reasoning"
    return "general"

# ---------------------------------------------------------------------------
# Prompt intent classification — deterministic regex / heuristics
# ---------------------------------------------------------------------------

_CODE_BLOCK_RE = re.compile(r"```[a-zA-Z0-9#\+\-]*\n", re.M)
_STACK_TRACE_RE = re.compile(r"(Traceback \(most recent call last\)|at \w+\.\w+\(|\w+Error:|\w+Exception:)", re.M)
_CODE_VERB_RE  = re.compile(
    r"\b(fix|refactor|debug|rewrite|optimize|patch|implement|compile|write a function|write a script)\b", re.I
)
_FILE_EXT_RE   = re.compile(r"\b\w+\.(py|js|ts|java|go|rs|cpp|c|cs|rb|php|sh|yaml|json|sql)\b", re.I)
_LIB_RE        = re.compile(
    r"\b(numpy|pandas|react|vue|svelte|fastapi|django|flask|pytorch|tensorflow|sklearn|langchain|openai|requests)\b", re.I
)

_REASONING_RE_PROMPT = re.compile(
    r"\b(step.by.step|step by step|prove|proof|mathematical|theorem|reasoning|if.then|iff|therefore|derive|"
    r"given that|infer|deduce|logic|plan|strategy|chain of thought|puzzle|riddle|how many|calculate)\b", re.I
)
_MATH_RE       = re.compile(r"[\d]+\s*[\+\-\*\/\^=]\s*[\d]+|[\u222b\u2211\u2202\u221a\u03c0\u2264\u2265\u2260]")

_EXTRACT_RE    = re.compile(
    r"\b(extract|parse|convert to json|convert to table|convert to csv|structured output|output as json|output as yaml|"
    r"return json|return a dict|return a list)\b", re.I
)
_SUMMARIZE_RE  = re.compile(r"\b(summarize|summary|tl;?dr|tldr|briefly|in a nutshell|what is the gist)\b", re.I)

# Rough token estimate (1 token ~= 4 chars)
_LONG_PROMPT_CHARS = 500 * 4  # ~500 tokens

def _classify_intent(text: str) -> str:
    """Return one of: coding, reasoning, extraction, summarization, general."""
    if (
        _CODE_BLOCK_RE.search(text)
        or _STACK_TRACE_RE.search(text)
        or _CODE_VERB_RE.search(text)
        or _FILE_EXT_RE.search(text)
        or _LIB_RE.search(text)
    ):
        return "coding"

    if _REASONING_RE_PROMPT.search(text) or _MATH_RE.search(text):
        return "reasoning"

    if _EXTRACT_RE.search(text):
        return "extraction"

    if _SUMMARIZE_RE.search(text):
        return "summarization"

    return "general"

# ---------------------------------------------------------------------------
# Parameter-size parsing helpers
# ---------------------------------------------------------------------------

_PARAM_NUM_RE = re.compile(r"([\d.]+)\s*([BbMmKk])?")

def _parse_param_size(raw: str | None) -> float:
    """Parse '8B', '70B', '3.8B', '405B' -> float in billions.  Returns 0 on failure."""
    if not raw:
        return 0.0
    m = _PARAM_NUM_RE.search(str(raw))
    if not m:
        return 0.0
    num = float(m.group(1))
    unit = (m.group(2) or "B").upper()
    if unit == "M":
        return num / 1000
    if unit == "K":
        return num / 1_000_000
    return num  # billions

# ---------------------------------------------------------------------------
# DEFAULT_MODEL resolution
# ---------------------------------------------------------------------------

async def _resolve_default_model(request: Any, available: dict) -> str:
    """
    1. Try Config table key 'default_models'.
    2. Fall back to first key in available.
    3. Hard-stop: 'llama3:latest'.
    """
    try:
        from avexie.models.config import Config  # type: ignore
        default = await Config.get("default_models")
        if default and isinstance(default, list) and default[0] in available:
            return default[0]
        if default and isinstance(default, str) and default in available:
            return default
    except Exception:
        pass

    if available:
        return next(iter(available))

    return "llama3:latest"

# ---------------------------------------------------------------------------
# Ollama model unload (fire-and-forget, side-effect only)
# ---------------------------------------------------------------------------

async def _unload_ollama_model(base_url: str, model_id: str) -> None:
    """Send keep_alive=0 to free the previous model from GPU memory."""
    try:
        payload = {"model": model_id, "keep_alive": 0, "prompt": ""}
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{base_url}/api/generate", json=payload) as r:
                await r.read()
        log.info("[ROUTER] Unloaded model %r (keep_alive=0)", model_id)
    except Exception as exc:
        log.warning("[ROUTER] Failed to unload %r: %s", model_id, exc)

# ---------------------------------------------------------------------------
# Core routing engine
# ---------------------------------------------------------------------------

async def model_router_filter(
    request: Any,
    body: dict,
    user: Any,
    model: Any,
    extra_params: dict,
) -> Tuple[dict, Any]:
    """
    Pre-dispatch inlet filter.  Mutates body['model'] to the chosen model_id.
    Returns (body, target_model_struct).
    """

    # Gather live registry
    available: dict = {}
    if request and hasattr(request, "app"):
        available = dict(getattr(request.app.state, "OLLAMA_MODELS", {}) or {})

    default_model_id = await _resolve_default_model(request, available)

    # Step 1: Hard filter — image / video attachments
    files = body.get("files", [])
    has_media = False
    attachment_mime = ""
    for f in files:
        mime = f.get("meta", {}).get("content_type") or f.get("content_type") or ""
        if mime.startswith("image/") or mime.startswith("video/"):
            has_media = True
            attachment_mime = mime
            break

    # Extract last user message
    messages = body.get("messages", [])
    last_user_text = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                for part in content:
                    if part.get("type") == "text":
                        last_user_text += part.get("text", "")
            else:
                last_user_text = content
            break

    is_long_prompt = len(last_user_text) > _LONG_PROMPT_CHARS

    # Step 2: Classify intent
    if has_media:
        intent = "vision"
    else:
        intent = _classify_intent(last_user_text)

    # Step 3: Narrow candidates by inferred capability
    intent_to_capability = {
        "vision":        "vision",
        "coding":        "coding",
        "reasoning":     "reasoning",
        "extraction":    "general",
        "summarization": "general",
        "general":       "general",
    }
    target_capability = intent_to_capability[intent]

    all_candidates = [
        {
            "id": mid,
            "capability": _capability(mid, entry.get("details", {}).get("family", "")),
            "param_size": _parse_param_size(entry.get("details", {}).get("parameter_size")),
        }
        for mid, entry in available.items()
    ]

    candidates = [c for c in all_candidates if c["capability"] == target_capability]
    signal_prefix = f"capability={target_capability}"

    if not candidates:
        # Step 3 fallback: no matching capability model -> use full set
        candidates = all_candidates
        signal_prefix = f"capability=fallback(no_{target_capability})"

    if not candidates:
        # Step 5 early exit: registry is empty
        chosen_id = default_model_id
        signal = "registry_empty->default"
        decision = RouteDecision(
            lane=intent,
            model_tag=chosen_id,
            signal=signal,
            confidence="deterministic",
        )
        _emit_log(decision)
        return _apply_decision(body, extra_params, decision, request, model, available, default_model_id)

    # Step 4: Tie-break by parameter_size
    heavy = intent in ("coding", "reasoning") or is_long_prompt
    candidates_sorted = sorted(candidates, key=lambda c: c["param_size"], reverse=heavy)
    chosen_id = candidates_sorted[0]["id"]

    # Step 5: Validate
    if chosen_id not in available:
        chosen_id = default_model_id
        signal = "validation_fail->default"
    else:
        direction = "largest" if heavy else "smallest"
        signal = f"{signal_prefix}|{direction}_param|intent={intent}"

    decision = RouteDecision(
        lane=intent,
        model_tag=chosen_id,
        signal=signal,
        confidence="deterministic",
    )
    _emit_log(decision)
    return _apply_decision(body, extra_params, decision, request, model, available, default_model_id)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _emit_log(decision: RouteDecision) -> None:
    log.info(
        "[ROUTE DECISION] lane=%s | model_tag=%s | signal=%s | confidence=%s",
        decision["lane"],
        decision["model_tag"],
        decision["signal"],
        decision["confidence"],
    )


def _apply_decision(
    body: dict,
    extra_params: dict,
    decision: RouteDecision,
    request: Any,
    prev_model: Any,
    available: dict,
    default_model_id: str,
) -> Tuple[dict, Any]:
    target_model_id = decision["model_tag"]

    # Persist in-memory (no DB write)
    metadata = extra_params.get("__metadata__") or {}
    metadata["route_decision"] = decision
    extra_params["__route_decision__"] = decision
    metadata["selected_model_id"] = target_model_id

    # Mutate payload
    body["model"] = target_model_id
    body["keep_alive"] = "5m"

    # Build target model struct for the dispatch pipeline
    app_models: dict = {}
    if request and hasattr(request, "app"):
        app_models = getattr(request.app.state, "MODELS", {}) or {}

    target_model = app_models.get(target_model_id)
    if not target_model:
        for key, val in app_models.items():
            if target_model_id in key or key in target_model_id:
                target_model = val
                target_model_id = key
                body["model"] = target_model_id
                metadata["selected_model_id"] = target_model_id
                break

    if not target_model:
        target_model = {
            "id": target_model_id,
            "name": target_model_id,
            "owned_by": "ollama",
            "info": {"meta": {"capabilities": {"builtin_tools": True, "file_context": True}}},
        }

    extra_params["__model__"] = target_model

    # Hot-swap: unload previous model if it changed
    prev_model_id: str | None = None
    if isinstance(prev_model, dict):
        prev_model_id = prev_model.get("id")
    elif hasattr(prev_model, "id"):
        prev_model_id = prev_model.id

    if prev_model_id and prev_model_id != target_model_id:
        ollama_models: dict = {}
        if request and hasattr(request, "app"):
            ollama_models = getattr(request.app.state, "OLLAMA_MODELS", {}) or {}
        if prev_model_id in ollama_models or any(prev_model_id in k for k in ollama_models):
            try:
                from avexie.models.config import Config  # type: ignore

                async def _unload() -> None:
                    try:
                        urls = await Config.get("ollama.base_urls") or ["http://localhost:11434"]
                        await _unload_ollama_model(urls[0], prev_model_id)
                    except Exception as exc:
                        log.warning("[ROUTER] Unload task error: %s", exc)

                asyncio.create_task(_unload())
            except Exception as exc:
                log.warning("[ROUTER] Could not schedule unload: %s", exc)

    return body, target_model
