from __future__ import annotations

import ast
import argparse
from pathlib import Path

from .exceptions import PySplitError
from .history import record_split_history, rollback_last
from .resolver import TargetSpec, parse_target, resolve_target
from .splitter import split_function
from .utils import path_to_module_parts



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="Splinter",
        description="Split a top-level Python function into its own module and rewrite imports.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    splitfunc = subparsers.add_parser(
        "splitfunc",
        help="Extract a function into modules/<function_name>.py",
    )
    splitfunc.add_argument(
        "target",
        help="Target in the form module.function or package.module.function",
    )
    splitfunc.add_argument(
        "--cwd",
        default=".",
        help="Project root to resolve from. Defaults to current directory.",
    )
    splitfunc.add_argument(
        "--preview",
        action="store_true",
        help="Show the planned changes without writing files.",
    )

    splitall = subparsers.add_parser(
        "splitall",
        help="Extract all top-level functions from a file or all Python files in a directory",
    )
    splitall.add_argument(
        "path",
        nargs="?",
        help="Python file to split, like main.py or package/module.py",
    )
    splitall.add_argument(
        "--dir",
        dest="directory",
        help="Directory whose top-level Python files should be split",
    )
    splitall.add_argument(
        "--cwd",
        default=".",
        help="Project root to resolve from. Defaults to current directory.",
    )
    splitall.add_argument(
        "--preview",
        action="store_true",
        help="Show the planned changes without writing files.",
    )

    undo = subparsers.add_parser(
        "undo",
        help="Roll back the last split operation",
    )
    undo.add_argument(
        "count",
        nargs="?",
        type=int,
        default=1,
        help="Number of recorded operations to roll back. Defaults to 1.",
    )
    undo.add_argument(
        "--cwd",
        default=".",
        help="Project root to resolve from. Defaults to current directory.",
    )

    return parser



def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "splitfunc":
            cwd = Path(args.cwd)
            spec = parse_target(args.target)
            resolved = resolve_target(spec, cwd=cwd)
            result = split_function(resolved, preview=args.preview)
            _print_split_result(result)
            print(f"Updated: {result.module_file}")
            print(f"Created: {result.new_module_file}")
            print(f"Inserted import: {result.import_statement}")
            if not args.preview:
                history_file = record_split_history(cwd, f"splitfunc {args.target}", [result])
                print(f"Recorded rollback history: {history_file}")
            return 0
        if args.command == "splitall":
            cwd = Path(args.cwd)
            split_count = _split_all(args.path, args.directory, cwd, preview=args.preview)
            if split_count == 0:
                print("No top-level functions found.")
            else:
                action = "Would split" if args.preview else "Split"
                print(f"{action} {split_count} function(s).")
            return 0
        if args.command == "undo":
            undo_count, history_file = rollback_last(Path(args.cwd), args.count)
            print(f"Rolled back {undo_count} operation(s).")
            print(f"Updated rollback history: {history_file}")
            return 0
    except PySplitError as exc:
        print(f"Splinter error: {exc}")
        return 1

    parser.print_help()
    return 1



def _split_all(path_arg: str | None, directory_arg: str | None, cwd: Path, *, preview: bool) -> int:
    target_files = _resolve_splitall_files(path_arg, directory_arg, cwd)
    split_count = 0
    results = []

    for file_path in target_files:
        file_results = _split_all_in_file(file_path, cwd, preview=preview)
        results.extend(file_results)
        split_count += len(file_results)

    if results and not preview:
        descriptor = path_arg if path_arg else f"--dir {directory_arg}"
        history_file = record_split_history(cwd, f"splitall {descriptor}", results)
        print(f"Recorded rollback history: {history_file}")

    return split_count



def _resolve_splitall_files(path_arg: str | None, directory_arg: str | None, cwd: Path) -> list[Path]:
    if bool(path_arg) == bool(directory_arg):
        raise PySplitError("splitall requires either a file path or --dir, but not both.")

    if directory_arg:
        directory = (cwd / directory_arg).resolve()
        if not directory.exists() or not directory.is_dir():
            raise PySplitError(f"Directory not found: {directory}")

        return sorted(
            file_path
            for file_path in directory.glob("*.py")
            if file_path.name != "__init__.py"
        )

    file_path = (cwd / (path_arg or "")).resolve()
    if not file_path.exists() or not file_path.is_file():
        raise PySplitError(f"File not found: {file_path}")
    if file_path.suffix != ".py":
        raise PySplitError(f"splitall only supports Python files: {file_path}")

    return [file_path]



def _split_all_in_file(file_path: Path, cwd: Path, *, preview: bool) -> list:
    function_names = _list_top_level_function_names(file_path)
    module_path = _module_path_from_file(file_path, cwd)
    results = []

    for function_name in function_names:
        spec = TargetSpec(module_path=module_path, function_name=function_name)
        resolved = resolve_target(spec, cwd=cwd)
        result = split_function(resolved, preview=preview)
        results.append(result)
        _print_split_result(result)
        print(f"Updated: {result.module_file}")
        print(f"Created: {result.new_module_file}")
        print(f"Inserted import: {result.import_statement}")

    return results



def _list_top_level_function_names(file_path: Path) -> list[str]:
    try:
        tree = ast.parse(file_path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        raise PySplitError(f"Could not parse '{file_path}': {exc}") from exc

    return [
        stmt.name
        for stmt in tree.body
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]



def _module_path_from_file(file_path: Path, cwd: Path) -> str:
    try:
        return ".".join(path_to_module_parts(file_path, cwd.resolve()))
    except ValueError as exc:
        raise PySplitError(
            f"File '{file_path}' is not inside the configured cwd '{cwd.resolve()}'."
        ) from exc


def _print_split_result(result) -> None:
    action = "Would split" if result.preview else "Split"
    print(f"{action} '{result.function_name}' successfully.")