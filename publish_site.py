#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import subprocess
from http.client import IncompleteRead
from time import sleep
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import build_site

REPO = "shinejjy/arxiv-web"
API = f"https://api.github.com/repos/{REPO}/contents"
BRANCH = "main"
HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "Content-Type": "application/json",
}


def get_token() -> str:
    env_token = subprocess.os.environ.get("GITHUB_TOKEN") or subprocess.os.environ.get("GH_TOKEN")
    if env_token:
        return env_token.strip()

    token_file = subprocess.os.path.expanduser("~/.hermes/github_token")
    try:
        with open(token_file, "r", encoding="utf-8") as fh:
            token = fh.read().strip()
        if token:
            return token
    except OSError:
        pass

    env_file = subprocess.os.path.expanduser("~/.hermes/.env")
    try:
        with open(env_file, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("GITHUB_TOKEN="):
                    token = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if token:
                        return token
    except OSError:
        pass

    payload = "protocol=https\nhost=github.com\n\n".encode("utf-8")
    git_candidates = [
        "git",
        "/mnt/c/Program Files/Git/cmd/git.exe",
        "/mnt/c/Program Files/Git/bin/git.exe",
    ]
    last_error = None
    for git_cmd in git_candidates:
        try:
            res = subprocess.run(
                [git_cmd, "credential", "fill"],
                input=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
                timeout=8,
            )
            token = ""
            for line in res.stdout.decode("utf-8", "replace").splitlines():
                if line.startswith("password="):
                    token = line.split("=", 1)[1].strip()
            if token:
                return token
        except Exception as e:
            last_error = e
    raise RuntimeError(f"No GitHub token found via env/file/helper: {last_error}")


def api_request(token: str, method: str, path: str, payload: dict | None = None) -> dict | None:
    headers = dict(HEADERS)
    headers["Authorization"] = f"token {token}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = Request(f"{API}/{path}", headers=headers, method=method, data=data)
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            with urlopen(req, timeout=45) as resp:
                try:
                    raw = resp.read().decode("utf-8", "replace")
                except IncompleteRead as e:
                    raw = e.partial.decode("utf-8", "replace")
                return json.loads(raw) if raw else None
        except HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            raise RuntimeError(f"GitHub API {method} {path} failed: {e.code} {body}") from e
        except (URLError, TimeoutError, IncompleteRead, OSError) as e:
            last_error = e
            if attempt == 4:
                raise RuntimeError(f"GitHub API {method} {path} failed after retries: {e}") from e
            sleep(2 ** attempt)
    raise RuntimeError(f"GitHub API {method} {path} failed: {last_error}")


def upsert_file(token: str, rel: str, content: str | bytes) -> dict:
    sha = None
    try:
        existing = api_request(token, "GET", rel)
        if isinstance(existing, dict):
            sha = existing.get("sha")
    except RuntimeError as e:
        if "404" not in str(e):
            raise
    raw = content if isinstance(content, bytes) else content.encode("utf-8")
    payload = {
        "message": f"Update arXiv site: {rel}",
        "content": base64.b64encode(raw).decode("ascii"),
        "branch": BRANCH,
    }
    if sha:
        payload["sha"] = sha
    return api_request(token, "PUT", rel, payload) or {}


def main() -> None:
    pages = build_site.generate_site()
    build_site.write_site(pages)
    token = get_token()
    results = []
    for rel in sorted(pages.keys()):
        results.append(rel)
        upsert_file(token, rel, pages[rel])
    asset_dir = build_site.PREVIEW_DIR
    if asset_dir.exists():
        for path in sorted(asset_dir.glob("*.png")):
            rel = str(path.relative_to(build_site.SITE_DIR)).replace("\\", "/")
            results.append(rel)
            upsert_file(token, rel, path.read_bytes())
    print(json.dumps({"updated": results, "count": len(results)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
