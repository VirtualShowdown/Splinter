from __future__ import annotations

import argparse
import ast
import fnmatch
import os
import sys
from pathlib import Path

from colorama import Fore, Style, just_fix_windows_console

from .config import load_project_config
from .exceptions import PySplitError
from .history import record_split_history, rollback_last
from .resolver import TargetSpec, parse_target, resolve_target
from .splitter import (
    FileChange,
    GroupSplitResult,
    SplitOptions,
    SplitResult,
    build_function_call_groups,
    split_function,
    split_group,
)
from .utils import path_to_module_parts, read_python_source

just_fix_windows_console()


def build_parser(config: dict | None = None) -> argparse.ArgumentParser:
    config = config or {}
    parser = argparse.ArgumentParser(
        prog="splinter",
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
    splitfunc.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing generated module if the output path already exists.",
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
    splitall.add_argument(
        "--force",
        action="store_true",
        help="Replace existing generated modules if output paths already exist.",
    )
    splitall.set_defaults(
        output_package=config.get("output_package", "modules"),
        validate=config.get("validate", False),
        public_only=config.get("public_only", False),
        include=config.get("include"),
        exclude=config.get("exclude"),
        related=config.get("related", False),
    )

    check = subparsers.add_parser(
        "check",
        help="Inspect whether a split can be applied without writing files",
    )
    check.add_argument(
        "target_or_path",
        nargs="?",
        help="Target like module.function, or a Python file for splitall-style checks.",
    )
    check.add_argument(
        "--dir",
        dest="directory",
        help="Directory whose top-level Python files should be checked.",
    )
    check.add_argument(
        "--cwd",
        default=".",
        help="Project root to resolve from. Defaults to current directory.",
    )
    check.add_argument(
        "--validate",
        action="store_true",
        help="Validate the generated Python source during the check.",
    )
    check.add_argument(
        "--output-package",
        default="modules",
        help="Package name to create extracted modules in. Defaults to 'modules'.",
    )
    check.add_argument(
        "--include",
        help="Comma-separated function name patterns to include when checking files.",
    )
    check.add_argument(
        "--exclude",
        help="Comma-separated function name patterns to exclude when checking files.",
    )
    check.add_argument(
        "--public-only",
        action="store_true",
        help="Only check public top-level functions whose names do not start with an underscore.",
    )
    check.add_argument(
        "--related",
        action="store_true",
        help="Check related functions as grouped output modules.",
    )
    check.add_argument(
        "--force",
        action="store_true",
        help="Allow checks to pass when an output module already exists.",
    )
    check.set_defaults(
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
            _print_split_result(result, show_diffs=args.preview)
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
        if args.command == "check":
            cwd = Path(args.cwd)
            options = _build_split_options(args, preview=True)
            check_count = _check(
                args.target_or_path,
                args.directory,
                cwd,
                options=options,
                include_patterns=_parse_patterns(args.include),
                exclude_patterns=_parse_patterns(args.exclude),
                public_only=args.public_only,
                related=args.related,
            )
            if check_count == 0:
                print("Check passed: no top-level functions found.")
            else:
                print(f"Check passed: {check_count} function(s) can be split.")
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
    show_diffs: bool = True,
) -> int:
    target_files = _resolve_splitall_files(path_arg, directory_arg, cwd)
    split_count = 0
    results: list[SplitResult | GroupSplitResult] = []

    for file_path in target_files:
        file_results = _split_all_in_file(
            file_path,
            cwd,
            options=options,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
            public_only=public_only,
            related=related,
            show_diffs=show_diffs,
        )
        results.extend(file_results)
        split_count += sum(len(r.function_names) if isinstance(r, GroupSplitResult) else 1 for r in file_results)

    if results and not options.preview:
        descriptor = path_arg if path_arg else f"--dir {directory_arg}"
        history_file = record_split_history(cwd, f"splitall {descriptor}", results)
        print(f"Recorded rollback history: {history_file}")

    return split_count


def _check(
    target_or_path: str | None,
    directory_arg: str | None,
    cwd: Path,
    *,
    options: SplitOptions,
    include_patterns: list[str],
    exclude_patterns: list[str],
    public_only: bool,
    related: bool,
) -> int:
    if directory_arg or _looks_like_file_path(target_or_path):
        return _split_all(
            target_or_path,
            directory_arg,
            cwd,
            options=options,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
            public_only=public_only,
            related=related,
            show_diffs=False,
        )

    if not target_or_path:
        raise PySplitError("check requires a target, a Python file path, or --dir.")

    spec = parse_target(target_or_path)
    resolved = resolve_target(spec, cwd=cwd)
    result = split_function(resolved, options=options)
    _print_split_result(result, show_diffs=False)
    return 1


def _resolve_splitall_files(path_arg: str | None, directory_arg: str | None, cwd: Path) -> list[Path]:
    if bool(path_arg) == bool(directory_arg):
        raise PySplitError("splitall requires either a file path or --dir, but not both.")

    if directory_arg:
        directory = (cwd / directory_arg).resolve()
        if not directory.exists() or not directory.is_dir():
            raise PySplitError(f"Directory not found: {directory}")

        return sorted(file_path for file_path in directory.glob("*.py") if file_path.name != "__init__.py")

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
    show_diffs: bool = True,
) -> list[SplitResult | GroupSplitResult]:
    function_names = _list_top_level_function_names(
        file_path,
        include_patterns=include_patterns,
        exclude_patterns=exclude_patterns,
        public_only=public_only,
    )
    module_path = _module_path_from_file(file_path, cwd)
    results: list[SplitResult | GroupSplitResult] = []

    if not related:
        for function_name in function_names:
            spec = TargetSpec(module_path=module_path, function_name=function_name)
            resolved = resolve_target(spec, cwd=cwd)
            result = split_function(resolved, options=options)
            results.append(result)
            _print_split_result(result, show_diffs=show_diffs)
            print(f"Updated: {result.module_file}")
            print(f"Created: {result.new_module_file}")
            print(f"Inserted import: {result.import_statement}")
        return results

    # --related: group functions by mutual references before splitting.
    source_text = read_python_source(file_path)
    groups = build_function_call_groups(source_text, function_names, file_path)

    for group in groups:
        if len(group) == 1:
            function_name = group[0]
            spec = TargetSpec(module_path=module_path, function_name=function_name)
            resolved = resolve_target(spec, cwd=cwd)
            result = split_function(resolved, options=options)
            results.append(result)
            _print_split_result(result, show_diffs=show_diffs)
            print(f"Updated: {result.module_file}")
            print(f"Created: {result.new_module_file}")
            print(f"Inserted import: {result.import_statement}")
        else:
            primary = group[0]
            spec = TargetSpec(module_path=module_path, function_name=primary)
            resolved = resolve_target(spec, cwd=cwd)
            group_result = split_group(resolved, group, options=options)
            results.append(group_result)
            _print_group_result(group_result, show_diffs=show_diffs)

    return results


def _list_top_level_function_names(
    file_path: Path,
    *,
    include_patterns: list[str],
    exclude_patterns: list[str],
    public_only: bool,
) -> list[str]:
    try:
        tree = ast.parse(read_python_source(file_path))
    except SyntaxError as exc:
        raise PySplitError(f"Could not parse '{file_path}': {exc}") from exc

    function_names = [stmt.name for stmt in tree.body if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))]

    if public_only:
        function_names = [name for name in function_names if not name.startswith("_")]
    if include_patterns:
        function_names = [
            name for name in function_names if any(fnmatch.fnmatchcase(name, pattern) for pattern in include_patterns)
        ]
    if exclude_patterns:
        function_names = [
            name
            for name in function_names
            if not any(fnmatch.fnmatchcase(name, pattern) for pattern in exclude_patterns)
        ]

    return function_names


def _module_path_from_file(file_path: Path, cwd: Path) -> str:
    try:
        return ".".join(path_to_module_parts(file_path, cwd.resolve()))
    except ValueError as exc:
        raise PySplitError(f"File '{file_path}' is not inside the configured cwd '{cwd.resolve()}'.") from exc


def _print_split_result(result: SplitResult, *, show_diffs: bool = True) -> None:
    action = "Would split" if result.preview else "Split"
    verb = _color_text(action, Fore.CYAN if result.preview else Fore.GREEN)
    function_name = _color_text(result.function_name, Fore.MAGENTA)
    print(f"{verb} '{function_name}' successfully.")
    if result.preview:
        _print_change_plan(
            functions=[result.function_name],
            module_file=result.module_file,
            new_module_file=result.new_module_file,
            import_statement=result.import_statement,
            file_changes=result.file_changes,
            output_package=result.output_package,
        )
    if result.preview and show_diffs:
        for diff in result.preview_diffs:
            _print_preview_diff(diff)


def _print_group_result(result: GroupSplitResult, *, show_diffs: bool = True) -> None:
    action = "Would split" if result.preview else "Split"
    verb = _color_text(action, Fore.CYAN if result.preview else Fore.GREEN)
    names_str = _color_text(", ".join(f"'{n}'" for n in result.function_names), Fore.MAGENTA)
    print(f"{verb} related group [{names_str}] -> {result.new_module_file.name}")
    if result.preview:
        _print_change_plan(
            functions=result.function_names,
            module_file=result.module_file,
            new_module_file=result.new_module_file,
            import_statement=result.import_statement,
            file_changes=result.file_changes,
            output_package=result.output_package,
        )
    if result.preview and show_diffs:
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


def _build_split_options(args: argparse.Namespace, *, preview: bool | None = None) -> SplitOptions:
    return SplitOptions(
        preview=args.preview if preview is None else preview,
        output_package=args.output_package,
        validate=args.validate,
        force=args.force,
    )


def _looks_like_file_path(value: str | None) -> bool:
    if value is None:
        return False
    return value.endswith(".py") or "/" in value or "\\" in value


def _print_change_plan(
    *,
    functions: list[str],
    module_file: Path,
    new_module_file: Path,
    import_statement: str,
    file_changes: list[FileChange],
    output_package: str,
) -> None:
    creates = [change.path for change in file_changes if not change.existed_before]
    updates = [change.path for change in file_changes if change.existed_before]
    overwrites = [
        change.path for change in file_changes if change.existed_before and change.path == new_module_file
    ]

    print("Plan:")
    print(f"  Functions: {', '.join(functions)}")
    print(f"  Source: {module_file}")
    print(f"  Output module: {new_module_file}")
    print(f"  Output package: {output_package}")
    print(f"  Import inserted: {import_statement}")
    if creates:
        print(f"  Create: {', '.join(str(path) for path in creates)}")
    if updates:
        print(f"  Update: {', '.join(str(path) for path in updates)}")
    if overwrites:
        print(f"  Overwrite: {', '.join(str(path) for path in overwrites)}")


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
