# SUMMARY.md — Router Module Summary

This document summarizes the changes, findings, and compliance checks for the Model Router module.

---

## 1. What was Built
* **Model Router Filter:** Implemented the `model_router_filter` inlet filter at [`backend/avexie/router/filter.py`](file:///c:/Users/Dell/Documents/avexie/backend/avexie/router/filter.py). It detects:
  * Image and video attachments (routing to the `vision` lane/model).
  * Code blocks and coding keywords (routing to the `coding` lane/model).
  * Ambiguous queries (falling back to the local-only classifier path).
* **Local-only Classifier Fallback:** In ambiguous cases, triggers a fast single-word classification request (`"reasoning"`, `"coding"`, or `"vision"`) using the local resident model only.
* **Ollama Hot-Swap & GPU Memory Management:** Automatically updates the query payload with `keep_alive="5m"`. If the routed model is different from the previously loaded model, it spawns a non-blocking background task to unload the previous model (`keep_alive=0`), freeing up GPU memory.
* **Structured Logging:** Emits a lightweight `INFO` log line on every decision (e.g. `[ROUTE DECISION] lane=coding | model_tag=qwen3-coder-30b | signal=keyword=refactor | confidence=deterministic`).
* **Unit Tests:** Created [`test/test_router_filter.py`](file:///c:/Users/Dell/Documents/avexie/test/test_router_filter.py) covering all routing logic and mock scenarios.

---

## 2. Existing Infrastructure Utilized
* **Filter Pipeline Hooks:** Hooked directly into the `process_chat_payload` method in [`backend/avexie/utils/middleware.py`](file:///c:/Users/Dell/Documents/avexie/backend/avexie/utils/middleware.py#L2623-L2631) (as mapped in `ARCHITECTURE_MAP.md` Section 6).
* **Ollama keep_alive / Unload:** Integrated with Ollama endpoint schema bindings and the model unloading mechanisms inside `main.py` and `ollama.py` (referenced in `ARCHITECTURE_MAP.md` Section 8).

---

## 3. Compliance with CONTRACTS.md
* **RouteDecision Shape:** The generated decisions conform exactly to the TypedDict contract in `CONTRACTS.md` §3.
* **Side-Effect-Free Constraint:** The filter has no database write side-effects and writes no audit rows (those are managed downstream by the tool dispatch wrapper). It only interacts with the local Ollama API to control model lifecycle.
* **BEGIN/END Blocks:** All edits were restricted to the owned package `router/` and our named registration block inside the shared file `middleware.py`.

---

## 4. Air-Gap Guardrail (§0.5) Compliance Self-Check
* **Outbound Call Audit:** We inspected all modified files and the git diff.
* **Findings:** The filter is fully air-gapped. The fallback classifier executes calls exclusively on `localhost` (resolving against `Config` or defaulting to `http://localhost:11434`), and does not initiate, configure, or enable any outbound requests to external APIs or third-party domains.

---

## 5. Wave 2 Integration Guide
* **Model State Fallbacks:** If the routed target model tag (e.g., `qwen3-coder-30b`) is not yet present in the initialized server registry (`request.app.state.MODELS`), the filter constructs a dummy model dict structure matching pipeline expectations. This ensures the chat execution pipeline continues without crashing.
* **Tailing Logs:** Tailing logs for `[ROUTE DECISION]` will output real-time lane, model, and routing confidence signals for demo visibility.
