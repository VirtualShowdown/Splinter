from __future__ import annotations

import ast
import argparse
import fnmatch
import os
import sys
from pathlib import Path

from colorama import Fore, Style, just_fix_windows_console

from .config import load_project_config
from .exceptions import PySplitError
from .history import record_split_history, rollback_last
from .resolver import TargetSpec, parse_target, resolve_target
from .splitter import GroupSplitResult, SplitOptions, build_function_call_groups, split_function, split_group
from .utils import path_to_module_parts

just_fix_windows_console()



def build_parser(config: dict | None = None) -> argparse.ArgumentParser:
    config = config or {}
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
    splitfunc.add_argument(
        "--validate",
        action="store_true",
        help="Validate the generated Python source before writing files.",
    )
    splitfunc.add_argument(
        "--output-package",
        default="modules",
        help="Package name to create extracted modules in. Defaults to 'modules'.",
    )
    splitfunc.set_defaults(
        output_package=config.get("output_package", "modules"),
        validate=config.get("validate", False),
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
    splitall.add_argument(
        "--validate",
        action="store_true",
        help="Validate the generated Python source before writing files.",
    )
    splitall.add_argument(
        "--output-package",
        default="modules",
        help="Package name to create extracted modules in. Defaults to 'modules'.",
    )
    splitall.add_argument(
        "--include",
        help="Comma-separated function name patterns to include, like 'main,run_*'.",
    )
    splitall.add_argument(
        "--exclude",
        help="Comma-separated function name patterns to exclude, like 'main,_*'.",
    )
    splitall.add_argument(
        "--public-only",
        action="store_true",
        help="Only split public top-level functions whose names do not start with an underscore.",
    )
    splitall.add_argument(
        "--related",
        action="store_true",
        help=(
            "Group functions that reference each other into a single shared module file "
            "instead of splitting each function into its own file."
        ),
    )
    splitall.set_defaults(
        output_package=config.get("output_package", "modules"),
        validate=config.get("validate", False),
        public_only=config.get("public_only", False),
        include=config.get("include"),
        exclude=config.get("exclude"),
        related=config.get("related", False),
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
    # Quick pre-parse to locate --cwd before loading the project config.
    _pre = argparse.ArgumentParser(add_help=False)
    _pre.add_argument("--cwd", default=".")
    pre_args, _ = _pre.parse_known_args(argv)

    config = load_project_config(Path(pre_args.cwd))
    parser = build_parser(config)
    args = parser.parse_args(argv)

    try:
        if args.command == "splitfunc":
            cwd = Path(args.cwd)
            spec = parse_target(args.target)
            resolved = resolve_target(spec, cwd=cwd)
            options = _build_split_options(args)
            result = split_function(resolved, options=options)
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
            options = _build_split_options(args)
            split_count = _split_all(
                args.path,
                args.directory,
                cwd,
                options=options,
                include_patterns=_parse_patterns(args.include),
                exclude_patterns=_parse_patterns(args.exclude),
                public_only=args.public_only,
                related=args.related,
            )
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



def _split_all(
    path_arg: str | None,
    directory_arg: str | None,
    cwd: Path,
    *,
    options: SplitOptions,
    include_patterns: list[str],
    exclude_patterns: list[str],
    public_only: bool,
    related: bool = False,
) -> int:
    target_files = _resolve_splitall_files(path_arg, directory_arg, cwd)
    split_count = 0
    results = []

    for file_path in target_files:
        file_results = _split_all_in_file(
            file_path,
            cwd,
            options=options,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
            public_only=public_only,
            related=related,
        )
        results.extend(file_results)
        split_count += sum(
            len(r.function_names) if isinstance(r, GroupSplitResult) else 1
            for r in file_results
        )

    if results and not options.preview:
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



def _split_all_in_file(
    file_path: Path,
    cwd: Path,
    *,
    options: SplitOptions,
    include_patterns: list[str],
    exclude_patterns: list[str],
    public_only: bool,
    related: bool = False,
) -> list:
    function_names = _list_top_level_function_names(
        file_path,
        include_patterns=include_patterns,
        exclude_patterns=exclude_patterns,
        public_only=public_only,
    )
    module_path = _module_path_from_file(file_path, cwd)
    results = []

    if not related:
        for function_name in function_names:
            spec = TargetSpec(module_path=module_path, function_name=function_name)
            resolved = resolve_target(spec, cwd=cwd)
            result = split_function(resolved, options=options)
            results.append(result)
            _print_split_result(result)
            print(f"Updated: {result.module_file}")
            print(f"Created: {result.new_module_file}")
            print(f"Inserted import: {result.import_statement}")
        return results

    # --related: group functions by mutual references before splitting.
    source_text = file_path.read_text(encoding="utf-8")
    groups = build_function_call_groups(source_text, function_names, file_path)

    for group in groups:
        if len(group) == 1:
            function_name = group[0]
            spec = TargetSpec(module_path=module_path, function_name=function_name)
            resolved = resolve_target(spec, cwd=cwd)
            result = split_function(resolved, options=options)
            results.append(result)
            _print_split_result(result)
            print(f"Updated: {result.module_file}")
            print(f"Created: {result.new_module_file}")
            print(f"Inserted import: {result.import_statement}")
        else:
            primary = group[0]
            spec = TargetSpec(module_path=module_path, function_name=primary)
            resolved = resolve_target(spec, cwd=cwd)
            result = split_group(resolved, group, options=options)
            results.append(result)
            _print_group_result(result)

    return results



def _list_top_level_function_names(
    file_path: Path,
    *,
    include_patterns: list[str],
    exclude_patterns: list[str],
    public_only: bool,
) -> list[str]:
    try:
        tree = ast.parse(file_path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        raise PySplitError(f"Could not parse '{file_path}': {exc}") from exc

    function_names = [
        stmt.name
        for stmt in tree.body
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]

    if public_only:
        function_names = [name for name in function_names if not name.startswith("_")]
    if include_patterns:
        function_names = [
            name for name in function_names if any(fnmatch.fnmatchcase(name, pattern) for pattern in include_patterns)
        ]
    if exclude_patterns:
        function_names = [
            name for name in function_names if not any(fnmatch.fnmatchcase(name, pattern) for pattern in exclude_patterns)
        ]

    return function_names



def _module_path_from_file(file_path: Path, cwd: Path) -> str:
    try:
        return ".".join(path_to_module_parts(file_path, cwd.resolve()))
    except ValueError as exc:
        raise PySplitError(
            f"File '{file_path}' is not inside the configured cwd '{cwd.resolve()}'."
        ) from exc


def _print_split_result(result) -> None:
    action = "Would split" if result.preview else "Split"
    verb = _color_text(action, Fore.CYAN if result.preview else Fore.GREEN)
    function_name = _color_text(result.function_name, Fore.MAGENTA)
    print(f"{verb} '{function_name}' successfully.")
    if result.preview:
        for diff in result.preview_diffs:
            _print_preview_diff(diff)


def _print_group_result(result: GroupSplitResult) -> None:
    action = "Would split" if result.preview else "Split"
    verb = _color_text(action, Fore.CYAN if result.preview else Fore.GREEN)
    names_str = _color_text(", ".join(f"'{n}'" for n in result.function_names), Fore.MAGENTA)
    print(f"{verb} related group [{names_str}] → {result.new_module_file.name}")
    if result.preview:
        for diff in result.preview_diffs:
            _print_preview_diff(diff)
    print(f"Updated: {result.module_file}")
    print(f"Created: {result.new_module_file}")
    print(f"Inserted import: {result.import_statement}")


def _parse_patterns(raw_patterns: str | list | None) -> list[str]:
    if raw_patterns is None:
        return []
    if isinstance(raw_patterns, list):
        return [str(p).strip() for p in raw_patterns if str(p).strip()]
    return [pattern.strip() for pattern in raw_patterns.split(",") if pattern.strip()]


def _build_split_options(args: argparse.Namespace) -> SplitOptions:
    return SplitOptions(
        preview=args.preview,
        output_package=args.output_package,
        validate=args.validate,
    )


def _print_preview_diff(diff: str) -> None:
    for line in diff.splitlines():
        print(_colorize_diff_line(line))


def _colorize_diff_line(line: str) -> str:
    if line.startswith("+++") or line.startswith("---"):
        return _color_text(line, Fore.CYAN)
    if line.startswith("@@"):
        return _color_text(line, Fore.YELLOW)
    if line.startswith("+"):
        return _color_text(line, Fore.GREEN)
    if line.startswith("-"):
        return _color_text(line, Fore.RED)
    return line


def _color_text(text: str, color: str) -> str:
    if not _supports_color():
        return text
    return f"{color}{text}{Style.RESET_ALL}"


def _supports_color() -> bool:
    if os.getenv("NO_COLOR"):
        return False
    return sys.stdout.isatty()