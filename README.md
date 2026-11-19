# Doc Intelligence

Doc Intelligence is a document workspace built on retrieval-augmented generation (RAG). Users
upload collections of documents in common office formats; the application produces a cited brief
of each document and answers questions using only the uploaded content. Every claim in an answer
links to the passage it came from, and the source page opens with that passage highlighted.

## Features

- **Multi-format ingestion:** PDF, Word, PowerPoint (including speaker notes and chart data),
  Excel, CSV, Markdown and plain text. Running headers, footers and contents pages are removed
  before indexing.
- **Collections:** documents are grouped into workspaces; search and chat can be scoped to a
  collection or a single document.
- **Document briefs:** summary, key points, actions and obligations (owner and due date),
  extracted facts (dates, amounts, people, organisations) and open questions. Every item cites
  its source passage; items without a valid citation are discarded.
- **Grounded chat:** follow-up questions are rewritten into standalone queries, answers cite
  numbered sources, invalid citations are removed, and the model states when the documents do not
  contain the answer.
- **Highlighted citations:** chunks store page numbers and character offsets, so the source viewer
  highlights the exact passage behind a citation.
- **Prompt-injection defences:** invisible text (white text in PDFs, hidden Word runs, hidden
  Excel sheets) is detected, excluded from the index and reported. Retrieved text is passed to the
  model as untrusted input.

## Architecture

```
upload ─► validate ─► encrypt and store ─► job: verify ─► parse ─► index ─► brief
          format from content,                         │         │        │
          size and page limits,                        │         │        └─ structured LLM output,
          zip-bomb checks                              │         │           schema validation,
                                                       │         │           citation verification
                                                       │         └─ heading-aware chunks with
                                                       │            offsets ─► embeddings ─► Qdrant
                                                       └─ format-specific parsers ─► pages,
                                                          hidden text removed

question ─► standalone query ─► hybrid retrieval (dense + BM25, RRF, optional reranker),
            scoped to the user's documents ─► answer from numbered sources ─► citation check
```

| Area | Implementation |
| --- | --- |
| Parsing | pypdf, python-docx, python-pptx, openpyxl; spreadsheet rows rendered as `Header: value` pairs |
| Chunking | Splits on document headings; sentence-aware windows of up to 1,400 characters with 200 characters of overlap; character offsets retained |
| Embeddings | OpenAI `text-embedding-3-small`, batched; title and section heading included in the embedded text |
| Vector store | Qdrant (embedded local mode or server), one collection per embedding model, owner and scope stored in the payload |
| Retrieval | Dense search plus BM25, merged with Reciprocal Rank Fusion; optional FastEmbed cross-encoder reranking |
| Generation | OpenAI chat completions (`gpt-4.1-mini` by default) with JSON mode for briefs; one repair attempt on invalid output |
| Processing | Four-stage resumable job with per-stage commits, retry with exponential backoff, transient/permanent error classification, and resumption of interrupted jobs at startup; runs in-process or on arq workers with Redis |
| Data | SQLAlchemy 2 (async) with PostgreSQL in deployment and SQLite for local development; Alembic migrations; the test suite runs against both |
| File storage | Fernet-encrypted files on local disk or S3-compatible object storage (Cloudflare R2, AWS S3) |
| Security | Argon2id password hashing, JWT with a fixed algorithm, per-user and service-wide rate limits, owner filtering on every query and vector search, Fernet encryption at rest, content-based file type detection, strict Content Security Policy, audit log |
| Offline mode | `DI_LLM_PROVIDER=offline` runs without an API key using extractive answers and rule-based briefs; used by the test suite |

The RAG pipeline (chunking, hybrid retrieval, rank fusion and citation verification) is
implemented directly rather than through an orchestration framework.

## Getting started

Requirements: Python 3.11 or later.

```bash
git clone <repository-url>
cd <repository-folder>
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env                 # Windows: copy .env.example .env
```

Edit `.env`:

- set `DI_JWT_SECRET` and `DI_STORAGE_ENCRYPTION_KEY` (generation commands are in the file)
- set `DI_OPENAI_API_KEY`, or set `DI_LLM_PROVIDER=offline` to run without a key

Start the application:

```bash
uvicorn doc_intelligence.main:create_app --factory --reload
```

Open http://127.0.0.1:8000, create an account and upload documents. Sample files are included in
`samples/` (see [samples/README.md](samples/README.md)); regenerate them with
`python scripts/make_samples.py`.

## Deployment

[DEPLOYMENT.md](DEPLOYMENT.md) describes a free-tier production setup: Render for the application,
Neon for PostgreSQL, Qdrant Cloud for vectors and Cloudflare R2 for files. [`render.yaml`](render.yaml)
defines the service, runs migrations on start and sets usage limits for a public demo.

## Configuration

All settings are environment variables with the `DI_` prefix (see
[`config.py`](src/doc_intelligence/config.py) and [`.env.example`](.env.example)).

| Variable | Default | Purpose |
| --- | --- | --- |
| `DI_DATABASE_URL` | `sqlite+aiosqlite:///./doc_intelligence.db` | Database connection |
| `DI_OPENAI_API_KEY` | not set | OpenAI API key |
| `DI_LLM_PROVIDER` | `openai` | `openai` or `offline` |
| `DI_LLM_MODEL` | `gpt-4.1-mini` | Chat model |
| `DI_EMBEDDING_MODEL` | `text-embedding-3-small` | Embedding model |
| `DI_STORAGE_BACKEND` | `local` | `local` or `s3` (with `DI_S3_*` settings) |
| `DI_QDRANT_LOCATION` | `./data/qdrant` | Local folder, `:memory:` or a server URL |
| `DI_QDRANT_API_KEY` | not set | API key for Qdrant Cloud |
| `DI_RERANKER` | `none` | `fastembed` enables cross-encoder reranking (`pip install -e ".[rerank]"`) |
| `DI_QUEUE_BACKEND` | `inline` | `inline` or `arq` |
| `DI_MAX_UPLOAD_MB` | `25` | Maximum upload size |
| `DI_GLOBAL_UPLOADS_PER_HOUR`, `DI_GLOBAL_CHAT_MESSAGES_PER_HOUR` | `0` (unlimited) | Service-wide limits for public deployments |

## API

| Method | Path | Description |
| --- | --- | --- |
| POST | `/auth/register`, `/auth/login` | Create an account, obtain a token |
| POST | `/auth/delete-account` | Delete the account and all its data |
| GET, POST, PATCH, DELETE | `/collections` | Manage collections |
| POST | `/documents` | Upload a document (multipart: `file`, `collection_id`, optional `title`) |
| GET | `/documents`, `/documents/{id}` | List documents; document detail with job status and brief |
| GET | `/documents/{id}/pages/{n}` | Page text, used to highlight citations |
| POST | `/documents/{id}/reprocess` | Run the processing pipeline again |
| DELETE | `/documents/{id}` | Delete a document and all derived data |
| POST | `/search` | Hybrid search within the user's documents |
| POST | `/chat/sessions`, `/chat/sessions/{id}/messages` | Grounded chat over a collection or document |

Interactive documentation is served at `/docs` outside production.

## Testing

```bash
pytest -q
ruff check .
```

The test suite runs in offline mode and needs no API key. It covers authentication, file
validation and parsing for every format, hidden-text detection, chunk offsets, retrieval scoping,
brief and chat citation checks, isolation between users, deletion, job retry and resumption,
S3 storage (mocked) and deployment configuration.

To run it against PostgreSQL, set `TEST_DATABASE_URL`:

```bash
TEST_DATABASE_URL=postgresql://user:password@localhost:5432/test pytest -q
```

CI runs the suite on SQLite and on PostgreSQL 16, verifies migrations in both directions, and runs
dependency and secret scanning.
[TESTING.md](TESTING.md) contains a manual test plan; edge-case files can be generated with
`python scripts/make_test_cases.py`.

## Project structure

```
src/doc_intelligence/
  api/            HTTP routes: auth, collections, documents and search, chat
  rag/            chunking, embeddings, BM25, vector store, hybrid retriever
  parsing.py      format-specific text extraction and hidden-text detection
  pipeline.py     resumable processing job
  briefing.py     document briefs
  chat.py         conversational retrieval and citation verification
  static/         web client (HTML, CSS, JavaScript)
migrations/       Alembic migrations
render.yaml       Render deployment blueprint
scripts/          sample and test-case generators
samples/          sample documents
tests/            pytest suite
```

## Roadmap

- Detection of contradictions between documents in a collection
- Identification of questions the collection cannot answer
- Semantic comparison of document versions
- Saved answers that are flagged when a source document changes
- MCP server for access from other AI tools
- Evaluation suite (retrieval recall, faithfulness, citation accuracy, abstention)
- OCR for scanned PDFs, PII masking, tracing and cost reporting
