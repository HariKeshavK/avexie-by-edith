# AVEXIE Architecture Map & Audit Report

This document maps the core architectural components of the AVEXIE codebase (a fork of Open WebUI) as of the current read-only audit. It includes verified file paths, operational details, and code excerpts as evidence.

---

## 1. DATABASE LAYER

### SQLAlchemy & Database Layer Definitions
The primary database layer, database helper functions, connection wrappers, and engine instantiation are defined in:
* [`backend/avexie/internal/db.py`](file:///c:/Users/Dell/Documents/avexie/backend/avexie/internal/db.py)

### SQLite vs. PostgreSQL Configuration Switch
The switch between SQLite/SQLCipher and PostgreSQL is determined dynamically by evaluating the database URL scheme.
* **Switch check:** It inspects the `SQLALCHEMY_DATABASE_URL` string for the presence of the substring `'sqlite'`.
* **Definition location:** [`backend/avexie/internal/db.py`](file:///c:/Users/Dell/Documents/avexie/backend/avexie/internal/db.py#L319-L322) and [`backend/avexie/internal/db.py#L406-L408`](file:///c:/Users/Dell/Documents/avexie/backend/avexie/internal/db.py#L406-L408)

#### Code Excerpt (Connection Switch in `db.py`):
```python
if "sqlite" in SQLALCHEMY_DATABASE_URL:
    engine = create_engine(
        SQLALCHEMY_DATABASE_URL,
        pool_pre_ping=True,
        connect_args={"check_same_thread": False},
    )
else:
    engine = create_engine(
        SQLALCHEMY_DATABASE_URL,
        pool_pre_ping=True,
        pool_size=DB_POOL_SIZE,
        max_overflow=DB_MAX_OVERFLOW,
    )
```

### Models & Migration Layer
* **Models location:** Individual SQLAlchemy models (e.g. `User`, `Chat`, `File`, `Tool`) are defined as declarative models using `Base` in the directory:
  * [`backend/avexie/models/`](file:///c:/Users/Dell/Documents/avexie/backend/avexie/models/)
* **Migration tool:** **Alembic** is used to manage database schema migrations.
  * Configuration: [`backend/avexie/alembic.ini`](file:///c:/Users/Dell/Documents/avexie/backend/avexie/alembic.ini)
  * Migration scripts: [`backend/avexie/migrations/`](file:///c:/Users/Dell/Documents/avexie/backend/avexie/migrations/)

#### Code Excerpt (Example Table Definition — `User` in `backend/avexie/models/users.py#L45-L54`):
```python
class User(Base):  # identity & profile
    """One row per registered account — profile, role, and settings."""

    __tablename__: str = 'user'  # Identity & Credentials
    id = Column(String, primary_key=True, unique=True)  # unique user id
    email = Column(String, unique=True)  # user email address
    username = Column(String(50), nullable=True)  # custom handle
    role = Column(String, default='pending')  # permissions role
    name = Column(String, nullable=False)  # display name
```

---

## 2. VECTOR DB / RAG PIPELINE

### Vector DB Abstractions & Clients
AVEXIE implements vector database client drivers in:
* [`backend/avexie/retrieval/vector/dbs/`](file:///c:/Users/Dell/Documents/avexie/backend/avexie/retrieval/vector/dbs/) (containing `chroma.py`, `pgvector.py`, `qdrant.py`, `milvus.py`, `opensearch.py`, etc.)
* The base async client class is defined in: [`backend/avexie/retrieval/vector/async_client.py`](file:///c:/Users/Dell/Documents/avexie/backend/avexie/retrieval/vector/async_client.py)

### Config Switch & Instantiation Factory
* The vector database type is selected via the `VECTOR_DB` environment variable (defaulting to `'chroma'`), defined in:
  * [`backend/avexie/config.py#L431`](file:///c:/Users/Dell/Documents/avexie/backend/avexie/config.py#L431)
* The dynamic instantiation factory is defined in:
  * [`backend/avexie/retrieval/vector/factory.py#L12`](file:///c:/Users/Dell/Documents/avexie/backend/avexie/retrieval/vector/factory.py#L12)

#### Code Excerpt (Factory in `factory.py`):
```python
class Vector:
    @staticmethod
    def get_vector(db_type: str, *args, **kwargs):
        match db_type:
            case "chroma":
                from avexie.retrieval.vector.dbs.chroma import ChromaClient
                return ChromaClient(*args, **kwargs)
            case "milvus":
                from avexie.retrieval.vector.dbs.milvus import MilvusClient
                return MilvusClient(*args, **kwargs)
            # ... additional case clauses for qdrant, pgvector, opensearch
```

### End-to-End Document Ingestion Flow
1. **Upload & Parse Entrypoints:** The endpoints `process_file` and `process_files_batch` inside [`backend/avexie/routers/retrieval.py`](file:///c:/Users/Dell/Documents/avexie/backend/avexie/routers/retrieval.py) receive file uploads.
2. **Text Extraction:** Ingestion is delegated to `Loader.aload` async thread wrapper in [`backend/avexie/retrieval/loaders/main.py`](file:///c:/Users/Dell/Documents/avexie/backend/avexie/retrieval/loaders/main.py#L313), which uses the `_get_loader` function to select the appropriate parser.
3. **Chunking / Splitting:** Retrieved documents are split into chunks using `split_text` (e.g. `RecursiveCharacterTextSplitter`) from [`backend/avexie/retrieval/utils.py`](file:///c:/Users/Dell/Documents/avexie/backend/avexie/retrieval/utils.py).
4. **Embedding Generation:** Embeddings are generated using the configured embedding function (`request.app.state.EMBEDDING_FUNCTION`) defined in `config.py` and resolved in `main.py`.
5. **Vector Storage:** Chunks and embeddings are stored by invoking `ASYNC_VECTOR_DB_CLIENT.insert` / `ASYNC_VECTOR_DB_CLIENT.upsert` on the active vector client instance.

### Wired Content-Extraction Engines
AVEXIE has integrated support for several extraction engines in [`backend/avexie/retrieval/loaders/main.py#L500-L557`](file:///c:/Users/Dell/Documents/avexie/backend/avexie/retrieval/loaders/main.py#L500-L557):
* **Tika (`TikaLoader`):** Self-hosted/API-based local server parser, enabled if `TIKA_SERVER_URL` is set.
* **Docling (`DoclingLoader`):** Self-hosted or API-based PDF parser, enabled if `DOCLING_SERVER_URL` is set.
* **MinerU (`MinerULoader`):** Self-hosted or cloud parser supporting local API mode and cloud API task-based mode.
* **PaddleOCR-vl (`PaddleOCRVLLoader`):** External layout-parsing API model, enabled if `PADDLEOCR_VL_BASE_URL` and `PADDLEOCR_VL_TOKEN` are set.
* **Local Fallbacks:** PyPDFLoader, CSVLoaderWithSummary, BSHTMLLoader, Docx2txtLoader, and unstructured package loaders for PPTX, XLS, XML, EPub, MSG, and ODT.

### Scanned vs. Born-Digital Routing Logic
**No scanned vs. born-digital routing exists.**
The system routes all files directly through the globally configured `CONTENT_EXTRACTION_ENGINE` or falls back to local file loaders (such as `PyPDFLoader` with optional image extraction flags). There is no automated scanning detection or routing.

### Extension Points (Wave 1 Hook)
* **Document Extraction Hook:** [`_get_loader` in `backend/avexie/retrieval/loaders/main.py`](file:///c:/Users/Dell/Documents/avexie/backend/avexie/retrieval/loaders/main.py#L500)
* **Vector DB Hook:** [`Vector.get_vector` factory in `backend/avexie/retrieval/vector/factory.py`](file:///c:/Users/Dell/Documents/avexie/backend/avexie/retrieval/vector/factory.py#L12)

---

## 3. TOOLS FRAMEWORK

### Tools, Filters, and Pipes Files
* Tools/Pipes/Filters live under:
  * [`backend/avexie/tools/`](file:///c:/Users/Dell/Documents/avexie/backend/avexie/tools/) (e.g., `builtin.py` and `knowledge_fs.py`)
  * Runtime utilities: [`backend/avexie/utils/tools.py`](file:///c:/Users/Dell/Documents/avexie/backend/avexie/utils/tools.py) and [`backend/avexie/utils/filter.py`](file:///c:/Users/Dell/Documents/avexie/backend/avexie/utils/filter.py)

### Tool Interface Specification
A tool is represented as a plain Python function.
* The function docstring is dynamically parsed to construct the JSON schema for model function calling.
* It supports injection of system arguments prefixed/suffixed with `__` (e.g. `__request__`, `__user__`, `__messages__`, `__files__`).

#### Code Excerpt (Example Built-in Tool Interface in `backend/avexie/tools/builtin.py#L820-L830`):
```python
async def delete_memory(
    memory_id: str,
    __request__: Request = None,
    __user__: dict = None,
) -> str:
    """
    Delete a saved memory by its ID.

    :param memory_id: The ID of the memory to delete
    :return: Confirmation that the memory was deleted
    """
```

### Dispatch & Execution Choke Point
The single choke point where tools are dispatched and executed is the `execute_tool_call` function.
* **Execution Location:** Enclosed inside `process_chat_payload` in [`backend/avexie/utils/middleware.py#L5619-L5657`](file:///c:/Users/Dell/Documents/avexie/backend/avexie/utils/middleware.py#L5619-L5657).
* Pre-approved/drained tool calls are also executed via `execute_tool_call_for_output` in [`backend/avexie/utils/middleware.py#L3141`](file:///c:/Users/Dell/Documents/avexie/backend/avexie/utils/middleware.py#L3141).

#### Code Excerpt (Execution Hook in `middleware.py`):
```python
async def execute_tool_call(tool_call):
    name = tool_call.get('function', {}).get('name', '')
    params = parse_tool_params(tool_call)
    # ... resolution and validations ...
    try:
        if direct_tool:
            result = await event_caller({
                'type': 'execute:tool',
                'data': {
                    'id': str(uuid4()),
                    'name': name,
                    'params': params,
                    'server': tool.get('server', {}),
                    'session_id': metadata.get('session_id'),
                },
            })
        else:
            function = await get_updated_tool_function(
                function=tool['callable'],
                extra_params={
                    '__messages__': form_data.get('messages', []),
                    '__files__': metadata.get('files', []),
                },
            )
            result = await function(**params)
    except Exception as e:
        result = {'error': str(e)}
    return params, result, tool, tool_type, direct_tool
```

---

## 4. AUDIT / LOGGING

### HTTP/Action Audit Middleware
* **Location:** [`backend/avexie/utils/audit.py`](file:///c:/Users/Dell/Documents/avexie/backend/avexie/utils/audit.py)
* **Middleware Class:** `AuditLoggingMiddleware` (inherits from standard ASGI application context).

### Logging Scope & Destination
* **Scope:** It captures request URIs, HTTP verbs, response status codes, client IPs, user agents, user information, and raw request/response bodies (up to a size limit defined by `MAX_BODY_LOG_SIZE`).
* **Destination:** **Logs to files/console only.** It writes structured log payloads using Loguru's `logger.bind(auditable=True)`. It **does NOT** write to database tables.

---

## 5. FILE STORAGE ABSTRACTION

### Storage-Provider Abstraction & Interface
* **Location:** [`backend/avexie/storage/provider.py`](file:///c:/Users/Dell/Documents/avexie/backend/avexie/storage/provider.py)
* **Abstraction Class:** `LocalStorageProvider` (aliased globally as `Storage = LocalStorageProvider()`).
* **Interface Methods:** `upload_file`, `get_file`, `delete_file`, and `delete_all_files`.

#### Code Excerpt (Storage Interface in `provider.py`):
```python
class LocalStorageProvider:
    @staticmethod
    def upload_file(file: BinaryIO, filename: str, tags: Dict[str, str]) -> Tuple[bytes, str]:
        # ... writes content to UPLOAD_DIR ...
        return contents, file_path

    @staticmethod
    def get_file(file_path: str) -> str:
        return file_path

    @staticmethod
    def delete_file(file_path: str) -> None:
        # ... removes file from local storage ...
```

### Access Control Scoping Model
File scope permission checks are defined in [`backend/avexie/utils/access_control/files.py`](file:///c:/Users/Dell/Documents/avexie/backend/avexie/utils/access_control/files.py). Direct ownership (`file.user_id == user.id`) is verified first. Beyond direct ownership, `has_access_to_file` evaluates access by checking:
1. **Knowledge Bases:** If the file is attached to a knowledge base owned by the user or shared with the user's groups (resolved via `AccessGrants.has_access`).
2. **Workspace Models:** If a model shared across the workspace links the file.
3. **Channels:** If the file is attached to a channel where the user is a member.
4. **Chats:** If the file is attached to a shared chat session the user has read access to.

---

## 6. MODEL SELECTION

### Chat Model Selection Execution
Model routing and dispatch happens within `process_chat_payload` inside [`backend/avexie/utils/middleware.py`](file:///c:/Users/Dell/Documents/avexie/backend/avexie/utils/middleware.py). The user's requested model ID is matched against the database `Models` registry, and requests are formatted/forwarded to the corresponding API backend (e.g. Ollama or OpenAI).

### Pre-dispatch Filter/Pipelines
Before the payload is submitted to the chosen model, pre-dispatch "inlet" functions are executed.
* **Definition Location:** [`backend/avexie/utils/filter.py`](file:///c:/Users/Dell/Documents/avexie/backend/avexie/utils/filter.py)
* **Function:** `process_filter_functions` with `filter_type='inlet'` evaluates active filters.

### Filter Function Registration & Signature
* **Registration:** Filters are uploaded as custom functions in the user interface and compiled into dynamic Python modules. They register by defining an `inlet` (pre-dispatch) or `outlet` (post-dispatch) function.
* **Receives:** The handler receives the request `body` dictionary containing the message structure, and can inspect context parameters such as `__user__` and `__id__` (resolved via `get_filter_params`).
* **Returns:** The function returns the modified/enriched `body` dictionary or raises an exception to block dispatch.

---

## 7. NAMING CONVENTIONS

* **Python Package Name:** Internally, the code has been refactored. The primary package namespace is named **`avexie`** (e.g., imports are structured like `from avexie.env import ...` or `import avexie.internal.db`).
* **Directory structure:** The top-level package folder is [`backend/avexie/`](file:///c:/Users/Dell/Documents/avexie/backend/avexie/).

---

## 8. CLOUD-DEPENDENCY AUDIT

Here is the status of external integrations and cloud services in the codebase:

| Dependency Area | Status | Operational Notes & Verification |
| :--- | :--- | :--- |
| **Cloud Model Connectors** (OpenAI, Anthropic, GroqCloud, Mistral, OpenRouter) | **LIVE** (Unconfigured) | Connectors are fully active. Dedicated request/response translation utilities for Anthropic are implemented in `backend/avexie/utils/anthropic.py` (triggered when the URL contains `'api.anthropic.com'`). |
| **External Web-Search** (Google PSE, Bing, Brave, Tavily) | **REMOVED** | Staged endpoints and logic inside `backend/avexie/routers/retrieval.py` (`search_web` and `process_web_search`) have been replaced with stubs raising `Web search is not available in this build`. |
| **Cloud Image Generation** (OpenAI DALL·E, Gemini) | **LIVE** (Disabled) | Implemented in `backend/avexie/routers/images.py`. Toggled off by default (`image_generation.enable: False` in `config.py`), but fully functional if keys are supplied. |
| **Cloud OCR/Document Engines** (Mistral OCR, Azure Document Intelligence) | **REMOVED** | Not present. The RAG pipeline relies on local parsers or Tika/Docling/MinerU/PaddleOCR endpoints. |
| **Cloud File Pickers** (Google Drive, OneDrive/SharePoint) | **REMOVED** | Code deleted. Verifying comment in `backend/avexie/config.py`: `# Google Drive and OneDrive integrations have been removed.` |
| **OAuth / OIDC SSO Login** | **REMOVED** | OAuth and LDAP have been stripped. Verifying comment in `backend/avexie/config.py`: `# OAuth/OIDC SSO login providers ... have been removed. Only local username/password authentication is supported.` |
| **Telemetry / Update Checks** | **DISABLED** | Telemetry tracking is absent. The version update check is hardcoded to `False` in `backend/avexie/main.py`: `'enable_version_update_check': False`. |
