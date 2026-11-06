"""Scratch copies for write_file and patch. Stdlib only."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import threading
from pathlib import Path

STAGED_TOOLS = frozenset({"write_file", "patch"})
PLUGIN_NAME = "stage"
_LOCK = threading.Lock()
_TASK_RE = re.compile(r"[^A-Za-z0-9._-]")


def workspace_root(task_context: dict | None = None) -> Path:
    """Resolve workspace from the task's cwd when available, falling back to Path.cwd().
    
    Hermes hooks receive a task_context dict with 'cwd' that may differ from process cwd
    (gateway, worktree, remote backends). Use that when available.
    """
    if task_context and isinstance(task_context, dict):
        cwd = task_context.get("cwd")
        if cwd and isinstance(cwd, str):
            try:
                resolved = Path(cwd).expanduser().resolve()
                if resolved.is_absolute():
                    return resolved
            except Exception:
                pass
    return Path.cwd().resolve()


def data_root() -> Path:
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir(PLUGIN_NAME)


def safe_task_id(task_id: object) -> str:
    text = str(task_id or "default")
    cleaned = _TASK_RE.sub("_", text).strip("._")[:80]
    return cleaned or "default"


def _session(data_root_path: Path, task_id: object) -> Path:
    return data_root_path / "sessions" / safe_task_id(task_id)


def _scratch_root(data_root_path: Path, task_id: object) -> Path:
    return _session(data_root_path, task_id) / "files"


def _journal_path(data_root_path: Path, task_id: object) -> Path:
    return _session(data_root_path, task_id) / "journal.json"


def _contained(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return path.resolve(strict=False) != root.resolve(strict=False)


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"files": {}}
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        return {"files": {}}
    return data


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _fingerprint(path: Path) -> str | None:
    """SHA-256 of a file, or None when there is no file to protect."""
    try:
        if not path.is_file():
            return None
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def _remember(data_root_path: Path, task_id: object, relative: str, original: Path, scratch: Path, tool_name: str) -> None:
    path = _journal_path(data_root_path, task_id)
    with _LOCK:
        data = _load(path)
        prior = data["files"].get(relative)
        fresh_hash = _fingerprint(original)
        stored_hash = fresh_hash
        if isinstance(prior, dict) and "original_sha256" in prior:
            stored_hash = prior["original_sha256"]
        data["files"][relative] = {
            "relative": relative,
            "original": str(original),
            "scratch": str(scratch),
            "tool": tool_name,
            "original_sha256": stored_hash,
        }
        _save(path, data)


def _get_write_protection_error(path: str) -> str | None:
    """Check if path would be denied by Hermes' write protections.
    
    Returns error message if denied, None if allowed.
    Imports Hermes file_safety on demand to avoid breaking when run outside Hermes.
    """
    try:
        from agent.file_safety import get_write_denied_error
        return get_write_denied_error(path)
    except Exception:
        # If we can't import Hermes protections (e.g., running standalone tests),
        # allow the write - the containment check below still applies
        return None


def plan_redirect(
    raw_path: object,
    tool_name: str,
    task_id: object,
    *,
    workspace: Path | None = None,
    storage: Path | None = None,
    task_context: dict | None = None,
) -> dict | None:
    """Return a pre_tool_call directive, or None when this call is not staged.
    
    Returns None (let core handle it) for paths Hermes would deny or gate,
    so those checks happen at write time instead of being silently staged.
    """
    if tool_name not in STAGED_TOOLS:
        return None
    if not isinstance(raw_path, str) or not raw_path.strip():
        return {"action": "block", "message": "stage: a file write needs a path inside the workspace. Nothing was written."}
    
    # Check Hermes write protections first - return None so core can deny/prompt
    protection_error = _get_write_protection_error(str(raw_path))
    if protection_error:
        return None
    
    root = (workspace or workspace_root(task_context)).resolve()
    store = storage or data_root()
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=False)
    if not _contained(resolved, root):
        return {
            "action": "block",
            "message": "stage: that path is outside the workspace. The write did not run.",
        }
    if resolved.is_dir():
        return {"action": "block", "message": "stage: that path is a directory. Nothing was written."}
    relative = resolved.relative_to(root).as_posix()
    scratch_root = _scratch_root(store, task_id)
    scratch = scratch_root / relative
    if not _contained(scratch, scratch_root):
        return {"action": "block", "message": "stage: could not place a scratch copy. Nothing was written."}
    scratch.parent.mkdir(parents=True, exist_ok=True)
    if tool_name == "patch" and not scratch.is_file():
        if not resolved.is_file():
            return {
                "action": "block",
                "message": "stage: patch needs an existing file. Nothing was changed.",
            }
        shutil.copy2(resolved, scratch)
    _remember(store, task_id, relative, resolved, scratch, tool_name)
    return {"action": "modify", "args": {"path": str(scratch)}}


def stage_status(task_id: object, *, storage: Path | None = None) -> dict:
    store = storage or data_root()
    data = _load(_journal_path(store, task_id))
    pending = []
    for relative, entry in sorted(data["files"].items()):
        scratch = Path(entry.get("scratch", ""))
        pending.append({
            "path": relative,
            "original": entry.get("original", ""),
            "bytes": scratch.stat().st_size if scratch.is_file() else 0,
            "ready": scratch.is_file(),
        })
    return {"pending": pending}


def _apply_through_hermes(destination: Path, scratch: Path, mode_before: int | None) -> str | None:
    """Apply scratch to destination through Hermes' write path to respect protections.
    
    Returns error message on failure, None on success.
    """
    try:
        # Try to import and use Hermes' file safety checks
        from agent.file_safety import get_write_denied_error
        
        # Check write protections before applying
        denied = get_write_denied_error(str(destination))
        if denied:
            return f"Hermes write protection: {denied}"
        
        # Read scratch content
        content = scratch.read_text(encoding="utf-8")
        
        # Write through atomic temp + replace (same pattern as Hermes file tools)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_name = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".stage-", suffix=".tmp", dir=destination.parent,
            )
            os.close(descriptor)
            Path(temporary_name).write_text(content, encoding="utf-8")
            if mode_before is not None:
                os.chmod(temporary_name, mode_before)
            os.replace(temporary_name, destination)
            return None
        except OSError as e:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass
            return f"could not apply the scratch copy: {e}"
    except ImportError:
        # Running outside Hermes (e.g., unit tests) - use direct write
        # but still respect the atomic temp + replace pattern
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary_name = None
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".stage-", suffix=".tmp", dir=destination.parent,
            )
            os.close(descriptor)
            shutil.copy2(scratch, temporary_name)
            if mode_before is not None:
                os.chmod(temporary_name, mode_before)
            os.replace(temporary_name, destination)
            return None
        except OSError as e:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass
            return f"could not apply the scratch copy: {e}"
    except Exception as e:
        return f"unexpected error during apply: {e}"


def stage_apply(task_id: object, *, workspace: Path | None = None, storage: Path | None = None, task_context: dict | None = None) -> dict:
    root = (workspace or workspace_root(task_context)).resolve()
    store = storage or data_root()
    journal = _journal_path(store, task_id)
    scratch_root = _scratch_root(store, task_id)
    with _LOCK:
        data = _load(journal)
        applied: list[str] = []
        errors: list[dict] = []
        remaining: dict = {}
        for relative, entry in data["files"].items():
            original = Path(entry.get("original", ""))
            scratch = Path(entry.get("scratch", ""))
            if not _contained(original, root) or not _contained(scratch, scratch_root):
                errors.append({"path": relative, "error": "path escaped the workspace"})
                remaining[relative] = entry
                continue
            if not scratch.is_file():
                errors.append({"path": relative, "error": "scratch copy is missing"})
                remaining[relative] = entry
                continue
            expected_present = "original_sha256" in entry
            current = _fingerprint(original)
            expected = entry.get("original_sha256")
            if not expected_present or current != expected:
                errors.append({"path": relative, "error": "original file changed since it was staged"})
                remaining[relative] = entry
                continue
            destination = original.resolve(strict=False)
            mode_before = stat.S_IMODE(destination.stat().st_mode) if destination.exists() else None
            
            # Apply through Hermes' write path to respect protections
            apply_error = _apply_through_hermes(destination, scratch, mode_before)
            if apply_error:
                errors.append({"path": relative, "error": apply_error})
                remaining[relative] = entry
                continue
            
            scratch.unlink()
            applied.append(relative)
        if remaining:
            _save(journal, {"files": remaining})
        elif journal.exists():
            journal.unlink()
    return {"applied": applied, "errors": errors}


def stage_discard(task_id: object, *, storage: Path | None = None) -> dict:
    store = storage or data_root()
    journal = _journal_path(store, task_id)
    with _LOCK:
        data = _load(journal)
        discarded = sorted(data["files"])
        session = _session(store, task_id)
        if session.exists():
            shutil.rmtree(session)
    return {"discarded": discarded}
