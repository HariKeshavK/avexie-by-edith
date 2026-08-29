# AVEXIE — ARCHITECTURE MAP

> Audit date: 2026-08-30  
> Repo: avexie-by-edith (Open WebUI fork)  
> Auditor: automated (Claude Code)

---

## FINDINGS: Cloud dependencies NOT yet removed

The following cloud-dependent code paths are **still reachable** (status: LIVE).
They are not footnotes — each one means a user could route data to external
cloud APIs without any code change, just by setting an env var.

| Component | Status | Evidence |
|---|---|---|
| **OpenAI-compatible proxy** | **LIVE** | `backend/avexie/config.py:264` — `ENABLE_OPENAI_API` defaults to `True`. The router at `backend/avexie/routers/openai.py` proxies to whatever `OPENAI_API_BASE_URL` is configured. Any OpenAI-compatible cloud provider (OpenAI, Groq, Mistral, OpenRouter, etc.) is reachable with one env var. |
| **Anthropic connector** | **LIVE** | `backend/avexie/utils/anthropic.py` — live code that fetches models from and relays requests to `api.anthropic.com`. Imported and used by the OpenAI router. |

All other cloud dependencies are either REMOVED or DISABLED (see §8 for the full breakdown).

---

## 1. DATABASE

### SQLAlchemy / DB layer

Defined in **`backend/avexie/internal/db.py`**. Creates both a sync engine (startup/migrations)
and an async engine (all runtime operations). Key exports:

- `Base` — declarative base (`MetaData(schema=DATABASE_SCHEMA)`)
- `SessionLocal` / `ScopedSession` — sync sessions for startup config loading
- `AsyncSessionLocal` — async session factory bound to `async_engine`
- `get_async_session()` — FastAPI `Depends()` generator
- `get_async_db()` / `get_async_db_context()` — async context managers
- `JSONField` — portable TEXT-backed JSON column (works on both SQLite and Postgres)

### Postgres vs SQLite config switch

Defined in **`backend/avexie/env.py`** (lines 266–296):

```python
DATABASE_URL = os.getenv('DATABASE_URL', f'sqlite:///{DATA_DIR}/webui.db')
```

If all of `DATABASE_TYPE`, `DATABASE_USER`, `DATABASE_HOST`, `DATABASE_PORT`, `DATABASE_NAME`
are set, it constructs `{db_type}://{cred}@{host}:{port}/{name}`. `postgres://` is auto-normalized
to `postgresql://`. In `db.py`, the engine branches on URL prefix: `sqlite+sqlcipher://` → SQLCipher
creator; `sqlite` → standard SQLite with PRAGMAs; else → Postgres with connection pooling. Async
URL derived via `_make_async_url()` which swaps drivers (`sqlite+aiosqlite`, `postgresql+psycopg`).

### Models / tables

All 26 model files live in **`backend/avexie/models/`**. Each file co-locates:
1. The SQLAlchemy ORM class (extends `Base`)
2. Pydantic schemas for validation/serialization
3. A `*Table` class (repository pattern) with async CRUD methods
4. A module-level singleton (e.g., `Users = UsersTable()`)

### Migration tool

**Alembic.** Config at `backend/avexie/alembic.ini`; runner at `backend/avexie/migrations/env.py`.
67 migration versions in `backend/avexie/migrations/versions/`. The `ENABLE_DB_MIGRATIONS` env var
gates whether migrations run at startup.

### Example table definition

From `backend/avexie/models/users.py` (lines 45–79):

```python
class User(Base):
    __tablename__: str = 'user'
    id = Column(String, primary_key=True, unique=True)
    email = Column(String, unique=True)
    username = Column(String(50), nullable=True)
    role = Column(String, default='pending')
    name = Column(String, nullable=False)
    profile_image_url = Column(Text)
    profile_banner_image_url = Column(Text, nullable=True)
    bio = Column(Text, nullable=True)
    gender = Column(Text, nullable=True)
    date_of_birth = Column(Date, nullable=True)
    timezone = Column(String, nullable=True)
    presence_state = Column(String, nullable=True)
    status_emoji = Column(String, nullable=True)
    status_message = Column(Text, nullable=True)
    status_expires_at = Column(BigInteger, nullable=True)
    info = Column(JSON, nullable=True)
    variables = Column(JSON, nullable=True)
    settings = Column(JSON, nullable=True)
    oauth = Column(JSON, nullable=True)
    scim = Column(JSON, nullable=True)
    last_active_at = Column(BigInteger)
    updated_at = Column(BigInteger)
    created_at = Column(BigInteger)
```

**Extend here:** `backend/avexie/models/` — add a new file with a `Base`-extending class, a
companion Pydantic model and `*Table` repository, then generate an Alembic migration.

---

## 2. VECTOR DB / RAG

### Vector-DB abstraction

Three files:

- **`backend/avexie/retrieval/vector/type.py`** — `VectorType(StrEnum)` enumerates 13 backends:
  `milvus`, `mariadb-vector`, `qdrant`, `chroma`, `pinecone`, `elasticsearch`, `opensearch`,
  `pgvector`, `oracle23ai`, `s3vector`, `weaviate`, `opengauss`, `valkey`.
- **`backend/avexie/retrieval/vector/main.py`** — `VectorDBBase(ABC)` is the interface. Abstract
  methods: `has_collection`, `delete_collection`, `insert`, `upsert`, `search`, `query`, `get`,
  `delete`, `reset`. Non-abstract `hybrid_search` returns `None` by default. Data types:
  `VectorItem`, `GetResult`, `SearchResult` (Pydantic).
- **`backend/avexie/retrieval/vector/factory.py`** — `Vector.get_vector(vector_type)` static
  factory using a `match` statement. Module-level singleton:
  `VECTOR_DB_CLIENT = Vector.get_vector(VECTOR_DB)`.

### Config switch (ChromaDB)

In **`backend/avexie/config.py`** (line 431):

```python
VECTOR_DB = os.getenv('VECTOR_DB', 'chroma')
```

ChromaDB is the default. Per-backend config (e.g. `PGVECTOR_DB_URL`) read from env vars
in the same file.

### Document ingestion end-to-end

1. **`backend/avexie/retrieval/utils.py`** — `build_loader_from_config(request, config)` constructs
   a `Loader` instance using the `CONTENT_EXTRACTION_ENGINE` config key.
2. **`backend/avexie/retrieval/loaders/main.py`** — `Loader` class. Its `load()` / `aload()` methods
   call `_get_loader()` to pick the document-loading backend by engine name + file extension, then
   run `.load()` and post-process with `ftfy.fix_text`.
3. Routers (`retrieval.py`, `files.py`) call `build_loader_from_config`, feed resulting `Document`
   list into `VECTOR_DB_CLIENT`.

### Content-extraction engines wired in

`CONTENT_EXTRACTION_ENGINE` env var (config.py line 769, default empty string) selects the engine:

| Engine value | Loader class | Classification |
|---|---|---|
| `""` (default) | Built-in per-extension: `PyPDFLoader`, `Docx2txtLoader`, `CSVLoader`, `BSHTMLLoader`, `TextLoader`, plus optional `unstructured` for rst/xml/epub/doc/xls/pptx/msg/odt | **Local-only** |
| `"tika"` | `TikaLoader` (`TIKA_SERVER_URL`, default `http://tika:9998`) | **Local-only** (self-hosted) |
| `"docling"` | `DoclingLoader` (`DOCLING_SERVER_URL`, default `http://docling:5001`) | **Local-only** (self-hosted) |
| `"mineru"` (local mode) | `MinerULoader` (`MINERU_API_URL`, `api_mode=local`) | **Local-only** (self-hosted) |
| `"mineru"` (cloud mode) | `MinerULoader` (`api_mode=cloud`) | **Cloud-dependent** |
| `"paddleocr_vl"` | `PaddleOCRVLLoader` (`PADDLEOCR_VL_BASE_URL`) | **Local-only** (typically self-hosted) |

**Mistral OCR** and **Azure Document Intelligence** are **NOT wired in**. They appear only in the
migration file `3ff2c63645b8_reshape_config_to_per_key_rows.py` as legacy config key mappings.
No loader class, no active config var exists for either.

### Scanned vs born-digital routing

**No existing logic.** `_get_loader()` routes entirely by engine name and file extension. A PDF
always goes to whichever single engine is configured — there is no content-inspection step
(checking for extractable text layers) that would route scanned documents to OCR while keeping
born-digital documents on a lighter path. The system is one-engine-for-all.

**Extend here:** `backend/avexie/retrieval/loaders/main.py` — `_get_loader()` method of `Loader`
class. Add born-digital vs scanned routing logic here.

---

## 3. TOOLS FRAMEWORK

### Where Tools/Filters/Pipes live

- **User-authored tools** — stored in the `tool` DB table, modeled at
  `backend/avexie/models/tools.py`. The `Tool` SQLAlchemy model holds `content` (Python source)
  and `specs` (OpenAPI-style function specs).
- **Functions (Filters/Pipes/Actions)** — stored in the `function` DB table, modeled at
  `backend/avexie/models/functions.py`. The `type` column distinguishes `"filter"`, `"pipe"`,
  `"action"`.
- **Built-in tools** — plain `async def` functions in `backend/avexie/tools/builtin.py` (~4300
  lines, ~60 functions). Imported by name into `backend/avexie/utils/tools.py` (lines 47–98).

### Tool interface

Convention-based — no decorator or base class. A tool is any public async function with typed
parameters and a `:param`-style docstring. The framework converts each function to a Pydantic model
via `convert_function_to_pydantic_model()` (`utils/tools.py:836`), then to an OpenAI function spec.
Magic parameters prefixed with `__` (e.g., `__request__`, `__user__`, `__event_emitter__`) are
injected at call time and stripped from the spec.

Example from `builtin.py`:

```python
async def get_current_timestamp(
    __request__: Request = None,
    __user__: dict = None,
) -> str:
    """Get the current date and time as a formatted string.
    :return: Current timestamp as ISO 8601 string
    """
```

### Tool dispatch / execution point (the choke point)

**`backend/avexie/utils/middleware.py`**, lines 1367–1481. The `tool_call_handler` inner function:

```python
async def tool_call_handler(tool_call):
    tool_function_name = tool_call.get('name', None)
    tool_function_params = tool_call.get('parameters', {})
    tool = tools[tool_function_name]
    # ...
    if tool.get('direct', False):
        tool_result = await event_caller({...})      # direct/MCP tool
    else:
        tool_function = tool['callable']
        tool_result = await tool_function(**tool_function_params)  # <-- THE DISPATCH
```

This is the single choke point where **every** tool call (built-in, user-authored, MCP, direct)
passes through. Line 1406–1407 is the exact call site for non-direct tools. Any audit wrapper
(TOAA) needs to sit here.

The tools dict is assembled earlier in the same function:
- Line ~2911: `get_tools()` loads user-authored DB tools
- Line ~3005: `get_builtin_tools()` injects platform built-ins
- Line ~2923: MCP tools merged in

### Example built-in tool

`backend/avexie/tools/builtin.py` contains ~60 built-in tools. Good pattern-match targets:
`get_current_timestamp` (minimal), `search_memories` (complex with multiple params),
`execute_code` (side-effecting).

**Extend here:** `backend/avexie/utils/middleware.py:1406` — wrap the `tool_function(**params)`
call with TOAA audit logic. For new built-in tools, add to `backend/avexie/tools/builtin.py` and
import in `backend/avexie/utils/tools.py`.

---

## 4. AUDIT / LOGGING

### HTTP-level audit middleware

**Exists** at **`backend/avexie/utils/audit.py`** as `AuditLoggingMiddleware` (pure-ASGI).
Registered conditionally in `backend/avexie/main.py` (lines 688–696):

```python
if audit_level != AuditLevel.NONE:
    app.add_middleware(
        AuditLoggingMiddleware,
        audit_level=audit_level,
        excluded_paths=AUDIT_EXCLUDED_PATHS,
        included_paths=AUDIT_INCLUDED_PATHS,
        audit_get_requests=ENABLE_AUDIT_GET_REQUESTS,
        max_body_size=MAX_BODY_LOG_SIZE,
    )
```

**Audit levels** (`AuditLevel` enum): `NONE`, `METADATA`, `REQUEST`, `REQUEST_RESPONSE`. Each
level captures progressively more data.

**What it logs** (`AuditLogEntry` dataclass): UUID, user dict (id/name/email/role), audit level,
HTTP method, URI, user agent, source IP, request body, response body, status code.

**Where it writes:** **Loguru only** (logger bound with `auditable=True`). There is **no database
table** for audit entries and no structured file sink. It relies on whatever Loguru sinks are
configured externally.

**Key behaviors:**
- Always logs auth endpoints (signin/signout/signup) regardless of path filters
- Skips unauthenticated requests
- Supports whitelist/blacklist path filtering with compiled regex
- Redacts `"password"` fields in request bodies
- Default audited methods: PUT, PATCH, DELETE, POST; GET is opt-in

### Action-level audit

**Does not exist.** There is no per-tool-call audit trail. The HTTP middleware sees HTTP verbs,
not semantic actions (tool calls, file shares, permission grants).

**Extend here:** `backend/avexie/utils/audit.py` for HTTP-level; the TOAA audit wrapper is
net-new and should hook into `backend/avexie/utils/middleware.py:1406` (the tool dispatch point).

---

## 5. FILE STORAGE

### Storage provider abstraction

**`backend/avexie/storage/provider.py`** — contains `LocalStorageProvider` with methods:
`upload_file`, `get_file`, `delete_file`, `delete_all_files`. Reads entire file into memory,
writes to `UPLOAD_DIR`. **No abstract base class**, no S3/GCS/Azure provider — the singleton
`Storage = LocalStorageProvider()` (line 57) is the only implementation.

### File model

**`backend/avexie/models/files.py`** — `file` table with columns: `id`, `user_id` (owner), `hash`,
`filename`, `path`, `data` (JSON), `meta` (JSON), `created_at`, `updated_at`. Notably: **no
`workspace_id`**, **no `visibility`** column, **no `scope`** column.

### Scoping model (user vs group vs workspace)

Files are **personal by default** (`user_id` marks ownership). There is no column or flag that
marks a file as "shared" or "workspace-level."

Sharing is **indirect**, resolved at query time via
**`backend/avexie/utils/access_control/files.py`** (`has_access_to_file`). Access is granted
transitively when a file is attached to a shared resource:

1. **Knowledge bases** — if the file belongs to a KB the requesting user owns or has an access
   grant for
2. **Workspace models** — if the file is attached to a model the user has access to
3. **Channels** — if the file is linked to a channel the user is a member of
4. **Shared chats** — if the file appears in a chat with read access grants

The `access_grant` table (`backend/avexie/models/access_grants.py`) supports
`resource_type = "file"` but direct file-level sharing is structurally supported yet effectively
unused — all sharing routes through parent objects.

**Extend here:** `backend/avexie/storage/provider.py` — add a `StorageProvider` ABC so
S3/MinIO backends can be plugged in. For org-level scoping, add a `scope` column to the `file`
table.

---

## 6. MODEL SELECTION

### Where model dispatch happens

**`backend/avexie/utils/chat.py`**, function `generate_chat_completion` (line 151). Flow:

1. `model_id = form_data['model']`; model dict fetched from `request.app.state.MODELS`
2. If `request.state.direct` → `generate_direct_chat_completion`
3. If `model.get('owned_by') == 'arena'` → random choice from arena model pool, recurse with
   `bypass_filter=True`
4. Backend dispatch (lines 282–299):
   - `model.get('pipe')` → `generate_function_chat_completion` (plugin/pipe model)
   - `model.get('owned_by') == 'ollama'` → Ollama endpoint with payload conversion
   - Otherwise → OpenAI-compatible endpoint

### Filter extension point

**`backend/avexie/utils/filter.py`** implements the full filter pipeline (inlet → outlet).

**Registration:** Filters are Python function-plugins stored in the DB as `Function` records with
`type="filter"`. Discovered by `Functions.get_active_filter_ids()` and associated with models
through `model['info']['meta']['filterIds']` plus global filters.

**Pipeline resolution:** `resolve_filter_pipeline()` (line 56) collects active filters, checks
toggle/enabled state, sorts by `priority` valve, returns ordered list.

**Execution:** `process_filter_functions()` (line 200) iterates sorted filters and calls the
handler matching `filter_type` (`inlet`, `outlet`, or `stream`).

**What a filter receives/returns:**

```python
# A filter module exposes:
def inlet(body: dict, **kwargs) -> dict:
    # body = the chat completion request (messages, model, etc.)
    # kwargs includes __user__, __id__, __model__, etc.
    # Must return the (possibly modified) body dict
    ...

def outlet(body: dict, **kwargs) -> dict:
    # body = the response
    ...
```

Each filter can declare `Valves` (admin-configured) and `UserValves` (per-user) Pydantic models,
hydrated from the DB before the handler runs.

**Extend here:** `backend/avexie/utils/filter.py` — the inlet phase is where a model-selection
filter should run. A filter function can modify `body['model']` to redirect to a different model
before dispatch.

---

## 7. NAMING

### Python package

The package is consistently named **`avexie`**. All imports use `from avexie.xxx import ...`.
No `open_webui` references remain in any Python source under `backend/avexie/`.

### Top-level backend directory

**`backend/avexie/`** — confirmed. There is no `backend/open_webui/` directory.

### Stale references

The only remaining `open_webui` reference is in `backend/start_windows.bat` (a launch script,
not Python source). The rename is complete within the Python package.

**Extend here:** N/A — rename is done. Clean up `start_windows.bat` as a housekeeping item.

---

## 8. CLOUD-DEPENDENCY AUDIT

Full status for each item on the AVEXIE plan's strip list:

### Cloud model connectors (OpenAI/Anthropic/GroqCloud/Mistral/OpenRouter/etc.)

**Status: LIVE**

- `backend/avexie/config.py:264` — `ENABLE_OPENAI_API = os.getenv('ENABLE_OPENAI_API', 'True')`
  defaults to `True`.
- `backend/avexie/routers/openai.py` — full OpenAI-compatible proxy router, active.
- `backend/avexie/utils/anthropic.py` — live Anthropic connector (fetches models from and relays
  requests to `api.anthropic.com`).
- Groq, Mistral, OpenRouter, Cohere have no dedicated connectors but are trivially reachable via
  the generic OpenAI-compatible `OPENAI_API_BASE_URL` mechanism.
- `backend/avexie/routers/ollama.py` — Ollama (local) router is also live.

### External web-search providers (Google PSE/Bing/Brave/Tavily/etc.)

**Status: DISABLED**

- `backend/avexie/retrieval/web/main.py` contains only `get_filtered_results` helper and
  `SearchResult` model — no search-provider implementations.
- `backend/avexie/main.py:2014` hardcodes `'enable_web_search': False` in frontend config.
- `WEB_SEARCH_ENGINE` config key survives as settings plumbing but no provider code exists.
- SearXNG (self-hostable) is also absent.

### Cloud image generation (OpenAI DALL-E, Gemini)

**Status: DISABLED**

- `backend/avexie/config.py:1023` hardcodes `ENABLE_IMAGE_GENERATION = False` (comment: "Images
  (generation removed; only utility constants kept for imports)").
- `backend/avexie/main.py:2019` hardcodes `'enable_image_generation': False`.
- `backend/avexie/routers/images.py` still contains OpenAI image-gen endpoint code and ComfyUI
  imports, but the feature is force-disabled at both config and frontend levels.

### Cloud OCR/document engines (Mistral OCR, Azure Document Intelligence)

**Status: REMOVED**

- No loader class, no active config var, no references in the retrieval package.
- Only appear in migration file `3ff2c63645b8` as legacy config key mappings.
- `config.py:945` states "OpenAI/Azure OpenAI embedding backends have been removed."

### Cloud file pickers (Google Drive, OneDrive/SharePoint)

**Status: REMOVED**

- `config.py:766` explicitly states "Google Drive and OneDrive integrations have been removed."
- Only references are in migration file `3ff2c63645b8` as legacy key mappings.

### OAuth against external IdPs (Google/Microsoft/GitHub login)

**Status: DISABLED**

- `backend/avexie/utils/oauth.py` is a generic OAuth/OIDC client (using authlib) for MCP server
  auth flows — it does not hardcode Google, Microsoft, or GitHub providers.
- Named-provider OAuth keys (Google/Microsoft/GitHub client IDs/secrets) appear only in the
  migration file.
- `config.py:2115` sets `ENABLE_OAUTH_PERSISTENT_CONFIG = False` by default.
- No `OAUTH_PROVIDERS`, `ENABLE_OAUTH_SIGNUP`, or named-IdP configuration variables exist in
  config.py.

### Telemetry / update-check pings

**Status: DISABLED**

- **OpenTelemetry:** `env.py:1168–1172` — "OPENTELEMETRY (removed — kept as False stubs for import
  compatibility)"; `ENABLE_OTEL = False` hardcoded. Packages remain in requirements but
  instrumentation is never activated.
- **Version update check:** `env.py:1108` reads `ENABLE_VERSION_UPDATE_CHECK` (default `true`),
  but `main.py:2000` hardcodes `'enable_version_update_check': False`.
- **Admin analytics:** `config.py:1544` defaults `ENABLE_ADMIN_ANALYTICS` to `True`, but
  `main.py:2027` hardcodes `'enable_admin_analytics': False`.
- **ChromaDB telemetry:** `retrieval/vector/dbs/chroma.py:34` sets `'anonymized_telemetry': False`.
- **Sentry:** No references found anywhere.

### Summary table

| Component | Status | Action needed |
|---|---|---|
| OpenAI-compatible proxy | **LIVE** | Must disable or remove to meet sovereignty claim |
| Anthropic connector | **LIVE** | Must disable or remove to meet sovereignty claim |
| External web search | DISABLED | No action for Wave 1 |
| Cloud image generation | DISABLED | Code present but off; consider removal |
| Cloud OCR (Mistral/Azure) | REMOVED | Clean |
| Cloud file pickers | REMOVED | Clean |
| OAuth external IdPs | DISABLED | Generic OAuth remains for MCP; named IdPs gone |
| Telemetry / update checks | DISABLED | `ENABLE_VERSION_UPDATE_CHECK` env default is `true` — hardcoded off only in frontend response; server-side check may still run |

---

## Conflicts with CONTRACTS.md

| CONTRACTS.md assumption | Actual state | Impact |
|---|---|---|
| **§0 Directory map** references `db/`, `toaa/`, `router/`, `rag/`, `sandbox/`, `tools/`, `filestore/` as top-level directories | These directories **do not exist**. All code lives under `backend/avexie/` with subdirectories `models/`, `routers/`, `retrieval/`, `tools/`, `storage/`, `utils/` | Every directory path in CONTRACTS.md is wrong. Decide whether Wave 1 creates new top-level dirs or works within the existing `backend/avexie/` structure |
| **§1.2** `toaa_audit_record` table | **Does not exist.** Current audit is HTTP-level only, via Loguru (no DB table). No action-level (tool call) audit exists at all | Must be created as a new Alembic migration |
| **§2** `tools/registry.py` with `@register_tool` decorator | **Does not exist.** Tools use a convention-based approach (public async functions, auto-discovered via reflection in `utils/tools.py`). There is no `@register_tool` decorator | Either build the registry as specified, or adapt CONTRACTS.md to match the existing convention-based pattern |
| **§3** `RouteDecision` TypedDict with `lane`, `model_tag`, `signal`, `confidence` fields | **Does not exist.** Model selection is a plain if/elif chain in `utils/chat.py` (`generate_chat_completion`). No `RouteDecision` type, no router module | Must be created net-new |
| **§4** `playbook_ingest` / `playbook_query` / `Chunk` TypedDict | **Do not exist.** RAG uses `VECTOR_DB_CLIENT` singleton directly with `VectorDBBase` interface. The `Chunk` type doesn't exist; results use `SearchResult` Pydantic model | Either wrap existing RAG infra with the CONTRACTS.md interface, or update CONTRACTS.md to reference `VectorDBBase` |
| **§5** `files` table with `scope text` column (`'user:<id>'` or `'org'`) | The actual table is named `file` (singular) and has `user_id` but **no `scope` column**. Sharing is resolved indirectly through parent objects (knowledge bases, channels, models, chats) | Either add a `scope` column via migration, or update CONTRACTS.md to document the indirect-sharing model |
| **§5** Object storage (MinIO) holds bytes | Storage is **local filesystem only** (`LocalStorageProvider`). No S3/MinIO/object-store provider exists | Must be built net-new |
| **§7 Open item**: "is it already the OWUI v0.6.5 fork with cloud connectors stripped?" | **No** — OpenAI proxy and Anthropic connector are still **LIVE** (enabled by default). Cloud connectors have NOT been fully stripped | Phase 0 (cloud stripping) is incomplete. The OpenAI router and Anthropic util must be disabled or removed before sovereignty claims hold |
