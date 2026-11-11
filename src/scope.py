"""Where a kept rule applies: in every project, or only in the projects the user named. A project is
known by its git origin, so that the clones and worktrees of one repository are one project; a
repository without an origin by its main folder, so that its worktrees fold into it; a folder
outside git by itself."""
import subprocess
from pathlib import Path
from urllib.parse import urlsplit


def git(cwd, *args):
    try:
        r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def origin(url, cwd):
    """host/owner/repo for a remote URL, the same for its ssh and https forms; a local remote's folder."""
    if "://" in url:
        parts = urlsplit(url)
        host, path = (parts.hostname or "").lower(), parts.path
    elif ":" in url.split("/", 1)[0]:   # scp-like: [user@]host:owner/repo
        host, path = url.split(":", 1)
        host = host.rsplit("@", 1)[-1].lower()
    else:
        return str((Path(cwd) / url).resolve()).removesuffix(".git")
    return f"{host}/{path.strip('/').removesuffix('.git')}"


def project(cwd):
    """The project key of a folder; None without one."""
    if not cwd:
        return None
    url = git(cwd, "remote", "get-url", "origin")
    if url:
        return origin(url, cwd)
    common = git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return str(Path(common).parent) if common else str(Path(cwd).resolve())


def name(key):
    """The short name of a project key, as the user sees it."""
    return key.rstrip("/").rsplit("/", 1)[-1] if key else "?"


def applies(h, key):
    """A kept rule applies in this project: it has no scope, or the scope names the project."""
    return bool(h.get("rule")) and (not h.get("scope") or key in h["scope"])


def where(h):
    """Every project, or the names of the projects in its scope."""
    return "every project" if not h.get("scope") else "only in " + ", ".join(name(k) for k in h["scope"])
