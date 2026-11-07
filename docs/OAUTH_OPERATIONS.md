# Public OAuth operation

Use HTTPS through a TLS tunnel or reverse proxy as described in the README.
The application applies the following POST limits before dispatching handlers:

| Setting | Default | Meaning |
| --- | --- | --- |
| `RAG_SHARE_REGISTER_PER_MINUTE` | 10 | Registration requests per 60-second window |
| `RAG_SHARE_AUTHORIZE_PER_MINUTE` | 10 | Consent submissions per window, including failed passphrases |
| `RAG_SHARE_TOKEN_PER_MINUTE` | 60 | Token exchanges per window |
| `RAG_SHARE_OAUTH_BODY_LIMIT` | 16384 | Maximum request body bytes, also checked across streamed chunks |
| `RAG_SHARE_OAUTH_MAX_CLIENTS` | 100 | Maximum persisted registrations |

The rate budgets are **application-wide**, not per client or IP, and reset on
process restart. A single process is required for these in-memory budgets and
JSON state locks. They do not coordinate across workers or replicas. Normal
single-owner setup and refresh flows use few requests; raise the documented
settings if several clients legitimately exhaust a shared budget.

Rate limits return HTTP 429 with `Retry-After`; an exhausted registration store
returns 429 with `registration_limit_reached` and requires owner cleanup.
Oversize bodies return 413. A 15-second wait limit applies to each body chunk;
a timed-out read returns 408.

The application does not trust `X-Forwarded-For` for limits. Uvicorn proxy-header
rewriting is disabled. All tunnel/proxy clients therefore share the same budget
regardless of forwarded headers. If your reverse proxy applies additional per-IP
limits, derive the address from the actual connection or explicitly trusted
upstream proxies only. Do not accept arbitrary client-supplied forwarding headers.
Configure connection/total-request time limits at that public ingress as well.

## Removing unused registrations

1. Stop the server so edits cannot race with a state write. Back up
   `oauth-clients.json` and `oauth-tokens.json` privately.
2. Identify the unused `client_id` by name and creation time in
   `oauth-clients.json`, then remove that key.
3. Remove entries from `oauth-tokens.json` whose `client_id` matches. Removing
   only the client prevents future refreshes but does not revoke existing access
   tokens. Remove both to revoke access.
4. Keep both files valid JSON objects with POSIX mode 0600 (or private Windows
   ACLs), restart the server, and retry registration.

Never delete active registrations merely to free space. If a state file is
malformed, restore the backup rather than initializing an empty store.

## 日本語の要点

上の制限はIP別ではなくプロセス全体で共有し、再起動でリセットされます。
設定値は `rag-share.env.example` にあります。複数workerでの運用には対応しません。
429の `Retry-After` に従って再試行してください。登録上限に達した場合はサーバーを停止し、
バックアップ後に不要なclient_idをクライアントJSONと、それに紐づくトークンJSONの両方から
削除して再起動します。クライアントだけの削除では発行済みアクセストークンは失効しません。
プロキシの転送ヘッダーを任意の利用者から信用しないでください。
