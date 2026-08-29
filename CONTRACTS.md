# AVEXIE — CONTRACTS.md

**Read this before writing any code in `db/`, `toaa/`, `router/`, `rag/`, `sandbox/`, `tools/`, or `filestore/`.**

This file is the single source of truth for interfaces shared across modules. If your work
requires changing something defined here, stop and raise it in the team channel first —
do not silently redefine a shape. Everyone (human or agent) building against these contracts
should assume the *other* modules are mocked/stubbed until integration, and build to match
this doc, not to match whatever another module currently does.

Last updated: <fill in> · Owner of this file: <fill in — one person merges edits to CONTRACTS.md>

---

## 0. Directory ownership map

| Directory | Owns | Primary |
|---|---|---|
| `db/` | Postgres schema, migrations, connection pooling | Person A |
| `toaa/` | Audit wrapper, `toaa_audit_record`, (later) `toaa_ticket` | Person A |
| `rag/` | ChromaDB client, PLAYBOOK ingest + query | Person B |
| `router/` | Model-selection filter fn, Ollama hot-swap | Person C |
| `sandbox/` | Judge0 client, code-exec tool, IDE panel | Person D |
| `tools/` | Tool registration, individual tool implementations (KB search, docx/pptx/xlsx, file I/O) | Wave 2 — assigned per tool |
| `filestore/` | User + org file storage, metadata | TBD Wave 2 |

**Rule:** only the owner edits inside their directory. Anyone else needing a change there opens
a PR against that owner, doesn't push directly.

**Shared files everyone touches** (`docker-compose.yml`, `requirements.txt`/`package.json`,
app startup/init module, `.env.example`): each owner adds *only their own block*, marked with a
comment header (see §6). Never reformat or reorder someone else's block.

---

## 1. TOAA audit wrapper — the most load-bearing contract

Every tool call, from every dispatch path, passes through this wrapper before execution.
No tool executes without producing an audit row. This is what makes the sovereignty/traceability
claims checkable facts.

### 1.1 Function signature (Python, illustrative — adjust types to actual stack)

```python
def toaa_wrap(
    tool_name: str,
    tool_input: dict,
    tool_fn: Callable[[dict], dict],
    *,
    session_id: str,
    user_id: str,
    requires_approval: bool = False,
) -> ToaaResult:
    """
    Wraps any tool call. Writes an audit row BEFORE execution (status=pending)
    and updates it AFTER execution (status=success|error|blocked).
    Never raises silently — a wrapper failure is itself an audit-worthy event.
    """
```

### 1.2 `toaa_audit_record` table shape

| Column | Type | Notes |
|---|---|---|
| `id` | uuid, PK | |
| `session_id` | text | groups calls within one chat session |
| `user_id` | text | who triggered it |
| `tool_name` | text | e.g. `vlm_extract`, `kb_search`, `generate_docx`, `run_code` |
| `tool_input` | jsonb | sanitized — no secrets logged |
| `tool_output` | jsonb, nullable | null while pending |
| `status` | text | `pending` \| `success` \| `error` \| `blocked` |
| `requires_approval` | boolean | default false in MVP (approval flow is Phase 8 / stretch) |
| `created_at` | timestamptz | |
| `completed_at` | timestamptz, nullable | |
| `error_detail` | text, nullable | |

**Append-only. No UPDATE that changes `tool_input`, `tool_name`, or `created_at` after insert.**
Only `status`, `tool_output`, `completed_at`, `error_detail` may be set once, on completion.
Enforce via DB permissions (a role that only has INSERT + a narrowly-scoped UPDATE), not just
application logic.

### 1.3 What every tool implementer must guarantee

- Never call your tool's core logic directly from the router or from another tool — always go
  through `toaa_wrap`.
- Your tool function must be pure w.r.t. side effects visible outside its own domain — i.e. it
  shouldn't itself write audit rows; the wrapper does that.
- Errors must be raised as exceptions, not swallowed — the wrapper catches and records them as
  `status=error`.

<!-- --- BEGIN: toaa — §1.4 working example --- -->

### 1.4 Working example & implementation diffs

A runnable echo tool wrapped end-to-end lives at
`backend/avexie/toaa/example_tool.py` with tests in
`backend/avexie/toaa/test_example_tool.py`. Copy this pattern when
wrapping your own tool.

The *implemented* `toaa_wrap` (in `backend/avexie/toaa/wrapper.py`)
differs from the illustrative §1.1 signature in three ways:

| §1.1 (illustrative) | Implementation | Why |
|---|---|---|
| `tool_fn: Callable[[dict], dict]` | `tool_fn: Callable[..., Awaitable]` | OWUI tools are async and accept `**kwargs`, not a single dict |
| Returns `ToaaResult` | Returns `Any` (the raw tool result) | Downstream code expects the unwrapped value; the audit record is a side-effect |
| Sync function | `async def` | All OWUI tool dispatch is async |

The wrapper calls `tool_fn(**tool_input)` internally, so tool authors
do not need to change their function signatures.

Integration guide for wiring `toaa_wrap` into the three middleware
dispatch sites: `backend/avexie/toaa/INTEGRATION.md`.

<!-- --- END: toaa --- -->

---

## 2. Tool registration interface

How a new tool becomes callable by the router and automatically wrapped by TOAA.

```python
# tools/registry.py (owned by whoever lands it first in Wave 1 — likely Person A or D)

@register_tool(
    name="kb_search",              # unique, snake_case, stable — used as tool_name in audit rows
    description="...",             # shown to the model for tool selection
    input_schema={...},            # JSON schema, validated before dispatch
    requires_approval=False,
)
def kb_search(query: str, top_k: int = 5) -> dict:
    ...
```

- Registration happens once at `get_tools()` — matching the OWUI extension point named in the
  plan doc (§4.3). Nobody bypasses this to register a tool ad hoc elsewhere.
- `name` is permanent once used in any committed audit row — do not rename a tool after other
  modules depend on it; add a new tool instead if the shape changes materially.
- Every registered tool is automatically passed through `toaa_wrap` by the registry — tool
  authors never call `toaa_wrap` themselves.

**Tool names locked so far** (add here as agreed, don't invent new ones mid-module):
`run_code`, `kb_search`, `generate_docx`, `generate_pptx`, `generate_xlsx`, `vlm_extract`,
`file_read`, `file_write`.

---

## 3. Router → tool dispatch contract

```python
class RouteDecision(TypedDict):
    lane: Literal["reasoning", "coding", "vision"]
    model_tag: str            # e.g. "gpt-oss-120b", "qwen3-coder-30b"
    signal: str                # what triggered the route — for audit/debug, e.g. "attachment=image/png"
    confidence: Literal["deterministic", "classified"]
```

- Router emits a `RouteDecision`; it does not call tools directly. The chat/orchestration layer
  reads `RouteDecision.model_tag` to pick which model handles the turn.
- Router must be side-effect-free except for the Ollama `keep_alive` hot-swap call — no DB writes,
  no audit rows (the model swap itself is not a tool call in the TOAA sense; log it separately if
  you want it visible, but don't overload `toaa_audit_record` with it).
- Anyone consuming routing decisions should treat `signal` as debug/display-only, never parse it
  programmatically — it's a human-readable string, not a structured field.

---

## 4. RAG / PLAYBOOK contract

```python
def playbook_ingest(doc_id: str, source_path: str, metadata: dict) -> IngestResult: ...
def playbook_query(query: str, top_k: int = 5, filters: dict | None = None) -> list[Chunk]: ...

class Chunk(TypedDict):
    doc_id: str
    text: str
    score: float
    source_metadata: dict   # e.g. {"filename": ..., "ingested_at": ...}
```

- `playbook_query` is what `kb_search` (the tool) calls internally — `kb_search` is the
  TOAA-audited public interface; `playbook_query` is the internal RAG-module function. Don't let
  other modules call `playbook_query` directly — always go through the `kb_search` tool so it's
  audited.
- Ingestion (scanned or born-digital) reuses the same two-tier pipeline as live-query attachments
  (fast parse → VLM fallback) — do not build a second ingestion path.

---

## 5. File store contract (Wave 2 — draft, confirm before building)

- Object storage (e.g. self-hosted MinIO) holds bytes; Postgres holds metadata. No direct
  filesystem sharing between users — all access mediated by the app.
- Scope is either `user:<user_id>` or `org` — every file row has exactly one scope.

```sql
-- files table (draft, owned by db/ once assigned)
id uuid PK
scope text            -- 'user:<id>' or 'org'
filename text
content_type text
size_bytes bigint
checksum text
storage_key text       -- key/path in object store
uploaded_by text
created_at timestamptz
```

- `file_read` / `file_write` tools are the only sanctioned access path — no tool or module talks
  to the object store directly.

---

## 6. Shared-file editing convention

In `docker-compose.yml`, `requirements.txt`, `.env.example`, and the app's startup/init module,
every addition is wrapped in a comment block naming the owner and module:

```yaml
# --- BEGIN: rag (ChromaDB) — Person B ---
chromadb:
  image: ...
# --- END: rag ---
```

Never edit inside another owner's `BEGIN`/`END` block. If a shared file gets too contentious,
split it (`docker-compose.rag.yml`, `docker-compose.sandbox.yml`, etc. merged via `-f` flags)
rather than fighting over one file.

---

## 7. Open items — do not build against these until resolved

- [ ] Confirm current repo state: is it already the OWUI v0.6.5 fork with cloud connectors
      stripped, or is that still pending? (Blocks Phase 0.)
- [ ] File store: MinIO vs. alternative — final decision + owner assignment.
- [ ] Env var naming convention (e.g. `AVEXIE_DB_URL` vs `POSTGRES_URL`) — pick one prefix scheme.
- [ ] `toaa_ticket` (plan layer) schema — deferred to Phase 8, not needed for Wave 1/2.

---

## 8. Change process for this document

Any change to §1–§5 (the actual interface shapes) requires a quick sync between whoever owns the
producing module and whoever owns the consuming module — not a unilateral edit. Additions to
§0/§6 (ownership, conventions) can be made freely as the team grows the module list.
