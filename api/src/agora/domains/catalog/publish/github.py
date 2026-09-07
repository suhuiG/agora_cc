"""GitHubPublisher — GitHub Contents API로 파일을 repo main에 커밋.

MVP: 파일별 개별 PUT(단순). http는 테스트 주입용(기본 httpx). 토큰은 인자로만 받고
저장하지 않아요(credential 스토어가 소유).
"""
from __future__ import annotations

import base64
import re

from .models import PullRequestResult, PushResult, RepoAccessResult, RepoConnection

_API = "https://api.github.com"
_REPO_RE = re.compile(r"github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$")


def parse_owner_repo(repo_url: str) -> tuple[str, str]:
    m = _REPO_RE.search(repo_url)
    if not m:
        raise ValueError(f"github repo URL을 파싱할 수 없어요: {repo_url!r}")
    return m.group("owner"), m.group("repo")


def _message_of(resp) -> str:
    """GitHub 에러 응답의 message를 안전하게 뽑아요(본문이 없거나 JSON이 아닐 수 있어요)."""
    try:
        body = resp.json()
    except Exception:
        return ""
    return str(body.get("message", "")) if isinstance(body, dict) else ""


class _HttpxCaller:
    """기본 httpx 래퍼 — request(method, url, headers=, json=) → 응답."""
    def request(self, method, url, *, headers=None, json=None):
        import httpx
        return httpx.request(method, url, headers=headers, json=json, timeout=30)


class GitHubPublisher:
    def __init__(self, http=None) -> None:
        self._http = http or _HttpxCaller()

    def _headers(self, token: str) -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def verify_access(self, repo_url: str, token: str) -> RepoAccessResult:
        owner, repo = parse_owner_repo(repo_url)
        resp = self._http.request(
            "GET", f"{_API}/repos/{owner}/{repo}", headers=self._headers(token))
        if resp.status_code == 401:
            return RepoAccessResult(ok=False, reason="invalid_token")
        if resp.status_code == 404:
            return RepoAccessResult(ok=False, reason="not_found")
        if resp.status_code != 200:
            return RepoAccessResult(ok=False, reason="not_found")
        body = resp.json()
        if not body.get("permissions", {}).get("push", False):
            return RepoAccessResult(ok=False, reason="no_write")
        return RepoAccessResult(ok=True, is_private=bool(body.get("private", False)))

    def push_files(self, conn: RepoConnection, files: dict[str, bytes], *,
                   message: str, token: str) -> PushResult:
        owner, repo = parse_owner_repo(conn.repo_url)
        headers = self._headers(token)
        base = f"{_API}/repos/{owner}/{repo}"

        # default branch
        repo_resp = self._http.request("GET", base, headers=headers)
        branch = repo_resp.json().get("default_branch", "main") if repo_resp.status_code == 200 else "main"

        # 현재 ref (빈 repo면 404)
        ref_resp = self._http.request(
            "GET", f"{base}/git/ref/heads/{branch}", headers=headers)
        parents: list[str] = []
        base_tree: str | None = None
        if ref_resp.status_code == 200:
            base_commit = ref_resp.json()["object"]["sha"]
            parents = [base_commit]
            commit_resp = self._http.request(
                "GET", f"{base}/git/commits/{base_commit}", headers=headers)
            base_tree = commit_resp.json().get("tree", {}).get("sha")

        # blobs
        tree_items = []
        for path, content in sorted(files.items()):
            blob_resp = self._http.request(
                "POST", f"{base}/git/blobs", headers=headers,
                json={"content": base64.b64encode(content).decode("ascii"),
                      "encoding": "base64"})
            tree_items.append({
                "path": path, "mode": "100644", "type": "blob",
                "sha": blob_resp.json()["sha"]})

        # tree
        tree_body: dict = {"tree": tree_items}
        if base_tree:
            tree_body["base_tree"] = base_tree
        tree_resp = self._http.request(
            "POST", f"{base}/git/trees", headers=headers, json=tree_body)
        tree_sha = tree_resp.json()["sha"]

        # commit
        commit_resp = self._http.request(
            "POST", f"{base}/git/commits", headers=headers,
            json={"message": message, "tree": tree_sha, "parents": parents})
        commit_sha = commit_resp.json()["sha"]

        # ref 갱신(있으면 PATCH, 없으면 POST 생성)
        if ref_resp.status_code == 200:
            self._http.request(
                "PATCH", f"{base}/git/refs/heads/{branch}", headers=headers,
                json={"sha": commit_sha, "force": False})
        else:
            self._http.request(
                "POST", f"{base}/git/refs", headers=headers,
                json={"ref": f"refs/heads/{branch}", "sha": commit_sha})

        return PushResult(commit_sha=commit_sha, files_written=len(files))

    def _build_commit(self, base: str, headers: dict, branch: str,
                      files: dict[str, bytes], message: str) -> tuple[str, str]:
        """default branch를 부모로 파일트리 단일 커밋을 만들어요.

        반환: (commit_sha, base_branch). ref는 갱신하지 않아요 — 호출자가 정해요.
        """
        ref_resp = self._http.request(
            "GET", f"{base}/git/ref/heads/{branch}", headers=headers)
        parents: list[str] = []
        base_tree: str | None = None
        if ref_resp.status_code == 200:
            base_commit = ref_resp.json()["object"]["sha"]
            parents = [base_commit]
            commit_resp = self._http.request(
                "GET", f"{base}/git/commits/{base_commit}", headers=headers)
            base_tree = commit_resp.json().get("tree", {}).get("sha")

        tree_items = []
        for path, content in sorted(files.items()):
            blob_resp = self._http.request(
                "POST", f"{base}/git/blobs", headers=headers,
                json={"content": base64.b64encode(content).decode("ascii"),
                      "encoding": "base64"})
            tree_items.append({
                "path": path, "mode": "100644", "type": "blob",
                "sha": blob_resp.json()["sha"]})

        tree_body: dict = {"tree": tree_items}
        if base_tree:
            tree_body["base_tree"] = base_tree
        tree_resp = self._http.request(
            "POST", f"{base}/git/trees", headers=headers, json=tree_body)

        commit_resp = self._http.request(
            "POST", f"{base}/git/commits", headers=headers,
            json={"message": message, "tree": tree_resp.json()["sha"],
                  "parents": parents})
        return commit_resp.json()["sha"], branch

    def open_pull_request(self, conn: RepoConnection, files: dict[str, bytes], *,
                          branch: str, title: str, body: str,
                          token: str) -> PullRequestResult:
        owner, repo = parse_owner_repo(conn.repo_url)
        headers = self._headers(token)
        base = f"{_API}/repos/{owner}/{repo}"

        repo_resp = self._http.request("GET", base, headers=headers)
        default_branch = (repo_resp.json().get("default_branch", "main")
                          if repo_resp.status_code == 200 else "main")

        commit_sha, _ = self._build_commit(
            base, headers, default_branch, files, title)

        # 새 브랜치 ref 생성. 이미 있으면(422) force로 갱신해요(재배포·재시도).
        create_resp = self._http.request(
            "POST", f"{base}/git/refs", headers=headers,
            json={"ref": f"refs/heads/{branch}", "sha": commit_sha})
        if create_resp.status_code not in (200, 201):
            patch_resp = self._http.request(
                "PATCH", f"{base}/git/refs/heads/{branch}", headers=headers,
                json={"sha": commit_sha, "force": True})
            if patch_resp.status_code not in (200, 201):
                raise RuntimeError(
                    f"브랜치 '{branch}'를 만들 수 없어요 "
                    f"(생성 {create_resp.status_code} / 갱신 "
                    f"{patch_resp.status_code}). PAT에 Contents 쓰기 권한이 "
                    f"있는지 확인해 주세요: {_message_of(patch_resp)}")

        pr_resp = self._http.request(
            "POST", f"{base}/pulls", headers=headers,
            json={"title": title, "body": body, "head": branch,
                  "base": default_branch})
        # PR 생성 실패를 삼키면 브랜치만 남고 PR은 없는 상태를 "배포 성공"으로
        # 보고해요. 관리자가 머지할 게 없는 걸 모르게 되니 예외로 올려요.
        if pr_resp.status_code not in (200, 201):
            raise RuntimeError(
                f"PR을 만들 수 없어요 (HTTP {pr_resp.status_code}). PAT에 "
                f"Pull requests 쓰기 권한이 필요해요. 커밋은 브랜치 "
                f"'{branch}'에 올라가 있어요: {_message_of(pr_resp)}")
        pr_body = pr_resp.json()
        pr_url = pr_body.get("html_url") or ""
        if not pr_url:
            raise RuntimeError(
                f"PR 응답에 html_url이 없어요 (HTTP {pr_resp.status_code}). "
                f"커밋은 브랜치 '{branch}'에 올라가 있어요")
        return PullRequestResult(
            pr_url=pr_url,
            pr_number=int(pr_body.get("number", 0)),
            commit_sha=commit_sha, files_written=len(files))

    def read_file(self, conn: RepoConnection, path: str, *,
                  token: str) -> bytes | None:
        owner, repo = parse_owner_repo(conn.repo_url)
        resp = self._http.request(
            "GET", f"{_API}/repos/{owner}/{repo}/contents/{path}",
            headers=self._headers(token))
        if resp.status_code != 200:
            return None
        body = resp.json()
        if body.get("encoding") == "base64":
            return base64.b64decode(body["content"])
        return (body.get("content") or "").encode("utf-8")

    def delete_paths(self, conn: RepoConnection, prefixes: list[str], *,
                     message: str, token: str) -> PushResult:
        owner, repo = parse_owner_repo(conn.repo_url)
        headers = self._headers(token)
        base = f"{_API}/repos/{owner}/{repo}"

        # default branch
        repo_resp = self._http.request("GET", base, headers=headers)
        branch = repo_resp.json().get("default_branch", "main") if repo_resp.status_code == 200 else "main"

        # 현재 ref — 빈 repo(404)면 지울 게 없어요.
        ref_resp = self._http.request(
            "GET", f"{base}/git/ref/heads/{branch}", headers=headers)
        if ref_resp.status_code != 200:
            return PushResult(commit_sha="", files_written=0)
        base_commit = ref_resp.json()["object"]["sha"]
        commit_resp = self._http.request(
            "GET", f"{base}/git/commits/{base_commit}", headers=headers)
        base_tree = commit_resp.json().get("tree", {}).get("sha")

        # 트리 전체(recursive)에서 prefix에 걸리는 blob만 골라 sha=null로 삭제 표시.
        tree_resp = self._http.request(
            "GET", f"{base}/git/trees/{base_tree}?recursive=1", headers=headers)
        entries = tree_resp.json().get("tree", [])
        norm = [p.rstrip("/") for p in prefixes]
        tree_items = [
            {"path": e["path"], "mode": "100644", "type": "blob", "sha": None}
            for e in entries
            if e.get("type") == "blob" and any(
                e["path"] == p or e["path"].startswith(p + "/") for p in norm)
        ]
        if not tree_items:
            return PushResult(commit_sha="", files_written=0)

        new_tree_resp = self._http.request(
            "POST", f"{base}/git/trees", headers=headers,
            json={"base_tree": base_tree, "tree": tree_items})
        new_tree_sha = new_tree_resp.json()["sha"]
        del_commit_resp = self._http.request(
            "POST", f"{base}/git/commits", headers=headers,
            json={"message": message, "tree": new_tree_sha, "parents": [base_commit]})
        del_commit_sha = del_commit_resp.json()["sha"]
        self._http.request(
            "PATCH", f"{base}/git/refs/heads/{branch}", headers=headers,
            json={"sha": del_commit_sha, "force": False})
        return PushResult(commit_sha=del_commit_sha, files_written=len(tree_items))
