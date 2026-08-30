# AVEXIE architecture map (read-only audit)

Scope: repository contents and checked-in defaults inspected on 2026-08-30. `CONTRACTS.md` was not modified.

## Prominent cloud-dependency findings

- **LIVE — cloud model connector framework.** The generic OpenAI-compatible connector is compiled in and enabled by default (`ENABLE_OPENAI_API=True`), and the OpenAI router includes Anthropic translation. It has no endpoint/key by default, but an administrator can make OpenAI, Anthropic-compatible, Groq, Mistral, OpenRouter, etc. reachable by adding endpoint/key configuration; that is configuration, not a code change.
- **LIVE — external cloud-file-picker client code remains.** The shipped chat input imports Google Drive Picker and OneDrive/SharePoint picker helpers. The server-side integration configuration is removed, so they are unconfigured by default; nevertheless the browser-side implementations remain and can contact the providers once supplied with client-side credentials/configuration.
- **LIVE — version-update checking defaults on.** `ENABLE_VERSION_UPDATE_CHECK` defaults to true. The frontend capability is deliberately reported false, but the environment-level default remains an enabled update-check setting; no implementation call site was found in this checkout, so this is an incomplete/ambiguous removal rather than a verified deletion.

Status vocabulary below follows the requested definition: **REMOVED** = executable feature code deleted; **DISABLED** = code present but off by default and requiring explicit reconfiguration; **LIVE** = reachable now or by a trivial configuration toggle.

## 1. DATABASE

The SQLAlchemy layer is in `backend/avexie/internal/db.py`; it defines `Base`, synchronous/async engines and sessions. `backend/avexie/env.py:266-296` builds `DATABASE_URL`, defaulting to SQLite and accepting `DATABASE_TYPE`/credentials for Postgres (or any supplied SQLAlchemy URL). `backend/avexie/internal/db.py:319-364` selects SQLite engine behavior versus the non-SQLite/Postgres path.

```py
# backend/avexie/env.py:266, 285-288
DATABASE_URL = os.getenv('DATABASE_URL', f'sqlite:///{DATA_DIR}/webui.db')
if all(DB_VARS.values()):
    DATABASE_URL = f'{DB_VARS["db_type"]}://{DB_VARS["db_cred"]}@{DB_VARS["db_host"]}:{DB_VARS["db_port"]}/{DB_VARS["db_name"]}'
```

Models/tables live in `backend/avexie/models/*.py` (for example `users.py`, `files.py`, `knowledge.py`, `functions.py`, `access_grants.py`). The migration tool is **Alembic**: configuration is `backend/avexie/alembic.ini`, runtime configuration is `backend/avexie/migrations/env.py`, and revisions are `backend/avexie/migrations/versions/`.

Existing-table example:

```py
# backend/avexie/models/files.py:18-31
class File(Base):
    __tablename__ = 'file'
    id = Column(String, primary_key=True, unique=True)
    user_id = Column(String, index=True)
    filename = Column(Text)
    path = Column(Text, nullable=True)
    data = Column(JSON, nullable=True)
```

Extend here: `backend/avexie/models/` (new ORM model) and `backend/avexie/migrations/versions/` (matching Alembic revision).

## 2. VECTOR DB / RAG

The abstraction is `backend/avexie/retrieval/vector/main.py:20-91`, `VectorDBBase`; selection/factory is `backend/avexie/retrieval/vector/factory.py`. Implementations are under `backend/avexie/retrieval/vector/dbs/`, including `chroma.py`, `pgvector.py`, and `qdrant.py` (also Milvus, Pinecone, OpenSearch, Weaviate, etc.).

```py
# backend/avexie/retrieval/vector/main.py:35-53
class VectorDBBase(ABC):
    @abstractmethod
    def has_collection(self, collection_name: str) -> bool: ...
    @abstractmethod
    def upsert(self, collection_name: str, items: List[VectorItem]) -> None: ...
    @abstractmethod
    def search(self, collection_name, vectors, filter=None, limit=10): ...
```

Chroma is selected by `backend/avexie/config.py:431`:

```py
VECTOR_DB = os.getenv('VECTOR_DB', 'chroma')
```

`backend/avexie/retrieval/vector/dbs/chroma.py:24-50` uses a local `chromadb.PersistentClient(path=CHROMA_DATA_PATH)` unless `CHROMA_HTTP_HOST` is configured; Chroma telemetry is explicitly disabled there.

Ingestion is: `POST /api/v1/files/` → `backend/avexie/routers/files.py:314-400` (`upload_file_handler`, calls `Storage.upload_file` and persists `File`) → `POST /api/v1/retrieval/process/file` → `backend/avexie/routers/retrieval.py:1430-1620` (`process_file`, loads text, stores it in `file.data.content`) → `save_docs_to_vector_db` in the same file (`:1260-1425`, chunk → embeddings → `VECTOR_DB_CLIENT.insert`).

Configured extraction paths:

| Engine / path | Evidence | Classification |
| --- | --- | --- |
| Built-in PyPDF / LangChain loaders (PDF, CSV, DOCX, HTML, text, etc.) | `backend/avexie/retrieval/loaders/main.py:1-27, 558+` | Local-only |
| Apache Tika server | `backend/avexie/retrieval/loaders/main.py:500-509`; default `http://tika:9998` in `backend/avexie/config.py:795` | Local-only/self-hosted service |
| Docling server | `backend/avexie/retrieval/loaders/main.py:510-530`; default `http://docling:5001` in `config.py:798-806` | Local-only/self-hosted service (an arbitrary configured URL could be remote) |
| MinerU | `backend/avexie/retrieval/loaders/main.py:531-546`; `MINERU_API_MODE='local'`, localhost default in `config.py:781-793` | Local-only by checked-in default; endpoint is configurable |
| PaddleOCR-vl | `backend/avexie/retrieval/loaders/main.py:547-556`, HTTP client in `backend/avexie/retrieval/loaders/paddleocr_vl.py:21-62`; localhost default and blank token in `config.py:808-810` | Local-only by checked-in default; remote API URL is configurable |
| Mistral OCR | only historical config-key migration references in `backend/avexie/migrations/versions/3ff2c63645b8_reshape_config_to_per_key_rows.py:232,280` | Cloud API; **REMOVED from executable loader code** |
| Azure Document Intelligence | only historical migration references in that same revision `:253-255` | Cloud API; **REMOVED from executable loader code** |

There is **no scanned-vs-born-digital router using local engines**. `Loader.aload` routes by the single configured `CONTENT_EXTRACTION_ENGINE` and extension; for Tika/Docling it only short-circuits already-text files with `_is_text_file` (`loaders/main.py:500-510`). That is not scan detection and does not select a local OCR path based on document content.

Extend here: `backend/avexie/retrieval/loaders/main.py` (routing) and `backend/avexie/routers/retrieval.py:1430` (ingestion orchestration).

## 3. TOOLS FRAMEWORK

Persisted Tools/Filters/Pipes are `Function` rows in `backend/avexie/models/functions.py:19-36`; CRUD is `backend/avexie/routers/functions.py`; plugin/module loading is `backend/avexie/utils/plugin.py`; filtering is `backend/avexie/utils/filter.py`; tool assembly is `backend/avexie/utils/tools.py`. Built-ins live in `backend/avexie/tools/builtin.py`.

A Tool is an exported Python callable. `backend/avexie/utils/tools.py:330-365` obtains `getattr(module, function_name)`, wraps it with injected parameters, and derives its OpenAI schema from type hints/docstring. A Filter exposes an `inlet`/`outlet` handler; its actual interface is dynamically inspected and awaited:

```py
# backend/avexie/utils/filter.py:168-190
handler = getattr(function_module, filter_type, None)
sig = inspect.signature(handler)
params = get_filter_params(sig, filter_id, filter_type, form_data, extra_params)
form_data = await run_filter_handler(handler, params)
```

The chat-time single execution choke point is the nested `execute_tool_call` in `backend/avexie/utils/middleware.py:5627-5672`: it resolves the declared tool, restricts args to its schema, then calls either the client/direct-tool event or `await function(**params)`. An audit wrapper for tool invocation should hook there. `backend/avexie/tools/builtin.py` is the built-in pattern-match source; its `kb_exec` is re-exported at line 58 as a native callable tool.

Extend here: `backend/avexie/utils/middleware.py:5627`.

## 4. AUDIT / LOGGING

Yes. HTTP-level audit middleware is `backend/avexie/utils/audit.py:116-307`, registered conditionally in `backend/avexie/main.py:680-696`. It captures authenticated user id/name/email/role, method, URI, response status, source IP, user agent and (depending on `AUDIT_LOG_LEVEL`) request/response bodies; it redacts a JSON `password` field.

```py
# backend/avexie/utils/audit.py:292-305
entry = AuditLogEntry(..., verb=request.method, request_uri=str(request.url),
    response_status_code=context.metadata.get('response_status_code'),
    request_object=request_body, response_object=response_body)
self.audit_logger.write(entry)
```

It is disabled by default (`AUDIT_LOG_LEVEL='NONE'`, `backend/avexie/env.py:1141`). When enabled it writes structured Loguru logs, normally to `${DATA_DIR}/audit.log` (`env.py:1126-1144`, `utils/logger.py:150-187`); it does **not** write audit rows to the database. There is no equivalent action/tool-call audit record at the execution choke point.

Extend here: `backend/avexie/utils/middleware.py:5627`.

## 5. FILE STORAGE

The only storage-provider implementation is `backend/avexie/storage/provider.py`. There is no active local/S3/GCS/Azure provider abstraction/switch: `Storage` is unconditionally `LocalStorageProvider()`.

```py
# backend/avexie/storage/provider.py:14-57
class LocalStorageProvider:
    @staticmethod
    def upload_file(file: BinaryIO, filename: str, tags: Dict[str, str]) -> Tuple[bytes, str]: ...
    @staticmethod
    def get_file(file_path: str) -> str: ...
    @staticmethod
    def delete_file(file_path: str) -> None: ...
    @staticmethod
    def delete_all_files() -> None: ...
Storage = LocalStorageProvider()
```

Personal ownership is `file.user_id` (`backend/avexie/models/files.py:18-31`). Shared knowledge uses `knowledge.user_id`, `knowledge_file`, and the generic `access_grant` table. `access_grant` supports user, group, and wildcard/anyone principals (`backend/avexie/models/access_grants.py:16-43`); groups/membership are `backend/avexie/models/groups.py:37-83`. There is no workspace/org tenant table or scope: “workspace” is UI terminology over owned resources plus grants/groups.

Extend here: `backend/avexie/storage/provider.py`.

## 6. MODEL SELECTION

The chat endpoint resolves `form_data['model']` against `request.app.state.MODELS`, loads its stored `Model`, applies access checks/overrides and optional fallback in `backend/avexie/main.py:940-1040`; it then calls the dispatch handler at `main.py:1484` (`chat_completion_handler`).

Yes, a Filter inlet extension point runs before model dispatch. `backend/avexie/utils/middleware.py:2630-2639` calls `process_filter_functions(..., filter_type='inlet', form_data=form_data, extra_params=extra_params)` before the later chat/provider processing. Registration is database-backed: `Function.type` identifies a `filter`, `is_active`/`is_global` control activation (`backend/avexie/models/functions.py:19-36`), and `backend/avexie/routers/functions.py:172-241` loads/persists it. The handler receives its declared subset of the body plus injected values such as `__user__`, `__metadata__`, `__request__`, and `__model__`, and must return the replacement `form_data` dict.

Extend here: `backend/avexie/utils/middleware.py:2630`.

## 7. NAMING

The internal Python package has been renamed to **`avexie`**: e.g. `backend/avexie/models/files.py:9` imports `from avexie.internal.db import ...`; the server entrypoint is `backend/avexie/main.py`. Searches found no `from open_webui...` imports in active Python source. The top-level backend directory remains `backend`, not an AVEXIE-named directory.

Extend here: No existing equivalent — net new.

## 8. CLOUD-DEPENDENCY AUDIT

| Strip-list item | Current state | Evidence and actual reachability |
| --- | --- | --- |
| Cloud model connectors (OpenAI/Anthropic/Groq/Mistral/OpenRouter/etc.) | **LIVE** | Generic OpenAI-compatible connector is enabled by default in `backend/avexie/config.py:264-291`; router is `backend/avexie/routers/openai.py`; Anthropic protocol conversion is `backend/avexie/utils/anthropic.py`. Base URLs/keys are blank by default, but adding them enables any compatible cloud provider. |
| External web search (Google PSE, Bing, Brave, Tavily-like/external providers; SearXNG excluded) | **DISABLED** | Provider settings and router code remain (`backend/avexie/routers/retrieval.py:204-314`), but public feature output hard-codes `enable_web_search: False` (`backend/avexie/main.py:2019-2023`) and web search is off until explicit admin/env reconfiguration. No Tavily reference was found. |
| Cloud image generation (OpenAI DALL·E/Gemini) | **DISABLED** | OpenAI image request code remains in `backend/avexie/routers/images.py:619-647`, but `ENABLE_IMAGE_GENERATION = False` is a hard constant in `backend/avexie/config.py:1023` and public features hard-code false at `main.py:2027`. No Gemini generator was found. |
| Cloud OCR/document engines (Mistral OCR/Azure Document Intelligence) | **REMOVED** | No executable loader/config route exists; only legacy migration key mappings remain (`backend/avexie/migrations/versions/3ff2c63645b8_reshape_config_to_per_key_rows.py:232-255,280`). |
| Cloud file pickers (Google Drive/OneDrive/SharePoint) | **LIVE** | Backend config says integrations are removed (`backend/avexie/config.py:766`), but the live client still imports and invokes the picker helpers in `src/lib/components/chat/MessageInput.svelte:18-19`; implementations remain in `src/lib/utils/google-drive-picker.ts` and `src/lib/utils/onedrive-file-picker.ts`. They need credentials/config, but executable client code remains. |
| OAuth against Google/Microsoft/GitHub external IdPs | **REMOVED** | `backend/avexie/config.py:1925-1930` states provider login removed; `backend/avexie/env.py:849-857` retains only MCP tool-server OAuth encryption/session support. `backend/avexie/utils/oauth.py` is for OAuth to configured MCP tool servers, not app login. |
| Telemetry/update-check pings | **LIVE** | OpenTelemetry is hard-disabled (`backend/avexie/env.py:1168-1170`) and Chroma sets `anonymized_telemetry=False` (`retrieval/vector/dbs/chroma.py:30-35`), but `ENABLE_VERSION_UPDATE_CHECK` defaults true (`env.py:1108-1113`). No outgoing update-check call was found, so this should be treated as unresolved until the enabled setting is removed or a call-path test proves it inert. |

## Conflicts with CONTRACTS.md

No `CONTRACTS.md` exists anywhere under the checked-out repository (or its immediate parent search scope), so there are no assumed paths/table names available to compare.
