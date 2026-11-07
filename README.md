# rag-share

A **read-only MCP search server** that lets several AI assistants share one
long-term memory: a Google Drive folder.

Each AI writes new memories into the Drive folder through its own Drive
access. This server exposes read-only `memory_search` and `memory_read`
tools over streamable HTTP, protected by bearer tokens — or by
its own built-in OAuth 2.0 authorization server for clients that cannot use
static tokens (such as ChatGPT's MCP connector).

> 日本語の説明は [README.ja.md](README.ja.md) にあります。

Use `memory_search {query, k}` to find documents, then
`memory_read {file_id, offset: 0, limit: 4000}` with a hit's `fileId` to read
the indexed text. Follow `next_offset` until it is null to read the entire
document. Offsets and limits count Unicode characters; the maximum page is
8,000 characters. The response includes `modifiedTime`; restart pagination
if it changes. Reads use the same authentication as search and accept only
IDs in the current index, never filesystem paths.

## How it works

```
Google Drive folder ── pull.py (service account, every ~30 min)
        │                extracts text, writes corpus/<fileId>.txt
        ▼                + corpus/manifest.json
   corpus/ ── server.py polls manifest.json every 60 s,
        │      re-chunks and re-embeds only changed files
        ▼
   MCP clients ── memory_search {query, k} ──► hybrid ranking
                  (0.4 · cosine over multilingual embeddings
                   + 0.6 · BM25 over character bigrams,
                   best chunk per file)
```

- **Search**: hybrid ranking over an in-memory index. Embeddings use
  `paraphrase-multilingual-mpnet-base-v2` via fastembed (ONNX, local).
  Results are deduplicated per file, so `k` results span `k` files.
- **Read-only by construction**: the server has no write path at all.
  Saving happens through Google Drive, never through the MCP.
- **Auth, two kinds**: static bearer tokens (stored only as SHA-256
  hashes in `tokens.json`), and OAuth 2.0 access tokens issued by the
  built-in authorization server (dynamic client registration,
  authorization code + PKCE S256, refresh-token rotation; only hashes
  are persisted). The authorization endpoint is gated by a consent
  passphrase the owner sets — it fails closed while no passphrase file
  exists.
- **Access log**: one TSV line per call (time, token label, tool, detail,
  hit count). Query text is omitted by default. `RAG_SHARE_LOG_QUERIES=1`
  enables plaintext query prefixes (200 characters), which may contain private
  information. Credentials are not logged. Files use POSIX mode 0600; manage
  directory ACLs on Windows. Rotation defaults to 1 MiB per file plus three
  backups, controlled by `RAG_SHARE_LOG_MAX_BYTES` and `RAG_SHARE_LOG_BACKUPS`
  (1..10). Retention is size-based, not a fixed number of days.

## Requirements

- Python 3.11+ (developed on 3.14)
- A Google Drive folder to mirror
- For the pull: a Google Cloud **service account** with the folder
  shared to it as a viewer, and the Drive API enabled in its project
- About 1.5 GB of free RAM while running (the embedding model dominates;
  the index itself is tiny at personal scale — see *Design notes*)

## Quick start

### Option A: Docker (easiest)

```sh
git clone <this repo> rag-share && cd rag-share
cp rag-share.env.example rag-share.env   # edit the values
docker compose up -d --build
```

All runtime state lives in the `rag-share-data` volume (`/data` in the
container). Read the initial token the server generated on first start:

```sh
docker compose exec server cat /data/INITIAL_TOKEN.txt
```

The pull runs as a one-shot container (see *The pull* below):

```sh
docker compose run --rm pull
```

### Option B: plain Python

```sh
git clone <this repo> rag-share && cd rag-share
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp rag-share.env.example rag-share.env   # edit the values
set -a && . ./rag-share.env && set +a
venv/bin/python server.py
```

Example systemd user units for the plain-Python setup are in `systemd/`.

On first start the server generates an initial static token: the hash
goes to `tokens.json` and the plaintext **only** to `INITIAL_TOKEN.txt`
(mode 0600) for the owner. Add more tokens by inserting
`label: sha256hex(token)` pairs into `tokens.json`.

Connect an MCP client to `http://127.0.0.1:8790/mcp` (or your public
base URL) with `Authorization: Bearer <token>`.

### The pull (Drive → corpus)

1. Create a service account, download its JSON key, and save it as
   `sa-key.json` next to `pull.py` (mode 0600). **Never commit it.**
2. Share your Drive folder to the service account's address as a
   **viewer**.
3. Set `RAG_SHARE_FOLDER_ID` to the folder's ID and run:

```sh
venv/bin/python pull.py     # prints "PULL DONE files=N ..."
```

When a listing is empty, `pull.py` verifies that the folder itself is accessible
and not trashed before clearing the corpus. An inaccessible folder preserves
existing memories; an accessible empty folder removes the last deleted memory.
Retryable download failures retain the last good version for the next pull.
Run it on a
timer (see `systemd/`) or from cron. If a Google Doc/Sheet/Slide is in
the folder it is exported as text/CSV; plain-text-like files are
downloaded; anything else (and files over 5 MB) is skipped.

### OAuth clients (e.g. ChatGPT)

1. Put a consent passphrase (one line) in `consent-passphrase.txt`
   (mode 0600). Without this file the authorization endpoint refuses
   every request.
2. Set `RAG_SHARE_PUBLIC_BASE` to the server's public HTTPS origin and
   expose the server (see below).
3. In the client, register the MCP server by URL. The client discovers
   the metadata under `/.well-known/`, registers itself, and sends the
   owner to `/authorize`, where entering the passphrase issues tokens
   (30-day access tokens with rotating refresh tokens).

Note: `/authorize` intentionally shows a minimal consent page — the
passphrase is the consent.

## Companion prompts — making AIs actually use it

The server alone does not make an AI participate. Two prompts in
[`docs/`](docs/) complete the system:

1. **Writing guide** ([`docs/MEMORY_WRITING_GUIDE.md`](docs/MEMORY_WRITING_GUIDE.md),
   [日本語](docs/MEMORY_WRITING_GUIDE.ja.md)): the rules each AI follows
   when saving a memory — file naming, front matter, facts vs.
   interpretation, what never to write. Put a copy **inside the shared
   folder itself**, so it is searchable and always the current version,
   and also give it to each AI (custom instructions, or at the start
   of a conversation).
2. **Bootstrap prompt** ([`docs/BOOTSTRAP_PROMPT.md`](docs/BOOTSTRAP_PROMPT.md),
   [日本語](docs/BOOTSTRAP_PROMPT.ja.md)): paste **once** to a new AI.
   It makes the AI fetch the writing guide via `memory_search` and
   retain the key points in whatever long-term memory mechanism it
   has.

Adapt the folder name in the templates to your own.

## Exposing the server

Public OAuth requests have bounded bodies, registration counts, and shared
per-route request budgets. See [OAuth operation](docs/OAUTH_OPERATIONS.md) for
limits, proxy trust, and removing unused registrations.

OAuth state is written to a private temporary file, flushed, then atomically
replaced. Invalid existing JSON is an error rather than an empty store; restore
the last known-good backup before retrying. Keep backups of the OAuth state
private as well. Missing state files can still be initialized normally.

The server itself is plain HTTP. Put a TLS tunnel or reverse proxy in
front of it, for example:

- **Tailscale Funnel**: `tailscale funnel --bg 8790` (only this server —
  keep the rest of your tailnet private), or
- nginx / Caddy with a public hostname.

Then set `RAG_SHARE_PUBLIC_BASE=https://your-host.example` so the OAuth
metadata and `WWW-Authenticate` header point at the right place.

## Configuration (environment variables)

| Variable | Used by | Default | Meaning |
|---|---|---|---|
| `RAG_SHARE_PUBLIC_BASE` | server | `http://127.0.0.1:8790` | Public origin of the server (OAuth issuer / resource URI) |
| `RAG_SHARE_BIND_HOSTS` | server | `127.0.0.1` | Comma-separated bind addresses |
| `RAG_SHARE_PORT` | server | `8790` | Listen port |
| `RAG_SHARE_FOLDER_ID` | pull | *(required)* | Google Drive folder ID to mirror |
| `RAG_SHARE_DIR` | server, pull | script's directory | Data directory (corpus, tokens, OAuth state, model cache, logs). Docker sets `/data` |

Runtime files (all mode 0600 where they hold secrets, all git-ignored):
`tokens.json`, `INITIAL_TOKEN.txt`, `oauth-clients.json`,
`oauth-tokens.json`, `consent-passphrase.txt`, `sa-key.json`,
`access.log`, `pull-state.json`, plus `corpus/` and `model-cache/`.

## Security notes — read before exposing

- The OAuth authorization server is **hand-rolled as a learning-grade
  implementation**. It follows the relevant RFC shapes (9728/8414
  metadata, PKCE S256, single-use codes, refresh rotation, hashed
  storage), but it has not been audited. Prefer a vetted authorization
  server for anything beyond personal use.
- The consent passphrase is a **single shared secret**. Anyone who
  learns it can authorize their own client and search your memory.
  Choose a strong one, and consider network-level restrictions in
  addition.
- Search results expose the *contents* of your memory folder to any
  token holder. Share tokens deliberately; revoke by removing the
  label from `tokens.json` (effective immediately).
- Do not publish your production endpoint URL together with this code.

## Design notes

- **Why in-memory?** At personal scale the corpus is small (tens to
  hundreds of notes). The index is rebuilt from the corpus text files,
  changed files are re-embedded incrementally, and the embedding model
  — not the index — dominates memory. Persisting embeddings (e.g. in
  SQLite, keyed by file ID + modified time) becomes worthwhile once a
  full restart re-embeds for minutes: roughly at ~1,000 files /
  ~10 MB of text / ~10k chunks.
- **Why file-level dedupe?** Memory folders accumulate several files
  about the same topic; without dedupe, top-k results collapse onto
  near-duplicate chunks of one file.
- **Why a service account for the pull?** It keeps the owner's OAuth
  tokens off the server machine and scopes access to exactly one
  folder, read-only.

## Layout

```
server.py            the MCP server (search + auth + OAuth)
pull.py              Drive → corpus sync (service account)
docs/                companion prompts: writing guide + bootstrap (EN/JA)
Dockerfile           container image (state in a /data volume)
docker-compose.yml   server + one-shot pull
systemd/             example user units (server, pull oneshot, pull timer)
rag-share.env.example
```

## License

MIT — see [LICENSE](LICENSE).
