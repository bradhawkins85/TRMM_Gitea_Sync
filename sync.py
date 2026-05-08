#!/usr/bin/env python3
"""TRMM-Gitea Sync

Synchronises scripts stored in a Gitea repository with the Tactical RMM
script library.

Rules
-----
* The top-level folder a script lives in becomes its TRMM *category*.
* The filename (without extension) becomes the TRMM *script name*.
* The file extension determines the TRMM *shell* type.
* Gitea content always wins – the script body is overwritten on every run.
* TRMM-managed settings (args, supported_platforms, run_as_user, env_vars,
  default_timeout, favorite, hidden) are **never** overwritten for scripts
  that already exist in TRMM.
* Scripts removed from Gitea are deleted from TRMM, **but only if** their
  description begins with the ``[Gitea]`` prefix (i.e. they were originally
  created by this sync tool).  Scripts created directly inside TRMM are never
  deleted.

Configuration (.env file or environment variables)
--------------------------------------------------
Values are loaded from a ``.env`` file in the working directory (via
python-dotenv) and then fall back to actual environment variables, so both
approaches work.

TRMM_API_URL      Base URL of the Tactical RMM instance, e.g. https://rmm.example.com
TRMM_API_KEY      Tactical RMM API key
GITEA_URL         Base URL of the Gitea instance, e.g. https://gitea.example.com
GITEA_TOKEN       Gitea access token (required for private repos)
GITEA_OWNER       Gitea repository owner (user or org)
GITEA_REPO        Gitea repository name
GITEA_BRANCH      Branch to read from (default: main)
GITEA_LOCAL_PATH  Path to the local clone of the Gitea repo (default: ./gitea_repo).
                  On first run the repo is cloned here; subsequent runs do a
                  ``git pull`` and only scripts whose files changed are synced.
FULL_SYNC         Set to "true", "1", or "yes" to sync every script regardless
                  of what changed in git.  Useful to force a complete re-sync
                  after editing TRMM scripts manually (default: false).
IGNORE_SSL        Set to "true", "1", or "yes" to disable SSL certificate
                  verification for all API calls.  Useful when the script runs
                  on the TRMM server itself where the API hostname resolves to
                  127.0.0.1 and the certificate CN does not match (default: false)
"""

import logging
import os
import subprocess
import sys
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse, urlunparse

import requests
import urllib3
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TRMM_API_URL: str = os.environ.get("TRMM_API_URL", "").rstrip("/")
TRMM_API_KEY: str = os.environ.get("TRMM_API_KEY", "")

GITEA_URL: str = os.environ.get("GITEA_URL", "").rstrip("/")
GITEA_TOKEN: str = os.environ.get("GITEA_TOKEN", "")
GITEA_OWNER: str = os.environ.get("GITEA_OWNER", "")
GITEA_REPO: str = os.environ.get("GITEA_REPO", "")
GITEA_BRANCH: str = os.environ.get("GITEA_BRANCH", "main")
GITEA_LOCAL_PATH: str = os.environ.get("GITEA_LOCAL_PATH", "./gitea_repo")

FULL_SYNC: bool = os.environ.get("FULL_SYNC", "").lower() in ("1", "true", "yes")

IGNORE_SSL: bool = os.environ.get("IGNORE_SSL", "").lower() in ("1", "true", "yes")
SSL_VERIFY: bool = not IGNORE_SSL

if IGNORE_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# File extension → TRMM shell type
EXTENSION_TO_SHELL: Dict[str, str] = {
    ".ps1": "powershell",
    ".py": "python",
    ".sh": "shell",
    ".bat": "cmd",
    ".cmd": "cmd",
}

# Default supported_platforms for newly created scripts, keyed by shell type
DEFAULT_PLATFORMS: Dict[str, List[str]] = {
    "powershell": ["windows"],
    "python": ["windows", "linux", "darwin"],
    "shell": ["linux", "darwin"],
    "cmd": ["windows"],
}

DEFAULT_SCRIPT_TYPE: str = "userdefined"
DEFAULT_TIMEOUT: int = 90

# ---------------------------------------------------------------------------
# Git / local-clone helpers
# ---------------------------------------------------------------------------


def _build_clone_url() -> str:
    """Return the authenticated HTTPS clone URL for the configured Gitea repo."""
    parsed = urlparse(GITEA_URL)
    netloc_with_auth = f"oauth2:{GITEA_TOKEN}@{parsed.netloc}"
    repo_path = f"/{GITEA_OWNER}/{GITEA_REPO}.git"
    return urlunparse((parsed.scheme, netloc_with_auth, repo_path, "", "", ""))


def _git_run(args: List[str], cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    """Run a git sub-command and return the CompletedProcess.  Raises RuntimeError on failure."""
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # Avoid leaking the token that may appear in URLs within error messages.
        safe_stderr = result.stderr.replace(GITEA_TOKEN, "***") if GITEA_TOKEN else result.stderr
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {safe_stderr.strip()}")
    return result


def _ensure_local_repo() -> Tuple[bool, List[str], List[str]]:
    """Ensure a local clone of the Gitea repo exists at ``GITEA_LOCAL_PATH``.

    * On the first run (or when the directory is not a git repository) the repo
      is cloned from Gitea.
    * On subsequent runs ``git pull`` is executed and the files that changed
      between the previous HEAD and the new HEAD are recorded.

    Returns:
        (is_fresh_clone, modified_paths, deleted_paths)

    ``is_fresh_clone`` is ``True`` when the repo was just cloned for the first
    time; in that case ``modified_paths`` and ``deleted_paths`` are both empty
    (the caller should treat every file as new).

    ``modified_paths`` and ``deleted_paths`` contain repository-relative POSIX
    paths (e.g. ``"Checks/Check Disk Space.ps1"``).
    """
    local_path = os.path.abspath(GITEA_LOCAL_PATH)

    is_git_repo = os.path.isdir(os.path.join(local_path, ".git"))

    if not is_git_repo:
        if os.path.exists(local_path) and os.listdir(local_path):
            log.error(
                "GITEA_LOCAL_PATH '%s' already exists and is not a git repository. "
                "Remove or empty the directory, or set GITEA_LOCAL_PATH to a different path.",
                local_path,
            )
            raise RuntimeError(f"Not a git repository: {local_path}")

        log.info("Cloning Gitea repo to %s …", local_path)
        clone_url = _build_clone_url()
        _git_run(["clone", "--branch", GITEA_BRANCH, clone_url, local_path])
        log.info("  Clone complete")
        return True, [], []

    # Existing clone – pull and collect changed paths.
    old_head = _git_run(["rev-parse", "HEAD"], cwd=local_path).stdout.strip()

    # Keep the remote URL up to date in case the token has changed.
    clone_url = _build_clone_url()
    _git_run(["remote", "set-url", "origin", clone_url], cwd=local_path)

    log.info("Pulling latest changes in %s …", local_path)
    _git_run(["pull", "origin", GITEA_BRANCH], cwd=local_path)

    new_head = _git_run(["rev-parse", "HEAD"], cwd=local_path).stdout.strip()

    if old_head == new_head:
        log.info("  Already up to date (HEAD=%s)", new_head[:8])
        return False, [], []

    log.info("  Updated %s → %s", old_head[:8], new_head[:8])

    diff_output = _git_run(
        ["diff", "--name-status", old_head, new_head],
        cwd=local_path,
    ).stdout.strip()

    modified_paths: List[str] = []
    deleted_paths: List[str] = []

    for line in diff_output.splitlines():
        if not line:
            continue
        parts = line.split("\t")
        status = parts[0]
        if status.startswith("R"):
            # Rename: R<score>\t<old_path>\t<new_path>
            if len(parts) >= 3:
                deleted_paths.append(parts[1])
                modified_paths.append(parts[2])
        elif status.startswith("C"):
            # Copy: C<score>\t<src_path>\t<dst_path> – only the new file needs syncing
            if len(parts) >= 3:
                modified_paths.append(parts[2])
        elif status in ("A", "M"):
            if len(parts) >= 2:
                modified_paths.append(parts[1])
        elif status == "D":
            if len(parts) >= 2:
                deleted_paths.append(parts[1])

    return False, modified_paths, deleted_paths


# ---------------------------------------------------------------------------
# Script discovery (local filesystem)
# ---------------------------------------------------------------------------


def _shell_from_filename(filename: str) -> Optional[str]:
    """Return the TRMM shell type for *filename*, or None if not recognised."""
    _, ext = os.path.splitext(filename.lower())
    return EXTENSION_TO_SHELL.get(ext)


def _append_local_script(
    scripts: List[dict], file_path: str, category: str
) -> None:
    """Read *file_path* from disk and append a script entry to *scripts*."""
    filename = os.path.basename(file_path)
    shell = _shell_from_filename(filename)
    if shell is None:
        log.debug("Skipping unsupported file type: %s/%s", category, filename)
        return

    name, _ = os.path.splitext(filename)

    try:
        with open(file_path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        log.warning("Could not read file %s: %s", file_path, exc)
        return

    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        content = raw.decode("latin-1")

    # Normalise Windows-style line endings.
    content = content.replace("\r\n", "\n")

    scripts.append(
        {
            "name": name,
            "category": category,
            "shell": shell,
            "content": content,
        }
    )


def collect_local_scripts(filter_paths: Optional[Set[str]] = None) -> List[dict]:
    """Walk the local clone and return one dict per script.

    Only files that are **direct children of a top-level directory** are
    processed; nested sub-directories are skipped.  Files at the repository
    root are assigned an empty-string category.

    If *filter_paths* is provided (a set of repository-relative POSIX paths),
    only scripts whose path is in the set are returned, enabling incremental
    syncing when only a subset of files changed.
    """
    local_path = os.path.abspath(GITEA_LOCAL_PATH)
    scripts: List[dict] = []

    try:
        root_entries = list(os.scandir(local_path))
    except OSError as exc:
        log.error("Cannot scan local repo at %s: %s", local_path, exc)
        raise

    for entry in root_entries:
        if entry.name.startswith("."):
            continue

        if entry.is_dir(follow_symlinks=False):
            category = entry.name
            try:
                dir_entries = list(os.scandir(entry.path))
            except OSError as exc:
                log.warning("Could not scan directory %s: %s", entry.path, exc)
                continue

            for file_entry in dir_entries:
                if not file_entry.is_file(follow_symlinks=False):
                    # Skip nested sub-directories.
                    continue
                rel_path = f"{category}/{file_entry.name}"
                if filter_paths is not None and rel_path not in filter_paths:
                    continue
                _append_local_script(scripts, file_entry.path, category)

        elif entry.is_file(follow_symlinks=False):
            rel_path = entry.name
            if filter_paths is not None and rel_path not in filter_paths:
                continue
            _append_local_script(scripts, entry.path, "")

    return scripts


# ---------------------------------------------------------------------------
# TRMM API helpers
# ---------------------------------------------------------------------------


def _trmm_headers() -> Dict[str, str]:
    return {
        "X-API-KEY": TRMM_API_KEY,
        "Content-Type": "application/json",
    }


def _trmm_get(path: str) -> requests.Response:
    url = f"{TRMM_API_URL}{path}"
    try:
        resp = requests.get(url, headers=_trmm_headers(), timeout=30, verify=SSL_VERIFY)
    except requests.exceptions.RequestException as exc:
        log.error("Network error contacting TRMM (%s): %s", url, exc)
        raise
    resp.raise_for_status()
    return resp


def _trmm_post(path: str, data: dict) -> requests.Response:
    url = f"{TRMM_API_URL}{path}"
    try:
        resp = requests.post(url, headers=_trmm_headers(), json=data, timeout=30, verify=SSL_VERIFY)
    except requests.exceptions.RequestException as exc:
        log.error("Network error contacting TRMM (%s): %s", url, exc)
        raise
    resp.raise_for_status()
    return resp


def _trmm_put(path: str, data: dict) -> requests.Response:
    url = f"{TRMM_API_URL}{path}"
    try:
        resp = requests.put(url, headers=_trmm_headers(), json=data, timeout=30, verify=SSL_VERIFY)
    except requests.exceptions.RequestException as exc:
        log.error("Network error contacting TRMM (%s): %s", url, exc)
        raise
    resp.raise_for_status()
    return resp


def _trmm_delete(path: str) -> requests.Response:
    url = f"{TRMM_API_URL}{path}"
    try:
        resp = requests.delete(url, headers=_trmm_headers(), timeout=30, verify=SSL_VERIFY)
    except requests.exceptions.RequestException as exc:
        log.error("Network error contacting TRMM (%s): %s", url, exc)
        raise
    resp.raise_for_status()
    return resp


def get_trmm_script_detail(script_id: int) -> dict:
    """Return full metadata for a single TRMM script, including ``script_body``."""
    resp = _trmm_get(f"/scripts/{script_id}/")
    return resp.json()


def get_all_trmm_scripts() -> Dict[Tuple[str, str], dict]:
    """
    Return a dict mapping (name, category) → script metadata for every
    script currently in TRMM.
    """
    resp = _trmm_get("/scripts/")
    try:
        data = resp.json()
    except (requests.exceptions.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(
            f"TRMM /scripts/ returned non-JSON response "
            f"(status {resp.status_code}): {exc}"
        ) from exc

    if not isinstance(data, list):
        raise RuntimeError(
            f"TRMM /scripts/ returned unexpected response type "
            f"{type(data).__name__!r} – expected a list. "
            f"Response: {str(data)[:200]}"
        )

    index: Dict[Tuple[str, str], dict] = {}
    for script in data:
        name = script.get("name")
        if not name:
            log.warning(
                "Skipping TRMM script with missing name (id=%s)", script.get("id")
            )
            continue
        key = (name, script.get("category") or "")
        index[key] = script
    return index


# ---------------------------------------------------------------------------
# Sync logic
# ---------------------------------------------------------------------------


GITEA_DESCRIPTION_PREFIX: str = "[Gitea]"


def _gitea_description(existing_description: str) -> str:
    """
    Return *existing_description* with ``[Gitea]`` prepended.

    Idempotent – if the prefix is already present it is not added again,
    so repeated sync runs do not accumulate multiple prefixes.
    """
    description = existing_description or ""
    if description.startswith(GITEA_DESCRIPTION_PREFIX):
        return description
    if description:
        return f"{GITEA_DESCRIPTION_PREFIX} {description}"
    return GITEA_DESCRIPTION_PREFIX


def sync_script(gitea_script: dict, trmm_index: Dict[Tuple[str, str], dict]) -> str:
    """
    Create or update a single TRMM script from *gitea_script*.

    Returns ``"created"``, ``"updated"``, ``"skipped"``, or raises on error.
    Scripts that already exist in TRMM with identical content are skipped so
    that only genuine changes result in API write calls.
    """
    name: str = gitea_script["name"]
    category: str = gitea_script["category"]
    shell: str = gitea_script["shell"]
    content: str = gitea_script["content"]
    key: Tuple[str, str] = (name, category)

    if key in trmm_index:
        existing = trmm_index[key]
        script_id: int = existing["id"]

        # The TRMM list endpoint (/scripts/) omits script_body for performance.
        # Fetch the full script detail so we can compare bodies accurately and
        # update the cache so any subsequent reference to this entry is complete.
        if "script_body" not in existing:
            existing = get_trmm_script_detail(script_id)
            trmm_index[key] = existing
        existing_body: str = existing.get("script_body") or ""
        existing_description: str = existing.get("description") or ""
        new_description: str = _gitea_description(existing_description)

        # Skip the PUT when the script body and description prefix are both
        # already up-to-date, avoiding unnecessary writes to TRMM.
        if existing_body == content and existing_description == new_description:
            log.debug("Skipped  : %s [category=%s] (no changes)", name, category)
            return "skipped"

        # Preserve every TRMM-managed field; only replace the script body
        # (and keep name/category/shell consistent with Gitea).
        # Description is updated to carry the [Gitea] prefix.
        payload = {
            "name": name,
            "script_body": content,
            "shell": shell,
            "script_type": existing.get("script_type") or DEFAULT_SCRIPT_TYPE,
            "category": category,
            "description": new_description,
            "args": existing.get("args") or [],
            "default_timeout": existing.get("default_timeout") or DEFAULT_TIMEOUT,
            "favorite": existing.get("favorite", False),
            "hidden": existing.get("hidden", False),
            "supported_platforms": existing.get("supported_platforms")
            or DEFAULT_PLATFORMS.get(shell, ["windows"]),
            "run_as_user": existing.get("run_as_user", False),
            "env_vars": existing.get("env_vars") or [],
        }
        _trmm_put(f"/scripts/{script_id}/", payload)
        log.info("Updated  : %s [category=%s]", name, category)
        return "updated"

    # Script does not exist in TRMM yet – create it with sensible defaults.
    payload = {
        "name": name,
        "script_body": content,
        "shell": shell,
        "script_type": DEFAULT_SCRIPT_TYPE,
        "category": category,
        "description": GITEA_DESCRIPTION_PREFIX,
        "args": [],
        "default_timeout": DEFAULT_TIMEOUT,
        "favorite": False,
        "hidden": False,
        "supported_platforms": DEFAULT_PLATFORMS.get(shell, ["windows"]),
        "run_as_user": False,
        "env_vars": [],
    }
    _trmm_post("/scripts/", payload)
    log.info("Created  : %s [category=%s]", name, category)
    return "created"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _validate_config() -> bool:
    """Return True only when all required environment variables are set."""
    # Map variable name → module-level value so we can report which are missing
    # without accidentally logging their contents.
    required_names = [
        "TRMM_API_URL",
        "TRMM_API_KEY",
        "GITEA_URL",
        "GITEA_TOKEN",
        "GITEA_OWNER",
        "GITEA_REPO",
    ]
    required_values = [
        TRMM_API_URL,
        TRMM_API_KEY,
        GITEA_URL,
        GITEA_TOKEN,
        GITEA_OWNER,
        GITEA_REPO,
    ]
    missing = [name for name, val in zip(required_names, required_values) if not val]
    if missing:
        log.error("Missing required environment variables: %s", ", ".join(missing))
        return False
    return True


def main() -> None:
    if not _validate_config():
        sys.exit(1)

    log.info("TRMM-Gitea sync starting")
    if IGNORE_SSL:
        log.warning("SSL certificate verification is DISABLED (IGNORE_SSL=true)")
    log.info(
        "Gitea : %s  owner=%s  repo=%s  branch=%s",
        GITEA_URL,
        GITEA_OWNER,
        GITEA_REPO,
        GITEA_BRANCH,
    )
    log.info("TRMM  : %s", TRMM_API_URL)
    log.info("Local repo path: %s", os.path.abspath(GITEA_LOCAL_PATH))
    if FULL_SYNC:
        log.info("FULL_SYNC is enabled – all scripts will be synced regardless of changes")

    # ------------------------------------------------------------------
    # Step 1 – Ensure the local git clone is up to date.
    # ------------------------------------------------------------------
    try:
        is_fresh_clone, modified_paths, deleted_paths = _ensure_local_repo()
    except Exception as exc:  # pylint: disable=broad-except
        log.error("Failed to prepare local Gitea repo: %s", exc)
        sys.exit(1)

    # Decide whether to do a full or incremental sync:
    #  * Always full on the first clone (all files are "new").
    #  * Always full when FULL_SYNC=true.
    #  * Incremental otherwise (only process files that changed).
    do_full: bool = FULL_SYNC or is_fresh_clone

    if not do_full and not modified_paths and not deleted_paths:
        log.info("No changes detected in Gitea repo – nothing to sync")
        return

    # ------------------------------------------------------------------
    # Step 2 – Fetch the current TRMM script index.
    # ------------------------------------------------------------------
    log.info("Fetching scripts from TRMM …")
    try:
        trmm_index = get_all_trmm_scripts()
    except (requests.exceptions.RequestException, RuntimeError) as exc:
        log.error("Failed to fetch scripts from TRMM: %s", exc)
        sys.exit(1)
    log.info("  %d script(s) found in TRMM", len(trmm_index))

    # ------------------------------------------------------------------
    # Step 3 – Collect scripts from the local clone.
    # ------------------------------------------------------------------
    if do_full:
        log.info("Collecting all scripts from local Gitea clone …")
        try:
            gitea_scripts = collect_local_scripts()
        except OSError as exc:
            log.error("Failed to read local Gitea repo: %s", exc)
            sys.exit(1)
    else:
        log.info(
            "Collecting %d changed file(s) from local Gitea clone …",
            len(modified_paths),
        )
        try:
            gitea_scripts = collect_local_scripts(filter_paths=set(modified_paths))
        except OSError as exc:
            log.error("Failed to read local Gitea repo: %s", exc)
            sys.exit(1)

    log.info("  %d script(s) to process", len(gitea_scripts))

    # ------------------------------------------------------------------
    # Step 4 – Create / update scripts in TRMM.
    # ------------------------------------------------------------------
    created = updated = skipped = errors = 0

    for gs in gitea_scripts:
        try:
            result = sync_script(gs, trmm_index)
            if result == "created":
                created += 1
            elif result == "skipped":
                skipped += 1
            else:
                updated += 1
        except requests.HTTPError as exc:
            response_detail = ""
            if exc.response is not None:
                try:
                    response_detail = f"  Response body: {exc.response.text}"
                except Exception:  # pylint: disable=broad-except
                    pass
            log.error(
                "HTTP error syncing '%s' [%s]: %s%s",
                gs["name"],
                gs["category"],
                exc,
                response_detail,
            )
            errors += 1
        except Exception as exc:  # pylint: disable=broad-except
            log.error(
                "Unexpected error syncing '%s' [%s]: %s",
                gs["name"],
                gs["category"],
                exc,
            )
            errors += 1

    # ------------------------------------------------------------------
    # Step 5 – Delete TRMM scripts that were removed from Gitea.
    # ------------------------------------------------------------------
    deleted = 0

    if do_full:
        # Full sync: remove any TRMM script (with the [Gitea] prefix) whose
        # counterpart no longer exists anywhere in the repo.
        gitea_keys = {(gs["name"], gs["category"]) for gs in gitea_scripts}
        for key, script in trmm_index.items():
            description = script.get("description") or ""
            if not description.startswith(GITEA_DESCRIPTION_PREFIX):
                continue
            if key in gitea_keys:
                continue
            script_id = script["id"]
            name, category = key
            try:
                _trmm_delete(f"/scripts/{script_id}/")
                log.info("Deleted  : %s [category=%s]", name, category)
                deleted += 1
            except requests.HTTPError as exc:
                log.error(
                    "HTTP error deleting '%s' [%s]: %s",
                    name,
                    category,
                    exc,
                )
                errors += 1
            except Exception as exc:  # pylint: disable=broad-except
                log.error(
                    "Unexpected error deleting '%s' [%s]: %s",
                    name,
                    category,
                    exc,
                )
                errors += 1
    else:
        # Incremental sync: only delete TRMM scripts for files that git
        # explicitly reported as deleted in this pull.
        for rel_path in deleted_paths:
            parts = rel_path.replace("\\", "/").split("/")
            if len(parts) == 1:
                filename, category = parts[0], ""
            elif len(parts) == 2:
                category, filename = parts[0], parts[1]
            else:
                # Nested path – outside our one-level category model; skip.
                continue

            shell = _shell_from_filename(filename)
            if shell is None:
                continue

            name, _ = os.path.splitext(filename)
            key = (name, category)

            if key not in trmm_index:
                continue

            script = trmm_index[key]
            description = script.get("description") or ""
            if not description.startswith(GITEA_DESCRIPTION_PREFIX):
                continue

            script_id = script["id"]
            try:
                _trmm_delete(f"/scripts/{script_id}/")
                log.info("Deleted  : %s [category=%s]", name, category)
                deleted += 1
            except requests.HTTPError as exc:
                log.error(
                    "HTTP error deleting '%s' [%s]: %s",
                    name,
                    category,
                    exc,
                )
                errors += 1
            except Exception as exc:  # pylint: disable=broad-except
                log.error(
                    "Unexpected error deleting '%s' [%s]: %s",
                    name,
                    category,
                    exc,
                )
                errors += 1

    log.info(
        "Sync complete – created: %d  updated: %d  skipped: %d  deleted: %d  errors: %d",
        created,
        updated,
        skipped,
        deleted,
        errors,
    )

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
