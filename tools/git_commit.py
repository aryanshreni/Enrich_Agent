"""Commit approved artifacts to GitHub (version history), via the REST Git Data API.

One commit per approval, holding all files of that artifact version, with a message that
references the run and review decision IDs (design doc section 10). The token is a
fine-grained GitHub token (contents: read/write on this repo only) stored in the secret
scope, so commits work from the stage runner job with no interactive user.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass

import requests
from databricks.sdk import WorkspaceClient

API = "https://api.github.com"


def read_token(secret_scope: str, key: str = "github_token", client: WorkspaceClient | None = None) -> str:
    w = client or WorkspaceClient()
    return base64.b64decode(w.secrets.get_secret(scope=secret_scope, key=key).value).decode("utf-8").strip()


@dataclass
class GitCommitter:
    repo: str          # owner/name
    branch: str
    token: str
    path_prefix: str = "artifacts"  # files land under <path_prefix>/<source_id>/<run_id>/<stage>/...
    timeout: int = 30

    def _req(self, method: str, url: str, **kw) -> dict:
        r = requests.request(method, f"{API}/repos/{self.repo}{url}", timeout=self.timeout, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }, **kw)
        if r.status_code >= 300:
            raise RuntimeError(f"GitHub {method} {url} failed: {r.status_code} {r.text[:500]}")
        return r.json() if r.text else {}

    def check_access(self) -> dict:
        """Read-only check used by the Phase 0 verification: repo reachable and branch exists."""
        repo = self._req("GET", "")
        ref = self._req("GET", f"/git/ref/heads/{self.branch}")
        return {"repo": repo["full_name"], "can_push": repo.get("permissions", {}).get("push"),
                "head": ref["object"]["sha"]}

    def commit_files(self, files: dict[str, str], message: str) -> str:
        """files: repo-relative path (under path_prefix) -> text content. Returns the new commit SHA."""
        if not files:
            raise ValueError("nothing to commit")
        head_sha = self._req("GET", f"/git/ref/heads/{self.branch}")["object"]["sha"]
        base_tree = self._req("GET", f"/git/commits/{head_sha}")["tree"]["sha"]
        tree = []
        for rel_path, content in files.items():
            blob = self._req("POST", "/git/blobs", json={"content": content, "encoding": "utf-8"})
            tree.append({"path": f"{self.path_prefix}/{rel_path}".lstrip("/"), "mode": "100644",
                         "type": "blob", "sha": blob["sha"]})
        new_tree = self._req("POST", "/git/trees", json={"base_tree": base_tree, "tree": tree})["sha"]
        commit = self._req("POST", "/git/commits", json={"message": message, "tree": new_tree, "parents": [head_sha]})
        # Fast-forward only (force=False): fails safely if the branch moved, caller retries.
        self._req("PATCH", f"/git/refs/heads/{self.branch}", json={"sha": commit["sha"], "force": False})
        return commit["sha"]
