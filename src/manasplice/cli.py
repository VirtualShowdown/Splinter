from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from colorama import Fore, Style, just_fix_windows_console

from .analysis import iter_assigned_names, iter_imported_names, statement_start_lineno
from .config import load_project_config
from .dependencies import collect_dependency_names, collect_required_import_names, render_dependency_blocks
from .exceptions import PySplitError
from .history import record_change_history, record_split_history, rollback_last
from .paradigm import (
    ParadigmOptions,
    ParadigmResult,
    transform_module_to_event_driven,
    transform_module_to_functional,
    transform_module_to_oop,
    transform_module_to_procedural,
)
from .resolver import TargetSpec, parse_target, resolve_target
from .rewrite import (
    build_import_block,
    build_preview_diffs,
    compose_new_module_text,
    insert_import,
    parse_package_exports,
    transform_function_block,
    updated_package_exports,
)
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
        prog="manasplice",
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
    _add_common_operation_flags(splitfunc)
    splitfunc.set_defaults(
        output_package=config.get("output_package", "modules"),
        validate=config.get("validate", False),
        recursive=config.get("recursive", False),
        format=config.get("format", None),
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
        "--auto-group",
        action="store_true",
        help=(
            "Automatically group related functions into shared module files when they "
            "reference each other."
        ),
    )
    splitall.add_argument(
        "--force",
        action="store_true",
        help="Replace existing generated modules if output paths already exist.",
    )
    splitall.add_argument("--recursive", action="store_true", help="Recurse into subdirectories when splitting --dir.")
    splitall.add_argument("--group", help="Comma-separated functions to place in one module.")
    splitall.add_argument("--module", help="Module name to use with --group.")
    _add_common_operation_flags(splitall)
    splitall.set_defaults(
        output_package=config.get("output_package", "modules"),
        validate=config.get("validate", False),
        public_only=config.get("public_only", False),
        include=config.get("include"),
        exclude=config.get("exclude"),
        related=config.get("related", False),
        auto_group=config.get("auto_group", False),
        recursive=config.get("recursive", False),
        format=config.get("format", None),
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
        "--auto-group",
        action="store_true",
        help="Automatically check related functions as grouped output modules.",
    )
    check.add_argument(
        "--force",
        action="store_true",
        help="Allow checks to pass when an output module already exists.",
    )
    check.add_argument("--recursive", action="store_true", help="Recurse into subdirectories when checking --dir.")
    check.add_argument("--project-check", action="store_true", help="Run additional project import safety checks.")
    check.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    check.set_defaults(
        output_package=config.get("output_package", "modules"),
        validate=config.get("validate", False),
        public_only=config.get("public_only", False),
        include=config.get("include"),
        exclude=config.get("exclude"),
        related=config.get("related", False),
        auto_group=config.get("auto_group", False),
        recursive=config.get("recursive", False),
    )

    splitmethod = subparsers.add_parser("splitmethod", help="Extract a class method behind a forwarding wrapper.")
    splitmethod.add_argument("target", help="Target in the form package.module.ClassName.method_name")
    splitmethod.add_argument("--cwd", default=".", help="Project root to resolve from. Defaults to current directory.")
    splitmethod.add_argument("--preview", action="store_true", help="Show the planned changes without writing files.")
    splitmethod.add_argument("--output-package", default=config.get("output_package", "modules"))
    splitmethod.add_argument("--force", action="store_true")
    splitmethod.add_argument("--json", action="store_true")
    splitmethod.add_argument("--format", nargs="?", const="ruff", default=config.get("format", None))
    splitmethod.add_argument("--require-clean-git", action="store_true")
    splitmethod.add_argument("--git-commit", action="store_true")

    paradigm = subparsers.add_parser("paradigm", help="Restructure Python modules toward a programming paradigm.")
    paradigm.add_argument(
        "style",
        help="Target paradigm. Supported: OOP, functional, event-driven, procedural.",
    )
    paradigm.add_argument(
        "path",
        nargs="?",
        help="Python file to restructure. Defaults to the current project directory.",
    )
    paradigm.add_argument("--dir", dest="directory", help="Directory whose Python files should be restructured.")
    paradigm.add_argument("--cwd", default=".", help="Project root to resolve from. Defaults to current directory.")
    paradigm.add_argument("--preview", action="store_true", help="Show planned changes without writing files.")
    paradigm.add_argument("--validate", action="store_true", help="Validate rewritten Python before writing files.")
    paradigm.add_argument("--recursive", action="store_true", help="Recurse into subdirectories.")
    paradigm.add_argument("--class-name", help="Class name to use when restructuring a single file.")
    paradigm.add_argument("--include", help="Comma-separated function name patterns to include, like 'run_*'.")
    paradigm.add_argument("--exclude", help="Comma-separated function name patterns to exclude, like 'main,_*'.")
    paradigm.add_argument("--public-only", action="store_true", help="Only restructure public top-level functions.")
    paradigm.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    paradigm.add_argument("--format", nargs="?", const="ruff", default=config.get("format", None))
    paradigm.add_argument("--require-clean-git", action="store_true")
    paradigm.add_argument("--git-commit", action="store_true")

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

    config_parser = subparsers.add_parser("config", help="Manage ManaSplice project configuration.")
    config_subparsers = config_parser.add_subparsers(dest="config_command", required=True)
    config_init = config_subparsers.add_parser("init", help="Create a [tool.manasplice] pyproject.toml section.")
    config_init.add_argument("--cwd", default=".")
    config_show = config_subparsers.add_parser("show", help="Show the discovered ManaSplice configuration.")
    config_show.add_argument("--cwd", default=".")
    config_show.add_argument("--json", action="store_true")

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
            _require_clean_git(cwd, args.require_clean_git)
            _preflight_git_commit(cwd, args.git_commit)
            spec = parse_target(args.target)
            resolved = resolve_target(spec, cwd=cwd)
            options = _build_split_options(args, cwd=cwd)
            result = split_function(resolved, options=options)
            _format_results([result], _normalize_format_tool(args.format))
            if args.json:
                _print_json({"status": "ok", "results": [_result_to_json(result)]})
            else:
                _print_split_result(result, show_diffs=args.preview)
                print(f"Updated: {result.module_file}")
                print(f"Created: {result.new_module_file}")
                print(f"Inserted import: {result.import_statement}")
            if not args.preview:
                history_file = record_split_history(cwd, f"splitfunc {args.target}", [result])
                if not args.json:
                    print(f"Recorded rollback history: {history_file}")
                _git_commit(cwd, args.git_commit, f"manasplice splitfunc {args.target}", [result])
            return 0
        if args.command == "splitall":
            cwd = Path(args.cwd)
            _require_clean_git(cwd, args.require_clean_git)
            _preflight_git_commit(cwd, args.git_commit)
            options = _build_split_options(args, cwd=cwd)
            results = _split_all(
                args.path,
                args.directory,
                cwd,
                options=options,
                include_patterns=_parse_patterns(args.include),
                exclude_patterns=_parse_patterns(args.exclude),
                public_only=args.public_only,
                auto_group=args.auto_group or args.related,
                recursive=args.recursive,
                output_package=args.output_package,
                manual_group=_parse_manual_group(args.group, args.module),
                emit_output=not args.json,
            )
            _format_results(results, _normalize_format_tool(args.format))
            split_count = _count_results(results)
            if split_count == 0:
                if args.json:
                    _print_json({"status": "ok", "count": 0, "results": []})
                else:
                    print("No top-level functions found.")
            else:
                if args.json:
                    _print_json(
                        {"status": "ok", "count": split_count, "results": [_result_to_json(r) for r in results]}
                    )
                else:
                    action = "Would split" if args.preview else "Split"
                    print(f"{action} {split_count} function(s).")
                if not args.preview:
                    _git_commit(cwd, args.git_commit, "manasplice splitall", results)
            return 0
        if args.command == "check":
            cwd = Path(args.cwd)
            options = _build_split_options(args, preview=True)
            results = _check(
                args.target_or_path,
                args.directory,
                cwd,
                options=options,
                include_patterns=_parse_patterns(args.include),
                exclude_patterns=_parse_patterns(args.exclude),
                public_only=args.public_only,
                auto_group=args.auto_group or args.related,
                recursive=args.recursive,
                project_check=args.project_check,
                emit_output=not args.json,
            )
            check_count = _count_results(results)
            if check_count == 0:
                if args.json:
                    _print_json({"status": "ok", "count": 0, "results": []})
                else:
                    print("Check passed: no top-level functions found.")
            else:
                if args.json:
                    _print_json(
                        {"status": "ok", "count": check_count, "results": [_result_to_json(r) for r in results]}
                    )
                else:
                    print(f"Check passed: {check_count} function(s) can be split.")
            return 0
        if args.command == "splitmethod":
            cwd = Path(args.cwd)
            _require_clean_git(cwd, args.require_clean_git)
            _preflight_git_commit(cwd, args.git_commit)
            result = _split_method(args, cwd)
            _format_results([result], _normalize_format_tool(args.format))
            if args.json:
                _print_json({"status": "ok", "results": [_result_to_json(result)]})
            else:
                _print_split_result(result, show_diffs=args.preview)
                print(f"Updated: {result.module_file}")
                print(f"Created: {result.new_module_file}")
                print(f"Inserted import: {result.import_statement}")
            if not args.preview:
                history_file = record_split_history(cwd, f"splitmethod {args.target}", [result])
                if not args.json:
                    print(f"Recorded rollback history: {history_file}")
                _git_commit(cwd, args.git_commit, f"manasplice splitmethod {args.target}", [result])
            return 0
        if args.command == "paradigm":
            cwd = Path(args.cwd)
            _require_clean_git(cwd, args.require_clean_git)
            _preflight_git_commit(cwd, args.git_commit)
            paradigm_results = _handle_paradigm(args, cwd)
            if not args.preview:
                _format_file_changes(_paradigm_file_changes(paradigm_results), _normalize_format_tool(args.format))
            changed_count = sum(len(result.function_names) for result in paradigm_results)
            if args.json:
                _print_json(
                    {
                        "status": "ok",
                        "style": args.style,
                        "count": changed_count,
                        "results": [_paradigm_result_to_json(result) for result in paradigm_results],
                    }
                )
            else:
                action = "Would restructure" if args.preview else "Restructured"
                print(f"{action} {changed_count} function(s) for {_normalize_paradigm_style(args.style)}.")
            if changed_count and not args.preview:
                changes = _paradigm_file_changes(paradigm_results)
                command = f"manasplice paradigm {_normalize_paradigm_style(args.style)}"
                history_file = record_change_history(cwd, command, changes)
                if not args.json:
                    print(f"Recorded rollback history: {history_file}")
                _git_commit_changes(cwd, args.git_commit, command, changes)
            return 0
        if args.command == "undo":
            undo_count, history_file = rollback_last(Path(args.cwd), args.count)
            print(f"Rolled back {undo_count} operation(s).")
            print(f"Updated rollback history: {history_file}")
            return 0
        if args.command == "config":
            return _handle_config(args)
    except PySplitError as exc:
        if getattr(args, "json", False):
            _print_json({"status": "error", "error": str(exc)})
        else:
            print(f"ManaSplice error: {exc}")
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
    auto_group: bool = False,
    show_diffs: bool = True,
    recursive: bool = False,
    output_package: str = "modules",
    manual_group: tuple[list[str], str] | None = None,
    emit_output: bool = True,
) -> list[SplitResult | GroupSplitResult]:
    target_files = _resolve_splitall_files(
        path_arg,
        directory_arg,
        cwd,
        recursive=recursive,
        output_package=output_package,
    )
    results: list[SplitResult | GroupSplitResult] = []

    for file_path in target_files:
        file_results = _split_all_in_file(
            file_path,
            cwd,
            options=options,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
            public_only=public_only,
            auto_group=auto_group,
            show_diffs=show_diffs,
            manual_group=manual_group,
            emit_output=emit_output,
        )
        results.extend(file_results)

    if results and not options.preview:
        descriptor = path_arg if path_arg else f"--dir {directory_arg}"
        history_file = record_split_history(cwd, f"splitall {descriptor}", results)
        if emit_output:
            print(f"Recorded rollback history: {history_file}")

    return results


def _check(
    target_or_path: str | None,
    directory_arg: str | None,
    cwd: Path,
    *,
    options: SplitOptions,
    include_patterns: list[str],
    exclude_patterns: list[str],
    public_only: bool,
    auto_group: bool,
    recursive: bool,
    project_check: bool,
    emit_output: bool = True,
) -> list[SplitResult | GroupSplitResult]:
    if directory_arg or _looks_like_file_path(target_or_path):
        results = _split_all(
            target_or_path,
            directory_arg,
            cwd,
            options=options,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
            public_only=public_only,
            auto_group=auto_group,
            show_diffs=False,
            recursive=recursive,
            output_package=options.output_package,
            emit_output=emit_output,
        )
        if project_check:
            _run_project_checks(results, cwd, emit_output=emit_output)
        return results

    if not target_or_path:
        raise PySplitError("check requires a target, a Python file path, or --dir.")

    spec = parse_target(target_or_path)
    resolved = resolve_target(spec, cwd=cwd)
    result = split_function(resolved, options=options)
    if project_check:
        _run_project_checks([result], cwd, emit_output=emit_output)
    if emit_output:
        _print_split_result(result, show_diffs=False)
    return [result]


def _handle_paradigm(args: argparse.Namespace, cwd: Path) -> list[ParadigmResult]:
    style = _normalize_paradigm_style(args.style)
    if args.class_name and (args.directory or not args.path or args.recursive):
        raise PySplitError("--class-name can only be used with one explicit Python file.")

    target_files = _resolve_paradigm_files(args.path, args.directory, cwd, recursive=args.recursive)
    results: list[ParadigmResult] = []
    for file_path in target_files:
        options = ParadigmOptions(
            preview=args.preview,
            class_name=args.class_name,
            validate=args.validate,
            include_patterns=_parse_patterns(args.include),
            exclude_patterns=_parse_patterns(args.exclude),
            public_only=args.public_only,
        )
        result = _transform_module_for_style(file_path, style, options)
        results.append(result)
        if not args.json:
            _print_paradigm_result(result, style=style, show_diffs=args.preview)
    return results


def _normalize_paradigm_style(raw_style: str) -> str:
    normalized = raw_style.strip().casefold().replace("_", "-")
    aliases = {
        "oo": "oop",
        "object-oriented": "oop",
        "object-oriented-programming": "oop",
        "fp": "functional",
        "event": "event-driven",
        "eventdriven": "event-driven",
        "events": "event-driven",
        "proc": "procedural",
        "imperative": "procedural",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"oop", "functional", "event-driven", "procedural"}:
        raise PySplitError("Unsupported paradigm. Supported: OOP, functional, event-driven, procedural.")
    return "OOP" if normalized == "oop" else normalized


def _transform_module_for_style(file_path: Path, style: str, options: ParadigmOptions) -> ParadigmResult:
    if style == "OOP":
        return transform_module_to_oop(file_path, options=options)
    if style == "functional":
        return transform_module_to_functional(file_path, options=options)
    if style == "event-driven":
        return transform_module_to_event_driven(file_path, options=options)
    if style == "procedural":
        return transform_module_to_procedural(file_path, options=options)
    raise PySplitError("Unsupported paradigm. Supported: OOP, functional, event-driven, procedural.")


def _resolve_paradigm_files(
    path_arg: str | None,
    directory_arg: str | None,
    cwd: Path,
    *,
    recursive: bool,
) -> list[Path]:
    if path_arg and directory_arg:
        raise PySplitError("paradigm requires either a file path or --dir, but not both.")

    if path_arg:
        file_path = (cwd / path_arg).resolve()
        if not file_path.exists() or not file_path.is_file():
            raise PySplitError(f"File not found: {file_path}")
        if file_path.suffix != ".py":
            raise PySplitError(f"paradigm only supports Python files: {file_path}")
        return [file_path]

    directory = (cwd / directory_arg).resolve() if directory_arg else cwd.resolve()
    if not directory.exists() or not directory.is_dir():
        raise PySplitError(f"Directory not found: {directory}")

    use_recursive = recursive or directory_arg is None
    iterator = directory.rglob("*.py") if use_recursive else directory.glob("*.py")
    return sorted(file_path for file_path in iterator if _is_restructurable_python_file(file_path, directory))


def _is_restructurable_python_file(file_path: Path, root: Path) -> bool:
    if file_path.name == "__init__.py":
        return False
    ignored_parts = {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
    }
    return not ignored_parts.intersection(file_path.relative_to(root).parts)


def _resolve_splitall_files(
    path_arg: str | None,
    directory_arg: str | None,
    cwd: Path,
    *,
    recursive: bool,
    output_package: str,
) -> list[Path]:
    if bool(path_arg) == bool(directory_arg):
        raise PySplitError("splitall requires either a file path or --dir, but not both.")

    if directory_arg:
        directory = (cwd / directory_arg).resolve()
        if not directory.exists() or not directory.is_dir():
            raise PySplitError(f"Directory not found: {directory}")

        iterator = directory.rglob("*.py") if recursive else directory.glob("*.py")
        output_parts = set(output_package.split("."))
        return sorted(
            file_path
            for file_path in iterator
            if file_path.name != "__init__.py" and not output_parts.intersection(file_path.relative_to(directory).parts)
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
    auto_group: bool = False,
    show_diffs: bool = True,
    manual_group: tuple[list[str], str] | None = None,
    emit_output: bool = True,
) -> list[SplitResult | GroupSplitResult]:
    function_names = _list_top_level_function_names(
        file_path,
        include_patterns=include_patterns,
        exclude_patterns=exclude_patterns,
        public_only=public_only,
    )
    module_path = _module_path_from_file(file_path, cwd)
    results: list[SplitResult | GroupSplitResult] = []
    groups = _find_related_function_groups(file_path, function_names)
    if manual_group is not None:
        group_names, module_name = manual_group
        missing_names = sorted(set(group_names) - set(function_names))
        if missing_names:
            raise PySplitError(f"Manual group function(s) not found in {file_path}: {', '.join(missing_names)}.")
        requested = [name for name in group_names if name in function_names]
        if requested:
            primary = requested[0]
            spec = TargetSpec(module_path=module_path, function_name=primary)
            resolved = resolve_target(spec, cwd=cwd)
            group_options = SplitOptions(
                preview=options.preview,
                output_package=options.output_package,
                validate=options.validate,
                force=options.force,
                extracted_name=module_name,
                format_tool=options.format_tool,
            )
            group_result = split_group(resolved, requested, options=group_options)
            results.append(group_result)
            if emit_output:
                _print_group_result(group_result, show_diffs=show_diffs)
            function_names = [name for name in function_names if name not in set(requested)]

    if not auto_group:
        if emit_output:
            _print_auto_group_recommendations(groups)
        for function_name in function_names:
            spec = TargetSpec(module_path=module_path, function_name=function_name)
            resolved = resolve_target(spec, cwd=cwd)
            result = split_function(resolved, options=options)
            results.append(result)
            if emit_output:
                _print_split_result(result, show_diffs=show_diffs)
                print(f"Updated: {result.module_file}")
                print(f"Created: {result.new_module_file}")
                print(f"Inserted import: {result.import_statement}")
        return results

    for group in groups:
        if len(group) == 1:
            function_name = group[0]
            spec = TargetSpec(module_path=module_path, function_name=function_name)
            resolved = resolve_target(spec, cwd=cwd)
            result = split_function(resolved, options=options)
            results.append(result)
            if emit_output:
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
            if emit_output:
                _print_group_result(group_result, show_diffs=show_diffs)

    return results


def _find_related_function_groups(file_path: Path, function_names: list[str]) -> list[list[str]]:
    source_text = read_python_source(file_path)
    return build_function_call_groups(source_text, function_names, file_path)


def _print_auto_group_recommendations(groups: list[list[str]]) -> None:
    related_groups = [group for group in groups if len(group) > 1]
    if not related_groups:
        return

    print("Related functions detected:")
    for group in related_groups:
        print(f"  {', '.join(group)}")
    print("Recommendation: use --auto-group to keep related functions together.")


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

    function_names = [
        stmt.name
        for stmt in tree.body
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and not _is_overload_function(stmt)
    ]

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
    for line in _function_kind_report(result.new_module_text, result.function_name):
        print(line)
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
    for function_name in result.function_names:
        for line in _function_kind_report(result.new_module_text, function_name):
            print(line)
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


def _print_paradigm_result(result: ParadigmResult, *, style: str, show_diffs: bool = True) -> None:
    if not result.function_names:
        if result.skipped:
            print(f"Skipped {result.module_file}: no safe top-level functions to restructure.")
        return

    action = "Would restructure" if result.preview else "Restructured"
    verb = _color_text(action, Fore.CYAN if result.preview else Fore.GREEN)
    names_str = _color_text(", ".join(result.function_names), Fore.MAGENTA)
    print(f"{verb} {result.module_file} for {style} via {result.class_name}: {names_str}")
    for function_name, reason in result.skipped.items():
        print(f"Skipped {function_name}: {reason}.")
    if result.preview and show_diffs:
        for diff in result.preview_diffs:
            _print_preview_diff(diff)


def _paradigm_file_changes(results: list[ParadigmResult]) -> list[FileChange]:
    return [change for result in results for change in result.file_changes]


def _parse_patterns(raw_patterns: str | list | None) -> list[str]:
    if raw_patterns is None:
        return []
    if isinstance(raw_patterns, list):
        return [str(p).strip() for p in raw_patterns if str(p).strip()]
    return [pattern.strip() for pattern in raw_patterns.split(",") if pattern.strip()]


def _add_common_operation_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--output", help="Write the extracted function to this module file path.")
    parser.add_argument("--name", help="Rename the extracted function while preserving the original import name.")
    parser.add_argument("--into", help="Append the extracted function to an existing module file.")
    parser.add_argument("--format", nargs="?", const="ruff", help="Format changed files, optionally with a tool name.")
    parser.add_argument(
        "--require-clean-git",
        action="store_true",
        help="Refuse to run when git has uncommitted changes.",
    )
    parser.add_argument("--git-commit", action="store_true", help="Commit the split after writing files.")
    decorators = parser.add_mutually_exclusive_group()
    decorators.add_argument("--keep-decorators", action="store_true", default=True)
    decorators.add_argument("--strip-decorators", action="store_true")


def _build_split_options(
    args: argparse.Namespace,
    *,
    preview: bool | None = None,
    cwd: Path | None = None,
) -> SplitOptions:
    output_arg = getattr(args, "output", None)
    into_arg = getattr(args, "into", None)
    if output_arg and into_arg:
        raise PySplitError("--output and --into cannot be used together.")
    output_file = None
    append = False
    if output_arg:
        output_file = ((cwd or Path(".")) / output_arg).resolve()
    if into_arg:
        output_file = ((cwd or Path(".")) / into_arg).resolve()
        append = True
    format_value = getattr(args, "format", None)
    if format_value is True:
        format_value = "ruff"
    return SplitOptions(
        preview=args.preview if preview is None else preview,
        output_package=args.output_package,
        validate=args.validate,
        force=args.force,
        output_file=output_file,
        extracted_name=getattr(args, "name", None),
        append=append,
        keep_decorators=not getattr(args, "strip_decorators", False),
        format_tool=format_value,
    )


def _parse_manual_group(group_arg: str | None, module_arg: str | None) -> tuple[list[str], str] | None:
    if group_arg is None and module_arg is None:
        return None
    if not group_arg or not module_arg:
        raise PySplitError("--group and --module must be used together.")
    names = _parse_patterns(group_arg)
    if not names:
        raise PySplitError("--group must include at least one function name.")
    if not module_arg.isidentifier():
        raise PySplitError("--module must be a valid Python module name.")
    return names, module_arg


def _count_results(results: list[SplitResult | GroupSplitResult]) -> int:
    return sum(len(r.function_names) if isinstance(r, GroupSplitResult) else 1 for r in results)


def _result_to_json(result: SplitResult | GroupSplitResult) -> dict[str, object]:
    names = result.function_names if isinstance(result, GroupSplitResult) else [result.function_name]
    return {
        "preview": result.preview,
        "functions": names,
        "function_kinds": {name: _function_kinds(result.new_module_text, name) for name in names},
        "source": str(result.module_file),
        "output_module": str(result.new_module_file),
        "import_statement": result.import_statement,
        "output_package": result.output_package,
        "changes": [
            {
                "path": str(change.path),
                "action": "update" if change.existed_before else "create",
                "changed": change.before_text != change.after_text,
            }
            for change in result.file_changes
        ],
    }


def _paradigm_result_to_json(result: ParadigmResult) -> dict[str, object]:
    return {
        "preview": result.preview,
        "source": str(result.module_file),
        "class_name": result.class_name,
        "functions": result.function_names,
        "skipped": result.skipped,
        "changes": [
            {
                "path": str(change.path),
                "action": "update" if change.existed_before else "create",
                "changed": change.before_text != change.after_text,
            }
            for change in result.file_changes
        ],
    }


def _print_json(data: dict[str, object]) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def _format_results(results: list[SplitResult | GroupSplitResult], format_tool: str | None) -> None:
    if not results or not format_tool or results[0].preview:
        return
    if format_tool != "ruff":
        raise PySplitError(f"Unsupported formatter '{format_tool}'. Only 'ruff' is supported.")

    files = sorted(
        {str(change.path) for result in results for change in result.file_changes if change.path.suffix == ".py"}
    )
    if not files:
        return
    subprocess.run(["ruff", "format", *files], check=False, capture_output=True, text=True)
    subprocess.run(["ruff", "check", "--fix", *files], check=False, capture_output=True, text=True)


def _format_file_changes(file_changes: list[FileChange], format_tool: str | None) -> None:
    if not file_changes or not format_tool:
        return
    if format_tool != "ruff":
        raise PySplitError(f"Unsupported formatter '{format_tool}'. Only 'ruff' is supported.")
    files = sorted({str(change.path) for change in file_changes if change.path.suffix == ".py"})
    if not files:
        return
    subprocess.run(["ruff", "format", *files], check=False, capture_output=True, text=True)
    subprocess.run(["ruff", "check", "--fix", *files], check=False, capture_output=True, text=True)


def _normalize_format_tool(format_tool: object) -> str | None:
    if format_tool is True:
        return "ruff"
    if format_tool in {False, None}:
        return None
    return str(format_tool)


def _require_clean_git(cwd: Path, enabled: bool) -> None:
    if not enabled:
        return
    result = subprocess.run(["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise PySplitError("--require-clean-git was requested, but this is not a git repository.")
    if result.stdout.strip():
        raise PySplitError("Working tree is dirty. Commit or stash changes before running with --require-clean-git.")


def _preflight_git_commit(cwd: Path, enabled: bool) -> None:
    if not enabled:
        return
    status = subprocess.run(["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True, check=False)
    if status.returncode != 0:
        raise PySplitError("--git-commit was requested, but this is not a git repository.")


def _git_commit(cwd: Path, enabled: bool, message: str, results: list[SplitResult | GroupSplitResult]) -> None:
    if not enabled:
        return
    status = subprocess.run(["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True, check=False)
    if status.returncode != 0:
        raise PySplitError("--git-commit was requested, but this is not a git repository.")
    changed_paths = sorted({str(change.path) for result in results for change in result.file_changes})
    if not changed_paths:
        return
    subprocess.run(["git", "add", "--", *changed_paths], cwd=cwd, check=True)
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=cwd, check=False)
    if staged.returncode == 0:
        return
    subprocess.run(["git", "commit", "--no-verify", "-m", message], cwd=cwd, check=True)


def _git_commit_changes(cwd: Path, enabled: bool, message: str, changes: list[FileChange]) -> None:
    if not enabled:
        return
    status = subprocess.run(["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True, check=False)
    if status.returncode != 0:
        raise PySplitError("--git-commit was requested, but this is not a git repository.")
    changed_paths = sorted({str(change.path) for change in changes})
    if not changed_paths:
        return
    subprocess.run(["git", "add", "--", *changed_paths], cwd=cwd, check=True)
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=cwd, check=False)
    if staged.returncode == 0:
        return
    subprocess.run(["git", "commit", "--no-verify", "-m", message], cwd=cwd, check=True)


def _run_project_checks(results: list[SplitResult | GroupSplitResult], cwd: Path, *, emit_output: bool) -> None:
    for result in results:
        output_parent = result.new_module_file.parent
        if output_parent.exists() and not output_parent.is_dir():
            raise PySplitError(f"Generated modules package conflicts with existing path: {output_parent}")
        source_module = _module_path_from_file(result.module_file, cwd)
        if _imports_module(result.new_module_text, source_module) or _imports_module(result.module_text, source_module):
            raise PySplitError(
                f"Potential circular import introduced: generated or rewritten code imports '{source_module}'."
            )
    if emit_output:
        print("Project check passed: generated imports and package path look consistent.")


def _imports_module(source_text: str, module_path: str) -> bool:
    try:
        tree = ast.parse(source_text)
    except SyntaxError:
        return False
    top_level = module_path.split(".", 1)[0]
    for stmt in ast.walk(tree):
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                imported = alias.name
                if imported == module_path or imported == top_level:
                    return True
        elif isinstance(stmt, ast.ImportFrom):
            imported_from = "." * stmt.level + (stmt.module or "")
            if imported_from == module_path:
                return True
    return False


def _handle_config(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd).resolve()
    pyproject = cwd / "pyproject.toml"
    if args.config_command == "init":
        block = (
            "\n[tool.manasplice]\n"
            'output_package = "modules"\n'
            "validate = true\n"
            "public_only = true\n"
            'exclude = ["main", "_*"]\n'
            "recursive = true\n"
            'format = "ruff"\n'
        )
        if pyproject.exists():
            text = pyproject.read_text(encoding="utf-8")
            if "[tool.manasplice]" in text:
                raise PySplitError(f"{pyproject} already contains [tool.manasplice].")
            pyproject.write_text(text.rstrip() + "\n" + block, encoding="utf-8")
        else:
            pyproject.write_text(block.lstrip(), encoding="utf-8")
        print(f"Wrote ManaSplice config: {pyproject}")
        return 0
    if args.config_command == "show":
        config = load_project_config(cwd)
        if args.json:
            _print_json({"config": config})
        else:
            print(json.dumps(config, indent=2, sort_keys=True))
        return 0
    raise PySplitError("Unknown config command.")


def _split_method(args: argparse.Namespace, cwd: Path) -> SplitResult:
    module_path, class_name, method_name = _parse_method_target(args.target)
    module_file = resolve_target(TargetSpec(module_path=module_path, function_name=method_name), cwd=cwd).module_file
    source_text = read_python_source(module_file)
    tree = ast.parse(source_text)
    imports, import_bindings, definitions = _collect_module_bindings(tree)
    class_node = next((stmt for stmt in tree.body if isinstance(stmt, ast.ClassDef) and stmt.name == class_name), None)
    if class_node is None:
        raise PySplitError(f"Class '{class_name}' was not found in '{module_file}'.")

    method_node = next(
        (
            stmt
            for stmt in class_node.body
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == method_name
        ),
        None,
    )
    if method_node is None or method_node.end_lineno is None:
        raise PySplitError(f"Method '{class_name}.{method_name}' was not found in '{module_file}'.")

    is_static = any(_decorator_name(decorator) == "staticmethod" for decorator in method_node.decorator_list)
    is_classmethod = any(_decorator_name(decorator) == "classmethod" for decorator in method_node.decorator_list)
    unsafe_decorators = [
        _decorator_name(decorator)
        for decorator in method_node.decorator_list
        if _decorator_name(decorator) not in {"staticmethod", "classmethod"}
    ]
    if unsafe_decorators:
        raise PySplitError(
            f"Refusing to split decorated method '{class_name}.{method_name}' by default: "
            f"{', '.join(unsafe_decorators)}."
        )
    if not is_static and (not method_node.args.args or method_node.args.args[0].arg not in {"self", "cls"}):
        raise PySplitError("Refusing to split method without an explicit self/cls first parameter.")

    output_dir = module_file.parent.joinpath(*args.output_package.split("."))
    helper_module_name = f"{_camel_to_snake(class_name)}_{method_name}"
    new_module_file = output_dir / f"{helper_module_name}.py"
    if new_module_file.exists() and not args.force:
        raise PySplitError(f"Refusing to overwrite existing generated module '{new_module_file}'. Pass --force.")

    dependency_names = collect_dependency_names(method_node, definitions)
    dependency_names.discard(class_name)
    dependency_names.discard(method_name)
    dependency_nodes = [stmt for name, stmt in definitions.items() if name in dependency_names]
    required_imports = collect_required_import_names([method_node], dependency_nodes, set(import_bindings))
    method_init_file = output_dir / "__init__.py"
    method_exports = parse_package_exports(
        method_init_file.read_text(encoding="utf-8") if method_init_file.exists() else ""
    )
    import_block = build_import_block(
        imports,
        source_text,
        (module_file.parent / "__init__.py").exists(),
        args.output_package,
        required_imports,
        method_exports,
    )
    dependency_block = render_dependency_blocks(definitions, source_text, dependency_names)
    lines = source_text.splitlines(keepends=True)
    method_start = statement_start_lineno(method_node)
    method_block = "".join(lines[method_start - 1 : method_node.end_lineno])
    helper_block = transform_function_block(
        textwrap.dedent(method_block),
        new_name=None,
        keep_decorators=False,
    )
    new_module_text = compose_new_module_text(
        source_path=module_file,
        import_block=import_block,
        dependency_block=dependency_block,
        function_block=helper_block,
    )

    helper_args = _call_arguments(method_node.args)
    wrapper_args = ast.unparse(method_node.args)
    returns = f" -> {ast.unparse(method_node.returns)}" if method_node.returns is not None else ""
    decorator = "    @staticmethod\n" if is_static else "    @classmethod\n" if is_classmethod else ""
    async_prefix = "async " if isinstance(method_node, ast.AsyncFunctionDef) else ""
    await_prefix = "await " if isinstance(method_node, ast.AsyncFunctionDef) else ""
    wrapper = (
        f"{decorator}    {async_prefix}def {method_name}({wrapper_args}){returns}:\n"
        f"        return {await_prefix}{method_name}({helper_args})\n"
    )
    updated_source = "".join(lines[: method_start - 1]) + wrapper + "".join(lines[method_node.end_lineno :])
    import_statement = compute_import = (
        f"from .{args.output_package}.{helper_module_name} import {method_name}"
        if (module_file.parent / "__init__.py").exists()
        else f"from {args.output_package}.{helper_module_name} import {method_name}"
    )
    updated_source = insert_import(updated_source, import_statement)

    init_file = output_dir / "__init__.py"
    existing_init = init_file.read_text(encoding="utf-8") if init_file.exists() else ""
    init_text = updated_package_exports(existing_init, method_name, module_name=helper_module_name)
    file_changes = [
        FileChange(module_file, True, source_text, updated_source),
        FileChange(
            new_module_file,
            new_module_file.exists(),
            new_module_file.read_text(encoding="utf-8") if new_module_file.exists() else "",
            new_module_text,
        ),
        FileChange(init_file, init_file.exists(), existing_init, init_text),
    ]
    if not args.preview:
        output_dir.mkdir(parents=True, exist_ok=True)
        for change in file_changes:
            change.path.write_text(change.after_text, encoding="utf-8")

    return SplitResult(
        module_file=module_file,
        new_module_file=new_module_file,
        function_name=method_name,
        import_statement=compute_import,
        module_text=updated_source,
        new_module_text=new_module_text,
        init_file=init_file,
        init_text=init_text,
        preview=args.preview,
        file_changes=file_changes,
        output_package=args.output_package,
        preview_diffs=build_preview_diffs(file_changes),
    )


def _parse_method_target(target: str) -> tuple[str, str, str]:
    parts = target.split(".")
    if len(parts) < 3:
        raise PySplitError("splitmethod target must look like package.module.ClassName.method_name.")
    return ".".join(parts[:-2]), parts[-2], parts[-1]


def _decorator_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    return ""


def _is_overload_function(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(_decorator_name(decorator) == "overload" for decorator in node.decorator_list)


def _collect_module_bindings(
    tree: ast.Module,
) -> tuple[list[ast.stmt], dict[str, ast.stmt], dict[str, ast.stmt]]:
    imports: list[ast.stmt] = []
    import_bindings: dict[str, ast.stmt] = {}
    definitions: dict[str, ast.stmt] = {}

    for stmt in tree.body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            imports.append(stmt)
            for name in iter_imported_names(stmt):
                import_bindings.setdefault(name, stmt)
            continue

        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) or not _is_overload_function(stmt):
                definitions.setdefault(stmt.name, stmt)
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for name in iter_assigned_names(stmt):
                definitions.setdefault(name, stmt)

    return imports, import_bindings, definitions


def _call_arguments(args: ast.arguments) -> str:
    names: list[str] = []
    names.extend(arg.arg for arg in args.posonlyargs)
    names.extend(arg.arg for arg in args.args)
    if args.vararg is not None:
        names.append(f"*{args.vararg.arg}")
    names.extend(f"{arg.arg}={arg.arg}" for arg in args.kwonlyargs)
    if args.kwarg is not None:
        names.append(f"**{args.kwarg.arg}")
    return ", ".join(names)


def _camel_to_snake(name: str) -> str:
    chars: list[str] = []
    for index, char in enumerate(name):
        if char.isupper() and index:
            chars.append("_")
        chars.append(char.lower())
    return "".join(chars)


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


def _function_kind_report(source_text: str, function_name: str) -> list[str]:
    kinds = _function_kinds(source_text, function_name)
    reports: list[str] = []
    if kinds["async"]:
        reports.append(f"Found async function: {function_name}")
    if kinds["generator"]:
        reports.append(f"Found generator function: {function_name}")
    return reports


def _function_kinds(source_text: str, function_name: str) -> dict[str, bool]:
    try:
        tree = ast.parse(source_text)
    except SyntaxError:
        return {"async": False, "generator": False}
    for stmt in tree.body:
        if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) or stmt.name != function_name:
            continue
        return {
            "async": isinstance(stmt, ast.AsyncFunctionDef),
            "generator": any(isinstance(node, (ast.Yield, ast.YieldFrom)) for node in ast.walk(stmt)),
        }
    return {"async": False, "generator": False}
