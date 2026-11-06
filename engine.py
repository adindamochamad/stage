"""Scratch copies for write_file and patch. Stdlib only."""

from __future__ import annotations

import contextlib
import datetime
import difflib
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import tempfile
import threading
import uuid
from pathlib import Path

STAGED_TOOLS = frozenset({"write_file", "patch"})
COMMAND_TOOLS = frozenset({"terminal", "execute_code"})
PLUGIN_NAME = "stage"
_LOCK = threading.Lock()
_TASK_RE = re.compile(r"[^A-Za-z0-9._-]")
RECEIPT_DIFF_LIMIT = 8192
SHELL_BLOCK = (
    "stage: that command would change a file directly. "
    "Use write_file or patch, then stage_apply."
)
CONFLICT_HINT = (
    "original file changed since it was staged. "
    "Call stage_diff for this path, then stage_discard on this path."
)

_V4A_BEGIN = "*** Begin Patch"
_V4A_END = "*** End Patch"
_V4A_HEADERS = (
    ("Update File", "update"),
    ("Add File", "add"),
    ("Delete File", "delete"),
)
_CODE_WRITE = (
    re.compile(r"""open\s*\([^)]*['"](?:w|a|x|r\+|w\+|a\+|wb|ab|xb|wt|at)"""),
    re.compile(r"""open\s*\([^)]*mode\s*=\s*['"](?:w|a|x)"""),
    re.compile(r"\.write_text\s*\("),
    re.compile(r"\.write_bytes\s*\("),
    re.compile(r"\.unlink\s*\("),
    re.compile(r"\bos\.remove\s*\("),
    re.compile(r"\bos\.unlink\s*\("),
    re.compile(r"\bsubprocess\."),
    re.compile(r"\bos\.system\s*\("),
    re.compile(r"\bshutil\.(copy|move|rmtree|copy2|copyfile|copytree)\s*\("),
)


def workspace_root() -> Path:
    """Resolve workspace from session_cwd if available in a Hermes context.

    Falls back to Path.cwd() for unit tests or non-Hermes environments.
    """
    try:
        from hermes_cli.plugin_session import session_cwd
        cwd = session_cwd()
        if cwd and isinstance(cwd, str):
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


def _batch_path(data_root_path: Path, task_id: object) -> Path:
    return _session(data_root_path, task_id) / "last_v4a_batch"


def _lock_path(data_root_path: Path, task_id: object) -> Path:
    return _session(data_root_path, task_id) / "journal.lock"


def _receipts_path(data_root_path: Path) -> Path:
    return data_root_path / "receipts.jsonl"


def _contained(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return path.resolve(strict=False) != root.resolve(strict=False)


def _file_lock(handle) -> None:
    try:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return
    except ImportError:
        pass
    try:
        import msvcrt
        handle.seek(0)
        if handle.read(1) == b"":
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    except ImportError:
        pass


def _file_unlock(handle) -> None:
    try:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    except ImportError:
        pass
    try:
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except ImportError:
        pass


@contextlib.contextmanager
def _file_mutex(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        _LOCK.acquire()
        try:
            _file_lock(handle)
            try:
                yield
            finally:
                _file_unlock(handle)
        finally:
            _LOCK.release()
    finally:
        handle.close()


def _session_lock(data_root_path: Path, task_id: object):
    return _file_mutex(_lock_path(data_root_path, task_id))


def _receipts_lock(data_root_path: Path):
    return _file_mutex(data_root_path / "receipts.lock")


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


def _remember_unlocked(
    data_root_path: Path,
    task_id: object,
    relative: str,
    original: Path,
    scratch: Path,
    tool_name: str,
    op: str = "write",
    *,
    batch: str | None = None,
    committed: bool = True,
) -> None:
    path = _journal_path(data_root_path, task_id)
    data = _load(path)
    prior = data["files"].get(relative)
    fresh_hash = _fingerprint(original)
    stored_hash = fresh_hash
    if isinstance(prior, dict) and "original_sha256" in prior:
        stored_hash = prior["original_sha256"]
    entry = {
        "relative": relative,
        "original": str(original),
        "scratch": str(scratch),
        "tool": tool_name,
        "op": op,
        "original_sha256": stored_hash,
        "committed": committed,
    }
    if batch:
        entry["batch"] = batch
    data["files"][relative] = entry
    _save(path, data)


def _remember(
    data_root_path: Path,
    task_id: object,
    relative: str,
    original: Path,
    scratch: Path,
    tool_name: str,
    op: str = "write",
) -> None:
    with _session_lock(data_root_path, task_id):
        _remember_unlocked(
            data_root_path, task_id, relative, original, scratch, tool_name, op=op,
        )


_PROTECTED_INSTRUCTION_BASENAMES = frozenset({
    "agents.md", "claude.md", "soul.md", ".cursorrules"
})


def _is_protected_instruction_backstop(path: str) -> bool:
    """Conservative built-in check for protected instruction files.

    Matches basenames case-insensitively per Hermes behavior.
    Used as fail-closed backstop when tools.file_tools_write_guards unavailable.
    """
    try:
        candidates = [os.path.normpath(os.path.expanduser(path))]
        try:
            candidates.append(str(Path(path).expanduser().resolve()))
        except Exception:
            pass

        for candidate in candidates:
            basename = os.path.basename(candidate).lower()
            if basename in _PROTECTED_INSTRUCTION_BASENAMES:
                return True
            parts = candidate.replace("\\", "/").rstrip("/").split("/")
            if len(parts) >= 2 and parts[-2] == ".hermes":
                return True
        return False
    except Exception:
        return True


def _check_write_protection(path: str) -> tuple[str | None, bool]:
    """Check if path would be denied or gated by Hermes write protections.

    Returns (error_message, is_hard_deny).
    - error_message: None if allowed, string if denied/gated
    - is_hard_deny: True if hard-denied, False if approval-gated

    Fail closed: when running inside Hermes (agent module present), ANY import
    or call failure in ANY protection check must fail closed. Only allows
    test-only fallback when agent module truly absent.
    """
    in_hermes = False
    try:
        import sys
        in_hermes = "agent" in sys.modules or "plugins" in sys.modules
    except Exception:
        pass

    try:
        from agent.file_safety import get_write_denied_error, is_write_approval_required

        denied = get_write_denied_error(path)
        if denied:
            return (denied, True)

        if is_write_approval_required(path):
            return (f"Write to {path} requires approval", False)

        try:
            from tools.file_tools_write_guards import _protected_instruction_reason
            reason = _protected_instruction_reason(path)
            if reason:
                return ("Write to protected instruction file requires approval", False)
        except ImportError as e:
            if in_hermes:
                if _is_protected_instruction_backstop(path):
                    return ("Write to protected instruction file requires approval (backstop)", False)
                return (f"Cannot verify instruction file protections: {e}", True)
            if _is_protected_instruction_backstop(path):
                return ("Write to protected instruction file requires approval (backstop)", False)
        except Exception as e:
            if in_hermes:
                return (f"Error checking instruction file protections: {e}", True)
            if _is_protected_instruction_backstop(path):
                return ("Write to protected instruction file requires approval (backstop)", False)

        return (None, False)
    except ImportError:
        if in_hermes:
            return ("Cannot verify write protections (agent.file_safety unavailable in Hermes)", True)
        if _is_protected_instruction_backstop(path):
            return ("Write to protected instruction file requires approval (backstop)", False)
        return (None, False)
    except Exception as e:
        if in_hermes:
            return (f"Error checking write protections: {e}", True)
        if _is_protected_instruction_backstop(path):
            return ("Write to protected instruction file requires approval (backstop)", False)
        return (None, False)


def _resolve_in_workspace(raw_path: str, root: Path) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.resolve(strict=False)


def _relative_to_root(raw_path: object, root: Path) -> str | None:
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    resolved = _resolve_in_workspace(raw_path.strip(), root)
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        text = raw_path.strip().replace("\\", "/")
        if text.startswith("./"):
            text = text[2:]
        return text or None


def plan_tool(
    tool_name: str,
    args: dict | None,
    task_id: object,
    *,
    workspace: Path | None = None,
    storage: Path | None = None,
) -> dict | None:
    payload = args if isinstance(args, dict) else {}
    if tool_name in COMMAND_TOOLS:
        return plan_command(tool_name, payload, workspace=workspace)
    if tool_name not in STAGED_TOOLS:
        return None
    if tool_name == "patch" and _is_v4a(payload):
        return plan_v4a(payload, task_id, workspace=workspace, storage=storage)
    return plan_redirect(
        payload.get("path"), tool_name, task_id, workspace=workspace, storage=storage,
    )


def plan_redirect(
    raw_path: object,
    tool_name: str,
    task_id: object,
    *,
    workspace: Path | None = None,
    storage: Path | None = None,
) -> dict | None:
    """Return a pre_tool_call directive, or None when this call is not staged.

    Returns None (let core handle it) for paths Hermes would deny or gate,
    so those checks happen at write time instead of being silently staged.

    Checks protection on the resolved absolute path (relative paths resolve
    against the session workspace, not process cwd).
    """
    if tool_name not in STAGED_TOOLS:
        return None
    if not isinstance(raw_path, str) or not raw_path.strip():
        return {"action": "block", "message": "stage: a file write needs a path inside the workspace. Nothing was written."}

    root = (workspace or workspace_root()).resolve()
    store = storage or data_root()
    resolved = _resolve_in_workspace(raw_path, root)

    protection_error, is_hard_deny = _check_write_protection(str(resolved))
    if protection_error:
        if is_hard_deny:
            return {
                "action": "block",
                "message": f"stage: write blocked by Hermes protections ({protection_error}). The original file was not changed.",
            }
        return None

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


def _is_v4a(args: dict) -> bool:
    if args.get("mode") == "patch":
        return True
    patch = args.get("patch")
    return isinstance(patch, str) and _V4A_BEGIN in patch


def _parse_v4a(text: str) -> list[dict] | None:
    if not isinstance(text, str) or _V4A_BEGIN not in text:
        return None
    start = text.find(_V4A_BEGIN)
    body = text[start + len(_V4A_BEGIN):]
    end = body.find(_V4A_END)
    if end < 0:
        return None
    body = body[:end]
    operations: list[dict] = []
    current: dict | None = None
    chunks: list[str] = []
    for line in body.splitlines(keepends=True):
        stripped = line.strip("\r\n")
        header = None
        for label, op in _V4A_HEADERS:
            prefix = f"*** {label}:"
            if stripped.startswith(prefix):
                header = (op, stripped[len(prefix):].strip())
                break
        if stripped.startswith("*** ") and header is None:
            if stripped.strip() == "":
                continue
            return None
        if header is not None:
            if current is not None:
                current["body"] = "".join(chunks)
                operations.append(current)
            op, path = header
            if not path:
                return None
            current = {"op": op, "path": path, "header": stripped}
            chunks = []
            continue
        if current is not None:
            chunks.append(line)
    if current is not None:
        current["body"] = "".join(chunks)
        operations.append(current)
    if not operations:
        return None
    return operations


def plan_v4a(
    args: dict,
    task_id: object,
    *,
    workspace: Path | None = None,
    storage: Path | None = None,
) -> dict | None:
    patch_text = args.get("patch")
    operations = _parse_v4a(patch_text if isinstance(patch_text, str) else "")
    if operations is None:
        return {
            "action": "block",
            "message": "stage: that patch could not be parsed. Nothing was changed.",
        }
    root = (workspace or workspace_root()).resolve()
    store = storage or data_root()
    scratch_root = _scratch_root(store, task_id)
    prepared: list[dict] = []
    for operation in operations:
        resolved = _resolve_in_workspace(operation["path"], root)
        protection_error, is_hard_deny = _check_write_protection(str(resolved))
        if protection_error:
            if is_hard_deny:
                return {
                    "action": "block",
                    "message": "stage: that patch includes a path blocked by Hermes protections. Nothing was changed.",
                }
            return {
                "action": "block",
                "message": "stage: that patch includes a path that requires Hermes approval. Nothing was changed.",
            }
        if not _contained(resolved, root) or resolved.is_dir():
            return {
                "action": "block",
                "message": "stage: that patch includes a path that cannot be staged. Nothing was changed.",
            }
        relative = resolved.relative_to(root).as_posix()
        scratch = scratch_root / relative
        if not _contained(scratch, scratch_root):
            return {
                "action": "block",
                "message": "stage: could not place a scratch copy. Nothing was changed.",
            }
        if operation["op"] == "update":
            if not scratch.is_file():
                if not resolved.is_file():
                    return {
                        "action": "block",
                        "message": "stage: patch needs an existing file. Nothing was changed.",
                    }
        prepared.append({**operation, "resolved": resolved, "relative": relative, "scratch": scratch})

    rewritten: list[str] = [_V4A_BEGIN + "\n"]
    batch_id = uuid.uuid4().hex
    has_core_patch = any(item["op"] != "delete" for item in prepared)
    committed = not has_core_patch
    with _session_lock(store, task_id):
        for item in prepared:
            scratch: Path = item["scratch"]
            scratch.parent.mkdir(parents=True, exist_ok=True)
            op = "delete" if item["op"] == "delete" else "write"
            if item["op"] == "update" and not scratch.is_file():
                shutil.copy2(item["resolved"], scratch)
            _remember_unlocked(
                store, task_id, item["relative"], item["resolved"], scratch, "patch",
                op=op, batch=batch_id, committed=committed,
            )
            if item["op"] == "delete":
                continue
            label = "Add File" if item["op"] == "add" else "Update File"
            rewritten.append(f"*** {label}: {scratch}\n")
            body = item.get("body") or ""
            if body and not body.startswith("\n") and not rewritten[-1].endswith("\n"):
                rewritten.append("\n")
            rewritten.append(body)
            if body and not body.endswith("\n"):
                rewritten.append("\n")
        rewritten.append(_V4A_END + "\n")
        if has_core_patch:
            _write_last_batch(store, task_id, batch_id)
    patch_out = "".join(rewritten)
    if "*** Update File:" not in patch_out and "*** Add File:" not in patch_out:
        return {
            "action": "block",
            "message": (
                "stage: deletion is staged. Original files are unchanged until stage_apply."
            ),
        }
    return {"action": "modify", "args": {"patch": patch_out}}


def _line_counts(before: str, after: str) -> tuple[int, int]:
    old_lines = before.splitlines(keepends=True)
    new_lines = after.splitlines(keepends=True)
    added = removed = 0
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "replace":
            removed += i2 - i1
            added += j2 - j1
        elif tag == "delete":
            removed += i2 - i1
        elif tag == "insert":
            added += j2 - j1
    return added, removed


def _read_text(path: Path) -> tuple[str | None, bool]:
    if not path.is_file():
        return "", False
    try:
        return path.read_text(encoding="utf-8"), False
    except UnicodeDecodeError:
        return None, True
    except OSError:
        return "", False


def _unified(path: str, before: str, after: str) -> str:
    old_lines = before.splitlines(keepends=True)
    new_lines = after.splitlines(keepends=True)
    if old_lines and not old_lines[-1].endswith("\n"):
        old_lines[-1] += "\n"
    if new_lines and not new_lines[-1].endswith("\n"):
        new_lines[-1] += "\n"
    return "".join(difflib.unified_diff(old_lines, new_lines, fromfile=f"a/{path}", tofile=f"b/{path}"))


def _entry_conflict(entry: dict) -> bool:
    if "original_sha256" not in entry:
        return True
    original = Path(entry.get("original", ""))
    return _fingerprint(original) != entry.get("original_sha256")


def _entry_counts(entry: dict) -> tuple[int, int, bool]:
    op = entry.get("op") or "write"
    original = Path(entry.get("original", ""))
    scratch = Path(entry.get("scratch", ""))
    if op == "delete":
        before, binary = _read_text(original)
        if binary:
            return 0, original.stat().st_size if original.is_file() else 0, True
        return 0, len((before or "").splitlines()), False
    before, before_binary = _read_text(original)
    after, after_binary = _read_text(scratch)
    if before_binary or after_binary:
        before_size = original.stat().st_size if original.is_file() else 0
        after_size = scratch.stat().st_size if scratch.is_file() else 0
        added = max(after_size - before_size, 0)
        removed = max(before_size - after_size, 0)
        return added, removed, True
    added, removed = _line_counts(before or "", after or "")
    return added, removed, False


def _entry_diff(relative: str, entry: dict) -> str:
    op = entry.get("op") or "write"
    original = Path(entry.get("original", ""))
    scratch = Path(entry.get("scratch", ""))
    if op == "delete":
        before, binary = _read_text(original)
        if binary:
            return "binary file deleted\n"
        return _unified(relative, before or "", "")
    before, before_binary = _read_text(original)
    after, after_binary = _read_text(scratch)
    if before_binary or after_binary:
        return "binary file\n"
    return _unified(relative, before or "", after or "")


def _is_committed(entry: dict) -> bool:
    return entry.get("committed", True) is not False


def _write_last_batch(data_root_path: Path, task_id: object, batch_id: str) -> None:
    path = _batch_path(data_root_path, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(batch_id + "\n", encoding="utf-8")


def _read_last_batch(data_root_path: Path, task_id: object) -> str | None:
    try:
        text = _batch_path(data_root_path, task_id).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _clear_last_batch(data_root_path: Path, task_id: object) -> None:
    path = _batch_path(data_root_path, task_id)
    try:
        path.unlink()
    except OSError:
        pass


def commit_v4a_batch(
    task_id: object,
    batch_id: str | None = None,
    *,
    storage: Path | None = None,
) -> dict:
    store = storage or data_root()
    with _session_lock(store, task_id):
        marker = batch_id or _read_last_batch(store, task_id)
        if not marker:
            return {"committed": []}
        journal = _journal_path(store, task_id)
        data = _load(journal)
        committed: list[str] = []
        for relative, entry in data["files"].items():
            if entry.get("batch") == marker:
                entry["committed"] = True
                committed.append(relative)
        if committed:
            _save(journal, data)
        if _read_last_batch(store, task_id) == marker:
            _clear_last_batch(store, task_id)
    return {"committed": committed}


def drop_v4a_batch(
    task_id: object,
    batch_id: str | None = None,
    *,
    storage: Path | None = None,
) -> dict:
    store = storage or data_root()
    with _session_lock(store, task_id):
        marker = batch_id or _read_last_batch(store, task_id)
        if not marker:
            return {"dropped": []}
        journal = _journal_path(store, task_id)
        data = _load(journal)
        remaining: dict = {}
        dropped: list[str] = []
        for relative, entry in data["files"].items():
            if entry.get("batch") == marker:
                scratch = Path(entry.get("scratch", ""))
                if scratch.is_file():
                    try:
                        scratch.unlink()
                    except OSError:
                        pass
                dropped.append(relative)
            else:
                remaining[relative] = entry
        if remaining:
            _save(journal, {"files": remaining})
        elif journal.exists():
            journal.unlink()
        if _read_last_batch(store, task_id) == marker:
            _clear_last_batch(store, task_id)
    return {"dropped": dropped}


def settle_v4a_batch(
    task_id: object,
    *,
    failed: bool,
    storage: Path | None = None,
) -> dict:
    if failed:
        return drop_v4a_batch(task_id, storage=storage)
    return commit_v4a_batch(task_id, storage=storage)


def result_looks_failed(result: object) -> bool:
    if not isinstance(result, str):
        return False
    lower = result.lower()
    if re.search(r"\b0 errors?\b", lower):
        return False
    return bool(re.search(r"\b(error|failed|failure)\b", lower))


def stage_status(task_id: object, *, storage: Path | None = None) -> dict:
    store = storage or data_root()
    with _session_lock(store, task_id):
        data = _load(_journal_path(store, task_id))
    pending = []
    for relative, entry in sorted(data["files"].items()):
        if not _is_committed(entry):
            continue
        scratch = Path(entry.get("scratch", ""))
        original = Path(entry.get("original", ""))
        op = entry.get("op") or "write"
        added, removed, binary = _entry_counts(entry)
        ready = original.is_file() if op == "delete" else scratch.is_file()
        size = 0
        if op == "delete" and original.is_file():
            size = original.stat().st_size
        elif scratch.is_file():
            size = scratch.stat().st_size
        pending.append({
            "path": relative,
            "original": entry.get("original", ""),
            "bytes": size,
            "ready": ready,
            "op": op,
            "added": added,
            "removed": removed,
            "conflict": _entry_conflict(entry),
            "binary": binary,
        })
    return {"pending": pending}


def parse_paths_parameter(args: object) -> tuple[list[str] | None, str | None]:
    """Return (paths, error). ``None`` paths means all pending files; ``[]`` means none."""
    if not isinstance(args, dict) or "paths" not in args:
        return None, None
    raw = args["paths"]
    if not isinstance(raw, list):
        return None, "stage: paths must be an array of workspace-relative strings."
    if not raw:
        return [], None
    cleaned: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            return None, "stage: paths must be an array of workspace-relative strings."
        cleaned.append(item)
    return cleaned, None


def stage_diff(
    task_id: object,
    paths: list | None = None,
    *,
    workspace: Path | None = None,
    storage: Path | None = None,
) -> dict:
    if paths is not None and len(paths) == 0:
        return {"files": [], "errors": []}
    root = (workspace or workspace_root()).resolve()
    store = storage or data_root()
    with _session_lock(store, task_id):
        data = _load(_journal_path(store, task_id))
    visible = {key: entry for key, entry in data["files"].items() if _is_committed(entry)}
    selected, errors = _select_entries(visible, paths, root)
    files = []
    for relative, entry in selected:
        files.append({
            "path": relative,
            "conflict": _entry_conflict(entry),
            "op": entry.get("op") or "write",
            "diff": _entry_diff(relative, entry),
        })
    return {"files": files, "errors": errors}


def _select_entries(files: dict, paths: list | None, root: Path) -> tuple[list[tuple[str, dict]], list[dict]]:
    if paths is None:
        return [(relative, files[relative]) for relative in sorted(files)], []
    if len(paths) == 0:
        return [], []
    selected: list[tuple[str, dict]] = []
    errors: list[dict] = []
    seen: set[str] = set()
    for raw in paths:
        relative = _relative_to_root(raw, root) if isinstance(raw, str) else None
        key = relative if relative in files else (raw if isinstance(raw, str) and raw in files else None)
        if key is None:
            errors.append({"path": relative or str(raw), "error": "that path is not staged"})
            continue
        if key in seen:
            continue
        seen.add(key)
        selected.append((key, files[key]))
    return selected, errors


def _apply_with_protection_check(destination: Path, scratch: Path, mode_before: int | None) -> str | None:
    """Apply scratch to destination with Hermes protection check, byte-exact copy.

    Returns error message on failure, None on success.

    Uses byte-exact copy (copy2 for binary safety), atomic temp+replace, mode preservation.
    Checks protections before writing. Fails closed if protection check fails.
    """
    protection_error, is_hard_deny = _check_write_protection(str(destination))
    del is_hard_deny
    if protection_error:
        return f"Hermes write protection: {protection_error}"

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
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
        return f"unexpected error during apply: {e}"


def _truncate_diff(text: str) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= RECEIPT_DIFF_LIMIT:
        return text
    clipped = encoded[:RECEIPT_DIFF_LIMIT]
    return clipped.decode("utf-8", errors="ignore") + "\n"


def _append_receipts(store: Path, records: list[dict]) -> None:
    if not records:
        return
    path = _receipts_path(store)
    with _receipts_lock(store):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=True) + "\n")


def _receipt_record(task_id: object, relative: str, entry: dict, before: str | None, after: str | None, diff: str) -> dict:
    return {
        "time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "task_id": safe_task_id(task_id),
        "path": relative,
        "before_sha256": before,
        "after_sha256": after,
        "diff": _truncate_diff(diff),
    }


def stage_apply(
    task_id: object,
    paths: list | None = None,
    *,
    workspace: Path | None = None,
    storage: Path | None = None,
) -> dict:
    root = (workspace or workspace_root()).resolve()
    store = storage or data_root()
    journal = _journal_path(store, task_id)
    scratch_root = _scratch_root(store, task_id)
    if paths is not None and len(paths) == 0:
        return {"applied": [], "errors": [], "receipts": []}
    receipts: list[dict] = []
    with _session_lock(store, task_id):
        data = _load(journal)
        selected, errors = _select_entries(data["files"], paths, root)
        filtering = paths is not None
        selected_keys = {relative for relative, _entry in selected}
        applied: list[str] = []
        remaining: dict = {}
        for relative, entry in data["files"].items():
            if filtering and relative not in selected_keys:
                remaining[relative] = entry
                continue
            if not _is_committed(entry):
                remaining[relative] = entry
                continue
            original = Path(entry.get("original", ""))
            scratch = Path(entry.get("scratch", ""))
            op = entry.get("op") or "write"
            if not _contained(original, root):
                errors.append({"path": relative, "error": "path escaped the workspace"})
                remaining[relative] = entry
                continue
            if op != "delete" and not _contained(scratch, scratch_root):
                errors.append({"path": relative, "error": "path escaped the workspace"})
                remaining[relative] = entry
                continue
            if op != "delete" and not scratch.is_file():
                errors.append({"path": relative, "error": "scratch copy is missing"})
                remaining[relative] = entry
                continue
            expected_present = "original_sha256" in entry
            current = _fingerprint(original)
            expected = entry.get("original_sha256")
            if not expected_present or current != expected:
                errors.append({"path": relative, "error": CONFLICT_HINT})
                remaining[relative] = entry
                continue
            destination = original.resolve(strict=False)
            diff_text = _entry_diff(relative, entry)
            if op == "delete":
                protection_error, is_hard_deny = _check_write_protection(str(destination))
                del is_hard_deny
                if protection_error:
                    errors.append({"path": relative, "error": f"Hermes write protection: {protection_error}"})
                    remaining[relative] = entry
                    continue
                try:
                    destination.unlink()
                except OSError as exc:
                    errors.append({"path": relative, "error": f"could not delete the original file: {exc}"})
                    remaining[relative] = entry
                    continue
                receipts.append(_receipt_record(task_id, relative, entry, current, None, diff_text))
                if scratch.is_file() or scratch.exists():
                    try:
                        scratch.unlink()
                    except OSError:
                        pass
                applied.append(relative)
                continue
            mode_before = stat.S_IMODE(destination.stat().st_mode) if destination.exists() else None
            apply_error = _apply_with_protection_check(destination, scratch, mode_before)
            if apply_error:
                errors.append({"path": relative, "error": apply_error})
                remaining[relative] = entry
                continue
            after_hash = _fingerprint(destination)
            receipts.append(_receipt_record(task_id, relative, entry, current, after_hash, diff_text))
            scratch.unlink()
            applied.append(relative)
        if remaining:
            _save(journal, {"files": remaining})
        elif journal.exists():
            journal.unlink()
    _append_receipts(store, receipts)
    return {"applied": applied, "errors": errors, "receipts": receipts}


def stage_discard(
    task_id: object,
    paths: list | None = None,
    *,
    workspace: Path | None = None,
    storage: Path | None = None,
) -> dict:
    root = (workspace or workspace_root()).resolve()
    store = storage or data_root()
    journal = _journal_path(store, task_id)
    session = _session(store, task_id)
    clear_session = False
    if paths is not None and len(paths) == 0:
        return {"discarded": [], "errors": []}
    with _session_lock(store, task_id):
        data = _load(journal)
        selected, errors = _select_entries(data["files"], paths, root)
        if paths is None:
            discarded = sorted(data["files"])
            clear_session = True
        else:
            discarded = [relative for relative, _entry in selected]
            remaining = dict(data["files"])
            for relative, entry in selected:
                remaining.pop(relative, None)
                scratch = Path(entry.get("scratch", ""))
                if scratch.is_file():
                    try:
                        scratch.unlink()
                    except OSError:
                        pass
            if remaining:
                _save(journal, {"files": remaining})
            else:
                clear_session = True
        if clear_session and session.exists():
            shutil.rmtree(session, ignore_errors=True)
    return {"discarded": discarded, "errors": errors}


def stage_receipts(limit: int = 10, *, storage: Path | None = None) -> dict:
    store = storage or data_root()
    path = _receipts_path(store)
    records: list[dict] = []
    with _receipts_lock(store):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            records.append(item)
    if limit < 1:
        limit = 10
    return {"receipts": records[-limit:]}


def plan_command(
    tool_name: str,
    args: dict,
    *,
    workspace: Path | None = None,
) -> dict | None:
    root = (workspace or workspace_root()).resolve()
    if tool_name == "terminal":
        command = args.get("command")
        workdir = args.get("workdir")
        if not isinstance(command, str) or not command.strip():
            return None
        if _terminal_mutates(command, root, workdir if isinstance(workdir, str) else None):
            return {"action": "block", "message": SHELL_BLOCK}
        return None
    if tool_name == "execute_code":
        code = args.get("code")
        if isinstance(code, str) and _code_mutates(code):
            return {"action": "block", "message": SHELL_BLOCK}
        return None
    return None


def _code_mutates(code: str) -> bool:
    return any(pattern.search(code) for pattern in _CODE_WRITE)


def _mask_quoted(command: str) -> str:
    out: list[str] = []
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if ch in {"'", '"'}:
            quote = ch
            out.append(" ")
            i += 1
            while i < n:
                if quote == '"' and command[i] == "\\" and i + 1 < n:
                    out.append(" ")
                    out.append(" ")
                    i += 2
                    continue
                if command[i] == quote:
                    out.append(" ")
                    i += 1
                    break
                out.append(" ")
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _redirect_destinations(masked: str) -> list[str | None]:
    destinations: list[str | None] = []
    i = 0
    length = len(masked)
    while i < length:
        if masked[i] != ">":
            i += 1
            continue
        end = i + 2 if i + 1 < length and masked[i + 1] == ">" else i + 1
        prev = masked[i - 1] if i else ""
        nxt = masked[end] if end < length else ""
        if prev in "-=~" or nxt in "&=":
            i = end
            continue
        rest = masked[end:].lstrip()
        if not rest:
            destinations.append(None)
        else:
            token = rest.split(None, 1)[0]
            destinations.append(token)
        i = end
    return destinations


def _terminal_mutates(command: str, root: Path, workdir: str | None) -> bool:
    masked = _mask_quoted(command)
    for dest in _redirect_destinations(masked):
        if dest is None:
            return True
        if dest.startswith("&"):
            continue
        if dest in {"/dev/null", "/dev/zero", "/dev/stderr", "/dev/stdout"}:
            continue
        if _target_in_workspace_or_unknown(dest, root, workdir):
            return True
    if re.search(r"\btee\b", command) or re.search(r"\btruncate\b", command):
        return True
    if re.search(r"\bgit\s+apply\b", command):
        return True
    if re.search(r"\bgit\s+checkout\b", command):
        return True
    if re.search(r"\bgit\s+restore\b", command):
        return True
    if re.search(r"\bgit\s+reset\b[^\n]*--hard", command):
        return True
    if re.search(r"\bsed\b[^\n]*-i", command) or re.search(r"\bperl\b[^\n]*-i", command):
        return True
    for chunk in re.split(r"&&|\|\||;|\n|\|", command):
        if _mutating_tokens(chunk, root, workdir):
            return True
    return False


def _mutating_tokens(chunk: str, root: Path, workdir: str | None) -> bool:
    try:
        tokens = shlex.split(chunk, posix=True)
    except ValueError:
        return bool(chunk.strip())
    if not tokens:
        return False
    name = Path(tokens[0]).name
    if name in {"rm", "rmdir"}:
        targets = [token for token in tokens[1:] if not token.startswith("-")]
        if not targets:
            return True
        return any(_target_in_workspace_or_unknown(token, root, workdir) for token in targets)
    if name == "mv":
        targets = [token for token in tokens[1:] if not token.startswith("-")]
        if len(targets) < 2:
            return True
        return any(_target_in_workspace_or_unknown(token, root, workdir) for token in targets)
    if name == "cp":
        targets = [token for token in tokens[1:] if not token.startswith("-")]
        if len(targets) < 2:
            return True
        return _target_in_workspace_or_unknown(targets[-1], root, workdir)
    return False


def _target_in_workspace_or_unknown(raw: str, root: Path, workdir: str | None) -> bool:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        base = root
        if workdir:
            work = Path(workdir).expanduser()
            base = work if work.is_absolute() else (root / work)
        candidate = base / candidate
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
        return True
    except ValueError:
        return False
