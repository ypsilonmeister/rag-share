#!/usr/bin/env python3
"""Pull the shared-memory Drive folder into the local rag-share corpus.

A Google service account with read-only access to the shared folder
reads Drive directly (Drive API v3 over REST), extracts plain text, and
writes corpus/<fileId>.txt + corpus/manifest.json for the search
server, which watches manifest.json and re-indexes incrementally.

Auth: service-account JWT bearer flow (RS256), scope drive.readonly.
Key file: sa-key.json in the data directory (mode 600, placed by the
owner; never commit it). Configuration via environment:
  RAG_SHARE_DIR       data directory (default: this script's directory)
  RAG_SHARE_FOLDER_ID the Drive folder ID to mirror (required)
State: pull-state.json in the data directory (fileId -> modifiedTime).

Exit codes: 0 ok, 2 setup incomplete (key/folder ID missing), 1 error.
"""
import base64
import json
import os
import sys
import time

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

BASE = os.environ.get("RAG_SHARE_DIR",
                      os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(BASE, "corpus")
STATE_PATH = os.path.join(BASE, "pull-state.json")
KEY_PATH = os.path.join(BASE, "sa-key.json")
FOLDER_ID = os.environ.get("RAG_SHARE_FOLDER_ID", "")
MAX_SIZE = 5 * 1024 * 1024  # skip files larger than 5 MB (same as v1)
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/drive/v3"

EXPORT_MAP = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}
DOWNLOAD_MIMES = {
    "text/plain", "text/markdown", "text/csv", "application/json",
    "text/x-python", "application/sql", "text/html",
}
FOLDER_MIME = "application/vnd.google-apps.folder"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def get_access_token(key: dict) -> str:
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {
        "iss": key["client_email"],
        "scope": "https://www.googleapis.com/auth/drive.readonly",
        "aud": TOKEN_URL,
        "iat": now,
        "exp": now + 3600,
    }
    signing_input = (
        b64url(json.dumps(header).encode()) + "."
        + b64url(json.dumps(claims).encode())
    ).encode()
    private_key = serialization.load_pem_private_key(
        key["private_key"].encode(), password=None)
    signature = private_key.sign(
        signing_input, padding.PKCS1v15(), hashes.SHA256())
    jwt_token = signing_input.decode() + "." + b64url(signature)
    r = requests.post(TOKEN_URL, data={
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": jwt_token,
    }, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"token exchange failed: {r.status_code} {r.text[:200]}")
    return r.json()["access_token"]


def api_get(token: str, url: str, **kw) -> requests.Response:
    return requests.get(url, headers={"Authorization": f"Bearer {token}"},
                        timeout=60, **kw)


def list_children(token: str, folder_id: str) -> list:
    files, page_token = [], None
    while True:
        params = {
            "q": f"'{folder_id}' in parents and trashed=false",
            "pageSize": 200,
            "fields": "nextPageToken,files(id,name,mimeType,modifiedTime,size,webViewLink)",
        }
        if page_token:
            params["pageToken"] = page_token
        r = api_get(token, f"{API}/files", params=params)
        if r.status_code != 200:
            raise RuntimeError(f"files.list failed: {r.status_code} {r.text[:200]}")
        data = r.json()
        files.extend(data.get("files", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            return files


def collect(token: str, folder_id: str, out: list) -> None:
    for f in list_children(token, folder_id):
        if f["mimeType"] == FOLDER_MIME:
            collect(token, f["id"], out)
        else:
            out.append(f)


def extract(token: str, f: dict) -> tuple:
    fid, mime = f["id"], f["mimeType"]
    if mime in EXPORT_MAP:
        r = api_get(token, f"{API}/files/{fid}/export",
                    params={"mimeType": EXPORT_MAP[mime]})
    elif mime in DOWNLOAD_MIMES or mime == "application/octet-stream":
        r = api_get(token, f"{API}/files/{fid}", params={"alt": "media"})
    else:
        return "skip-mime", None
    if r.status_code != 200:
        return f"error-http-{r.status_code}", None
    raw = r.content
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return "skip-not-utf8", None
    if not raw.strip():
        return "skip-empty", None
    return "ok", raw


def write_atomic(path: str, data: bytes) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def main() -> int:
    if not os.path.exists(KEY_PATH):
        print(f"WAITING: service account key not found at {KEY_PATH}")
        return 2
    mode = os.stat(KEY_PATH).st_mode & 0o777
    if mode & 0o077:
        print(f"warning: {KEY_PATH} permissions are {oct(mode)}; 600 recommended")
    with open(KEY_PATH) as fh:
        key = json.load(fh)
    if not FOLDER_ID:
        print("WAITING: RAG_SHARE_FOLDER_ID is not set")
        return 2
    token = get_access_token(key)

    os.makedirs(CORPUS, exist_ok=True)
    state = {}
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as fh:
            state = json.load(fh)
    files: list = []
    collect(token, FOLDER_ID, files)
    if not files:
        # An empty listing almost always means the folder is not shared
        # with the service account. Never overwrite a populated corpus
        # with an empty one.
        raise RuntimeError(
            "no files visible in the shared folder; aborting to protect "
            "the corpus (is the folder shared with the service account?)")
    manifest = {}
    added = updated = skipped = 0
    seen = set()
    for f in files:
        fid = f["id"]
        seen.add(fid)
        size = int(f.get("size", 0) or 0)
        if size > MAX_SIZE:
            skipped += 1
            print(f"skip-too-large: {f['name']} ({size} bytes)")
            continue
        dest = os.path.join(CORPUS, fid + ".txt")
        changed = (state.get(fid) != f["modifiedTime"]
                   or not os.path.exists(dest)
                   or os.path.getsize(dest) == 0)
        if changed:
            res, raw = extract(token, f)
            if res != "ok":
                skipped += 1
                print(f"{res}: {f['name']}")
                continue
            write_atomic(dest, raw)
            if fid in state:
                updated += 1
            else:
                added += 1
            state[fid] = f["modifiedTime"]
        manifest[fid] = {
            "name": f["name"], "mimeType": f["mimeType"],
            "modifiedTime": f["modifiedTime"],
            "webViewLink": f.get("webViewLink", ""),
            "text_file": fid + ".txt",
        }
    for fid in list(state):
        if fid not in seen:
            p = os.path.join(CORPUS, fid + ".txt")
            if os.path.exists(p):
                os.remove(p)
            del state[fid]
            print(f"removed: {fid}")
    write_atomic(os.path.join(CORPUS, "manifest.json"),
                 json.dumps(manifest, ensure_ascii=False, indent=1).encode())
    write_atomic(STATE_PATH,
                 json.dumps(state, ensure_ascii=False, indent=1).encode())
    print(f"PULL DONE files={len(manifest)} added={added} updated={updated} skipped={skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
