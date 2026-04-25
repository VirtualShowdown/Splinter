from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .exceptions import PySplitError
from .models import FileChange, GroupSplitResult, SplitResult
from .utils import write_text_preserving_newlines

HISTORY_FILENAME = ".manasplice_history.json"


@dataclass(slots=True)
class HistoryEntry:
    command: str
    changes: list[FileChange]


def record_split_history(cwd: Path, command: str, results: list[SplitResult | GroupSplitResult]) -> Path:
    if not results:
        raise PySplitError("Cannot record rollback history for an empty split operation.")

    return record_change_history(cwd, command, _coalesce_changes(results))


def record_change_history(cwd: Path, command: str, changes: list[FileChange]) -> Path:
    if not changes:
        raise PySplitError("Cannot record rollback history for an empty operation.")

    history_file = _history_file(cwd)
    history = _load_history(history_file)
    history.append(_serialize_entry(cwd, HistoryEntry(command=command, changes=changes)))
    _write_history(history_file, history)
    return history_file


def rollback_last(cwd: Path, count: int = 1) -> tuple[int, Path]:
    if count < 1:
        raise PySplitError("undo count must be at least 1.")

    history_file = _history_file(cwd)
    history = _load_history(history_file)
    if not history:
        raise PySplitError("No rollback history found.")
    if count > len(history):
        raise PySplitError(f"Cannot undo {count} operation(s); only {len(history)} recorded.")

    remaining = history[:-count]
    to_rollback = history[-count:]
    cwd = cwd.resolve()

    for entry_data in reversed(to_rollback):
        entry = _deserialize_entry(cwd, entry_data)
        for change in reversed(entry.changes):
            _restore_change(change, cwd)

    _write_history(history_file, remaining)
    return count, history_file


def _history_file(cwd: Path) -> Path:
    return cwd.resolve() / HISTORY_FILENAME


def _coalesce_changes(results: list[SplitResult | GroupSplitResult]) -> list[FileChange]:
    coalesced: dict[Path, FileChange] = {}
    order: list[Path] = []

    for result in results:
        for change in result.file_changes:
            path = change.path.resolve()
            existing = coalesced.get(path)
            if existing is None:
                coalesced[path] = FileChange(
                    path=path,
                    existed_before=change.existed_before,
                    before_text=change.before_text,
                    after_text=change.after_text,
                )
                order.append(path)
                continue

            existing.after_text = change.after_text

    return [coalesced[path] for path in order]


def _serialize_entry(cwd: Path, entry: HistoryEntry) -> dict[str, object]:
    root = cwd.resolve()
    return {
        "command": entry.command,
        "changes": [
            {
                "path": str(change.path.resolve().relative_to(root)),
                "existed_before": change.existed_before,
                "before_text": change.before_text,
                "after_text": change.after_text,
            }
            for change in entry.changes
        ],
    }


def _deserialize_entry(cwd: Path, data: dict[str, object]) -> HistoryEntry:
    changes: list[FileChange] = []
    raw_changes = data.get("changes", [])
    if not isinstance(raw_changes, list):
        raise PySplitError("Rollback history is corrupted.")

    for change_data in raw_changes:
        if not isinstance(change_data, dict):
            raise PySplitError("Rollback history is corrupted.")

        path_value = change_data.get("path")
        existed_before = change_data.get("existed_before")
        before_text = change_data.get("before_text")
        after_text = change_data.get("after_text")
        if not isinstance(path_value, str) or not isinstance(existed_before, bool):
            raise PySplitError("Rollback history is corrupted.")
        if not isinstance(before_text, str) or not isinstance(after_text, str):
            raise PySplitError("Rollback history is corrupted.")

        changes.append(
            FileChange(
                path=_resolve_history_path(cwd, path_value),
                existed_before=existed_before,
                before_text=before_text,
                after_text=after_text,
            )
        )

    return HistoryEntry(command=str(data.get("command", "split")), changes=changes)


def _resolve_history_path(cwd: Path, path_value: str) -> Path:
    raw_path = Path(path_value)
    if raw_path.is_absolute():
        raise PySplitError("Rollback history contains an absolute path.")

    root = cwd.resolve()
    resolved = (root / raw_path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PySplitError("Rollback history contains a path outside the project root.") from exc
    return resolved


def _restore_change(change: FileChange, cwd: Path) -> None:
    if change.existed_before:
        change.path.parent.mkdir(parents=True, exist_ok=True)
        write_text_preserving_newlines(change.path, change.before_text)
        return

    if change.path.exists():
        change.path.unlink()
    _prune_empty_directories(change.path.parent, cwd.resolve())


def _prune_empty_directories(directory: Path, root: Path) -> None:
    current = directory.resolve()
    while current.exists() and current.is_dir():
        if current == root:
            break
        try:
            current.relative_to(root)
        except ValueError:
            break
        try:
            next(current.iterdir())
            break
        except StopIteration:
            parent = current.parent
            current.rmdir()
            current = parent


def _load_history(history_file: Path) -> list[dict[str, object]]:
    if not history_file.exists():
        return []

    try:
        data = json.loads(history_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PySplitError(f"Could not read rollback history '{history_file}': {exc}") from exc

    if not isinstance(data, list):
        raise PySplitError(f"Rollback history '{history_file}' is corrupted.")
    return data


def _write_history(history_file: Path, history: list[dict[str, object]]) -> None:
    history_file.write_text(json.dumps(history, indent=2) + "\n", encoding="utf-8")
