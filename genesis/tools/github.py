"""GitHub tools. Opening pages needs no token; listing/creating/deleting repositories uses GITHUB_TOKEN from .env.

Deleting a repository is HIGH risk and irreversible: the user must type/say the repository's name (the planner asks for
it) *and* answer yes to the confirmation; afterwards Genesis checks that GitHub really no longer has it.
"""
from __future__ import annotations

import re
from urllib.parse import quote

from ..security.permissions import RiskLevel
from ..services.github import WEB, GitHubError
from .base import Tool, ToolResult, obj, s


def _svc(ctx):
    svc = getattr(ctx, "github", None)
    if svc is None:
        raise GitHubError("GitHub isn't available in this session.", "unavailable")
    return svc


def _fail(exc: GitHubError) -> ToolResult:
    hint = {"not_configured": "Create a token at github.com → Settings → Developer settings → Personal access tokens "
                              "(fine-grained: Repository permissions → Administration: Read and write), then put "
                              "GITHUB_TOKEN=… in .env.",
            "forbidden": "Update the token's permissions on github.com, then try again."}.get(exc.kind)
    return ToolResult.fail(str(exc), hint=hint, kind=exc.kind)


def _same(a: str, b: str) -> bool:
    norm = lambda x: re.sub(r"[^a-z0-9]", "", (x or "").lower().split("/")[-1])  # noqa: E731
    return bool(norm(a)) and norm(a) == norm(b)


class GitHubOpen(Tool):
    name = "github_open"
    category = "github"
    description = ("Open GitHub in the browser: the home page, the user's repositories, the 'new repository' page, or a "
                   "specific repository by name.")
    parameters = obj({"target": s("'home', 'repos', 'new', or a repository name (e.g. 'genesis' or 'owner/genesis')", maxLength=150)})
    output = {"url": "str", "repo": "str"}

    def run(self, args, ctx):
        target = (args.get("target") or "home").strip()
        repo = None
        if target.lower() in ("home", "github", ""):
            url = WEB + "/"
        elif target.lower() == "new":
            url = WEB + "/new"
        elif target.lower() in ("repos", "repositories"):
            try:
                url = f"{WEB}/{quote(_svc(ctx).login())}?tab=repositories" if _svc(ctx).configured() else WEB + "/"
            except GitHubError:
                url = WEB + "/"
        elif "/" in target and re.fullmatch(r"[\w.-]+/[\w.-]+", target):
            url, repo = f"{WEB}/{target}", target
        else:
            try:
                found = _svc(ctx).find_repo(target)
                url, repo = found["url"], found["full_name"]
            except GitHubError as exc:
                if exc.kind not in ("not_configured",):
                    return _fail(exc)
                url = f"{WEB}/search?q={quote(target)}&type=repositories"  # no token: search GitHub instead
        ok = ctx.backend.open_url(url)
        if ok is False:
            return ToolResult.fail("I couldn't open the browser.")
        what = f"the {repo} repository" if repo else ("your repositories" if "tab=repositories" in url else
                                                      "GitHub search" if "/search" in url else "GitHub")
        return ToolResult.ok(f"Done — {what} is open in your browser.", url=url, repo=repo or "")


class GitHubListRepos(Tool):
    name = "github_list_repos"
    category = "github"
    description = "List the user's GitHub repositories (most recently updated first)."
    parameters = obj({"limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}})
    output = {"repos": "list[{name, full_name, private, url, updated}]"}
    remote = True

    def run(self, args, ctx):
        try:
            repos = _svc(ctx).list_repos(int(args.get("limit", 10)))
        except GitHubError as exc:
            return _fail(exc)
        if not repos:
            return ToolResult.ok("You don't have any repositories on GitHub yet.", repos=[])
        lines = "\n".join(f"{i + 1}. {r['name']}{' (private)' if r['private'] else ''}" + (f" — {r['description'][:60]}" if
                          r["description"] else "") for i, r in enumerate(repos))
        res = ToolResult.ok(f"Your GitHub repositories:\n{lines}", repos=repos)
        res.untrusted_content = True  # descriptions are user/third-party text
        return res


class GitHubCreateRepo(Tool):
    name = "github_create_repo"
    category = "github"
    description = "Create a new GitHub repository for the user (private unless they say public)."
    parameters = obj({"name": s("Repository name", maxLength=100), "private": {"type": "boolean", "default": True},
                      "description": s("Short description", maxLength=300)}, ["name"])
    output = {"full_name": "str", "url": "str"}
    risk = RiskLevel.MEDIUM
    remote = True

    def confirmation_prompt(self, args):
        vis = "private" if args.get("private", True) else "PUBLIC (anyone can see it)"
        return f"Create a {vis} GitHub repository called “{args['name']}”?"

    def run(self, args, ctx):
        try:
            repo = _svc(ctx).create_repo(args["name"], bool(args.get("private", True)), args.get("description") or "")
        except GitHubError as exc:
            return _fail(exc)
        return ToolResult.ok(f"Created {'private' if repo['private'] else 'public'} repository {repo['full_name']}: {repo['url']}",
                             **repo)

    def verify(self, args, result, ctx):
        if not result.success:
            return False
        try:
            return _svc(ctx).exists(result.data["full_name"])
        except GitHubError:
            return None


class GitHubDeleteRepo(Tool):
    name = "github_delete_repo"
    category = "github"
    description = ("Permanently delete one of the user's GitHub repositories. Irreversible: requires the repository name typed "
                   "back by the user (confirm_name) and an explicit yes.")
    parameters = obj({"repo": s("Repository name", maxLength=150), "confirm_name": s("The name as typed by the user to confirm",
                                                                                      maxLength=150)}, ["repo", "confirm_name"])
    output = {"full_name": "str", "deleted": "bool"}
    risk = RiskLevel.HIGH
    always_confirm = True
    remote = True

    def confirmation_prompt(self, args):
        return (f"Permanently delete the GitHub repository “{args['repo']}”? All its code, issues and history will be gone — "
                "this can't be undone.")

    def run(self, args, ctx):
        if not _same(args.get("confirm_name", ""), args["repo"]):
            return ToolResult.fail(f"The name you gave (“{args.get('confirm_name', '')}”) doesn't match “{args['repo']}”, so I "
                                   "didn't delete anything.")
        try:
            svc = _svc(ctx)
            repo = svc.find_repo(args["repo"])
            if not _same(args["confirm_name"], repo["name"]):
                return ToolResult.fail(f"That matched “{repo['name']}”, not what you typed, so I didn't delete anything.")
            svc.delete_repo(repo["full_name"])
        except GitHubError as exc:
            return _fail(exc)
        return ToolResult.ok(f"Deleted the GitHub repository {repo['full_name']}.", full_name=repo["full_name"], deleted=True)

    def verify(self, args, result, ctx):
        if not result.success:
            return False
        try:
            return not _svc(ctx).exists(result.data["full_name"])
        except GitHubError:
            return None


TOOLS = [GitHubOpen(), GitHubListRepos(), GitHubCreateRepo(), GitHubDeleteRepo()]
