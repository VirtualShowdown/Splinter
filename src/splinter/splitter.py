from __future__ import annotations

import ast
import difflib
from dataclasses import dataclass
from pathlib import Path

from .exceptions import FunctionExtractionError
from .resolver import ResolvedTarget


@dataclass(slots=True)
class FileChange:
    path: Path
    existed_before: bool
    before_text: str
    after_text: str


@dataclass(slots=True)
class SplitOptions:
    preview: bool = False
    output_package: str = "modules"
    validate: bool = False


@dataclass(slots=True)
class SplitResult:
    module_file: Path
    new_module_file: Path
    function_name: str
    import_statement: str
    module_text: str
    new_module_text: str
    init_file: Path
    init_text: str
    preview: bool
    file_changes: list[FileChange]
    output_package: str
    preview_diffs: list[str]


@dataclass(slots=True)
class FunctionNodeInfo:
    node: ast.FunctionDef | ast.AsyncFunctionDef
    start_lineno: int
    end_lineno: int


@dataclass(slots=True)
class ModuleAnalysis:
    tree: ast.Module
    imports: list[ast.stmt]
    definitions: dict[str, ast.stmt]
    target: FunctionNodeInfo
    source_text: str


@dataclass(slots=True)
class MultiModuleAnalysis:
    tree: ast.Module
    imports: list[ast.stmt]
    definitions: dict[str, ast.stmt]
    targets: list[FunctionNodeInfo]
    source_text: str


@dataclass(slots=True)
class GroupSplitResult:
    module_file: Path
    new_module_file: Path
    function_names: list[str]
    import_statement: str
    module_text: str
    new_module_text: str
    init_file: Path
    init_text: str
    preview: bool
    file_changes: list[FileChange]
    output_package: str
    preview_diffs: list[str]



def split_function(resolved: ResolvedTarget, *, options: SplitOptions | None = None, preview: bool | None = None) -> SplitResult:
    if options is None:
        options = SplitOptions()
    if preview is not None:
        options = SplitOptions(
            preview=preview,
            output_package=options.output_package,
            validate=options.validate,
        )
    _validate_output_package(options.output_package)

    source_text = resolved.module_file.read_text(encoding="utf-8")
    analysis = _analyze_module(source_text, resolved.spec.function_name, resolved.module_file)

    dependency_names = _collect_dependency_names(analysis.target.node, analysis.definitions)
    dependency_names.discard(resolved.spec.function_name)
    _detect_local_dependency_cycle(
        resolved.spec.function_name,
        dependency_names,
        analysis.definitions,
        resolved.module_file,
    )

    new_module_file = _build_new_module_file_path(resolved, options)
    init_file = new_module_file.parent / "__init__.py"
    new_module_existed_before = new_module_file.exists()
    init_file_existed_before = init_file.exists()
    existing_new_module_text = new_module_file.read_text(encoding="utf-8") if new_module_existed_before else ""
    existing_init_text = init_file.read_text(encoding="utf-8") if init_file_existed_before else ""
    init_text = _updated_package_exports(existing_init_text, resolved.spec.function_name)

    function_block = _extract_lines(
        analysis.source_text,
        analysis.target.start_lineno,
        analysis.target.end_lineno,
    )
    import_block = _build_import_block(
        analysis.imports,
        analysis.source_text,
        resolved.package_mode,
        options.output_package,
    )
    dependency_block = _build_dependency_block(analysis, resolved.spec.function_name, dependency_names)
    new_module_text = _compose_new_module_text(
        source_path=resolved.module_file,
        import_block=import_block,
        dependency_block=dependency_block,
        function_block=function_block,
    )

    updated_source = _remove_function_block(
        analysis.source_text,
        analysis.target.start_lineno,
        analysis.target.end_lineno,
    )
    import_statement = _compute_replacement_import(resolved, options.output_package)
    updated_source = _insert_import(updated_source, import_statement)

    file_changes = [
        FileChange(
            path=resolved.module_file,
            existed_before=True,
            before_text=source_text,
            after_text=updated_source,
        ),
        FileChange(
            path=new_module_file,
            existed_before=new_module_existed_before,
            before_text=existing_new_module_text,
            after_text=new_module_text,
        ),
        FileChange(
            path=init_file,
            existed_before=init_file_existed_before,
            before_text=existing_init_text,
            after_text=init_text,
        ),
    ]

    if options.validate:
        _validate_split_outputs(file_changes)

    if not options.preview:
        new_module_file.parent.mkdir(parents=True, exist_ok=True)
        if init_text:
            init_file.write_text(init_text, encoding="utf-8")
        elif init_file.exists():
            init_file.write_text("", encoding="utf-8")
        new_module_file.write_text(new_module_text, encoding="utf-8")
        resolved.module_file.write_text(updated_source, encoding="utf-8")

    return SplitResult(
        module_file=resolved.module_file,
        new_module_file=new_module_file,
        function_name=resolved.spec.function_name,
        import_statement=import_statement,
        module_text=updated_source,
        new_module_text=new_module_text,
        init_file=init_file,
        init_text=init_text,
        preview=options.preview,
        file_changes=file_changes,
        output_package=options.output_package,
        preview_diffs=_build_preview_diffs(file_changes),
    )



def split_group(
    resolved: ResolvedTarget,
    function_names: list[str],
    *,
    options: SplitOptions | None = None,
) -> GroupSplitResult:
    """Extract a group of related functions into a single shared module file."""
    if options is None:
        options = SplitOptions()
    _validate_output_package(options.output_package)

    source_text = resolved.module_file.read_text(encoding="utf-8")
    analysis = _analyze_module_for_group(source_text, function_names, resolved.module_file)

    # Collect all module-level dependencies across every function in the group,
    # but exclude the group functions themselves (they'll all be in the same file).
    func_set = set(function_names)
    all_dep_names: set[str] = set()
    for target_info in analysis.targets:
        deps = _collect_dependency_names(target_info.node, analysis.definitions)
        all_dep_names.update(deps)
    all_dep_names -= func_set

    # The group output file is named after the first function in source order.
    group_module_name = resolved.spec.function_name
    package_parts = options.output_package.split(".")
    new_module_file = resolved.module_file.parent.joinpath(*package_parts) / f"{group_module_name}.py"
    init_file = new_module_file.parent / "__init__.py"
    new_module_existed_before = new_module_file.exists()
    init_file_existed_before = init_file.exists()
    existing_new_module_text = new_module_file.read_text(encoding="utf-8") if new_module_existed_before else ""
    existing_init_text = init_file.read_text(encoding="utf-8") if init_file_existed_before else ""
    init_text = _updated_package_exports_for_group(existing_init_text, group_module_name, function_names)

    import_block = _build_import_block(
        analysis.imports,
        source_text,
        resolved.package_mode,
        options.output_package,
    )
    dependency_block = _render_dependency_blocks(analysis.definitions, source_text, all_dep_names)

    # Concatenate all function blocks in source order.
    function_blocks = [
        _extract_lines(source_text, t.start_lineno, t.end_lineno).strip()
        for t in analysis.targets
    ]
    function_block = "\n\n".join(function_blocks) + "\n"

    new_module_text = _compose_new_module_text(
        source_path=resolved.module_file,
        import_block=import_block,
        dependency_block=dependency_block,
        function_block=function_block,
    )

    # Remove all group functions from source in a single pass (bottom-to-top to
    # preserve line numbers), then do cleanup once.
    ranges = [(t.start_lineno, t.end_lineno) for t in analysis.targets]
    updated_source = _remove_function_blocks(source_text, ranges)

    # Insert one import name at a time; _insert_import merges them into a single line.
    import_statement = _compute_group_import_statement(resolved, sorted(function_names), options.output_package)
    for func_name in sorted(function_names):
        single = (
            f"from .{options.output_package} import {func_name}"
            if resolved.package_mode
            else f"from {options.output_package} import {func_name}"
        )
        updated_source = _insert_import(updated_source, single)

    file_changes = [
        FileChange(
            path=resolved.module_file,
            existed_before=True,
            before_text=source_text,
            after_text=updated_source,
        ),
        FileChange(
            path=new_module_file,
            existed_before=new_module_existed_before,
            before_text=existing_new_module_text,
            after_text=new_module_text,
        ),
        FileChange(
            path=init_file,
            existed_before=init_file_existed_before,
            before_text=existing_init_text,
            after_text=init_text,
        ),
    ]

    if options.validate:
        _validate_split_outputs(file_changes)

    if not options.preview:
        new_module_file.parent.mkdir(parents=True, exist_ok=True)
        if init_text:
            init_file.write_text(init_text, encoding="utf-8")
        elif init_file.exists():
            init_file.write_text("", encoding="utf-8")
        new_module_file.write_text(new_module_text, encoding="utf-8")
        resolved.module_file.write_text(updated_source, encoding="utf-8")

    return GroupSplitResult(
        module_file=resolved.module_file,
        new_module_file=new_module_file,
        function_names=function_names,
        import_statement=import_statement,
        module_text=updated_source,
        new_module_text=new_module_text,
        init_file=init_file,
        init_text=init_text,
        preview=options.preview,
        file_changes=file_changes,
        output_package=options.output_package,
        preview_diffs=_build_preview_diffs(file_changes),
    )


def build_function_call_groups(
    source_text: str,
    function_names: list[str],
    module_file: Path,
) -> list[list[str]]:
    """Group top-level functions by mutual references into connected components.

    Functions that (directly or transitively) reference each other end up in the
    same group.  Returns a list of groups where each group is a list of function
    names in their original source order.
    """
    if not function_names:
        return []

    try:
        tree = ast.parse(source_text)
    except SyntaxError as exc:
        raise FunctionExtractionError(f"Could not parse '{module_file}': {exc}") from exc

    func_set = set(function_names)
    source_order: list[str] = []
    nodes: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}

    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name in func_set:
            if stmt.name not in nodes:
                nodes[stmt.name] = stmt
                source_order.append(stmt.name)

    # Build an undirected adjacency graph of direct cross-function references.
    adj: dict[str, set[str]] = {name: set() for name in source_order}
    for name, node in nodes.items():
        for ref in _find_module_level_references(node):
            if ref in func_set and ref != name:
                adj[name].add(ref)
                adj[ref].add(name)

    # BFS to find connected components, preserving source order.
    visited: set[str] = set()
    components: list[list[str]] = []

    for start in source_order:
        if start in visited:
            continue
        component: list[str] = []
        queue = [start]
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            component.append(current)
            for neighbor in adj.get(current, set()):
                if neighbor not in visited:
                    queue.append(neighbor)
        component_set = set(component)
        components.append([n for n in source_order if n in component_set])

    return components


def _analyze_module_for_group(
    source_text: str,
    function_names: list[str],
    module_file: Path,
) -> MultiModuleAnalysis:
    try:
        tree = ast.parse(source_text)
    except SyntaxError as exc:
        raise FunctionExtractionError(f"Could not parse '{module_file}': {exc}") from exc

    func_set = set(function_names)
    imports: list[ast.stmt] = []
    definitions: dict[str, ast.stmt] = {}
    found: dict[str, FunctionNodeInfo] = {}

    for stmt in tree.body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            imports.append(stmt)
            continue

        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            definitions.setdefault(stmt.name, stmt)
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for name in _iter_assigned_names(stmt):
                definitions.setdefault(name, stmt)

        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name in func_set:
            if stmt.name in found:
                raise FunctionExtractionError(
                    f"Found duplicate top-level definitions for '{stmt.name}' in '{module_file}'."
                )
            if stmt.end_lineno is None:
                raise FunctionExtractionError(
                    f"Could not determine end line for '{stmt.name}'."
                )
            found[stmt.name] = FunctionNodeInfo(
                node=stmt,
                start_lineno=_statement_start_lineno(stmt),
                end_lineno=stmt.end_lineno,
            )

    missing = func_set - set(found)
    if missing:
        names_str = ", ".join(sorted(missing))
        raise FunctionExtractionError(
            f"Function(s) {names_str!r} not found as top-level definitions in '{module_file}'."
        )

    targets = sorted(found.values(), key=lambda t: t.start_lineno)
    return MultiModuleAnalysis(
        tree=tree,
        imports=imports,
        definitions=definitions,
        targets=targets,
        source_text=source_text,
    )


def _compute_group_import_statement(resolved: ResolvedTarget, function_names: list[str], output_package: str) -> str:
    names_str = ", ".join(sorted(function_names))
    if resolved.package_mode:
        return f"from .{output_package} import {names_str}"
    return f"from {output_package} import {names_str}"


def _analyze_module(source_text: str, function_name: str, module_file: Path) -> ModuleAnalysis:
    try:
        tree = ast.parse(source_text)
    except SyntaxError as exc:
        raise FunctionExtractionError(f"Could not parse '{module_file}': {exc}") from exc

    imports: list[ast.stmt] = []
    definitions: dict[str, ast.stmt] = {}
    target_info: FunctionNodeInfo | None = None

    for stmt in tree.body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            imports.append(stmt)
            continue

        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            definitions.setdefault(stmt.name, stmt)
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for name in _iter_assigned_names(stmt):
                definitions.setdefault(name, stmt)

        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == function_name:
            if target_info is not None:
                raise FunctionExtractionError(
                    f"Found duplicate top-level definitions for function '{function_name}' in '{module_file}'."
                )
            if stmt.end_lineno is None:
                raise FunctionExtractionError(
                    f"Could not determine end line for function '{function_name}'."
                )
            target_info = FunctionNodeInfo(
                node=stmt,
                start_lineno=_statement_start_lineno(stmt),
                end_lineno=stmt.end_lineno,
            )

    if target_info is None:
        raise FunctionExtractionError(
            f"Function '{function_name}' was not found as a top-level definition in '{module_file}'."
        )

    return ModuleAnalysis(
        tree=tree,
        imports=imports,
        definitions=definitions,
        target=target_info,
        source_text=source_text,
    )



def _build_new_module_file_path(resolved: ResolvedTarget, options: SplitOptions) -> Path:
    package_parts = options.output_package.split(".")
    return resolved.module_file.parent.joinpath(*package_parts) / f"{resolved.spec.function_name}.py"



def _extract_lines(source_text: str, start_lineno: int, end_lineno: int) -> str:
    lines = source_text.splitlines(keepends=True)
    return "".join(lines[start_lineno - 1 : end_lineno])



def _build_import_block(
    imports: list[ast.stmt],
    source_text: str,
    package_mode: bool,
    output_package: str,
) -> str:
    lines = source_text.splitlines(keepends=True)
    blocks: list[str] = []
    for stmt in imports:
        if isinstance(stmt, ast.ImportFrom) and stmt.module == output_package:
            blocks.extend(_rewrite_package_import(stmt, package_mode, output_package))
            continue

        if stmt.end_lineno is None:
            continue
        blocks.append("".join(lines[stmt.lineno - 1 : stmt.end_lineno]).rstrip())

    if not blocks:
        return ""
    return "\n".join(blocks) + "\n\n"



def _rewrite_package_import(stmt: ast.ImportFrom, package_mode: bool, output_package: str) -> list[str]:
    rewritten: list[str] = []

    for alias in sorted(stmt.names, key=lambda item: item.name.casefold()):
        import_name = alias.name
        if alias.asname is None:
            if package_mode:
                rewritten.append(f"from .{import_name} import {import_name}")
            else:
                rewritten.append(f"from {output_package}.{import_name} import {import_name}")
            continue

        if package_mode:
            rewritten.append(f"from .{import_name} import {import_name} as {alias.asname}")
        else:
            rewritten.append(
                f"from {output_package}.{import_name} import {import_name} as {alias.asname}"
            )

    return rewritten



def _updated_package_exports(existing: str, function_name: str) -> str:
    export_line = f"from .{function_name} import {function_name}\n"
    lines = existing.splitlines(keepends=True)

    if export_line in lines:
        return existing

    lines.append(export_line)
    lines = sorted(set(lines), key=str.casefold)
    return "".join(lines)


def _updated_package_exports_for_group(existing: str, group_module_name: str, function_names: list[str]) -> str:
    """Update __init__.py to export all names in a group from one submodule."""
    names_str = ", ".join(sorted(function_names))
    export_line = f"from .{group_module_name} import {names_str}\n"

    lines = existing.splitlines(keepends=True)
    # Remove any pre-existing individual exports for these names that would conflict.
    filtered = [
        line for line in lines
        if not any(line.strip() == f"from .{n} import {n}" for n in function_names)
    ]
    if export_line in filtered:
        return "".join(filtered)

    filtered.append(export_line)
    filtered = sorted(set(filtered), key=str.casefold)
    return "".join(filtered)



def _build_dependency_block(
    analysis: ModuleAnalysis,
    function_name: str,
    dependency_names: set[str] | None = None,
) -> str:
    dep_names = set(dependency_names or _collect_dependency_names(analysis.target.node, analysis.definitions))
    dep_names.discard(function_name)
    return _render_dependency_blocks(analysis.definitions, analysis.source_text, dep_names)


def _render_dependency_blocks(
    definitions: dict[str, ast.stmt],
    source_text: str,
    dependency_names: set[str],
) -> str:
    lines = source_text.splitlines(keepends=True)
    blocks: list[tuple[int, str]] = []
    seen_nodes: set[int] = set()

    for name in dependency_names:
        stmt = definitions.get(name)
        if stmt is None or stmt.end_lineno is None:
            continue
        stmt_id = id(stmt)
        if stmt_id in seen_nodes:
            continue
        seen_nodes.add(stmt_id)
        start = _statement_start_lineno(stmt)
        blocks.append((start, "".join(lines[start - 1 : stmt.end_lineno]).rstrip()))

    if not blocks:
        return ""

    blocks.sort(key=lambda item: item[0])
    return "\n\n".join(block for _, block in blocks) + "\n\n"



def _compose_new_module_text(
    source_path: Path,
    import_block: str,
    dependency_block: str,
    function_block: str,
) -> str:
    header = f'"""Auto-generated by Splinter from {source_path.name}."""\n\n'
    function_block = function_block.strip() + "\n"
    return header + import_block + dependency_block + function_block


def _validate_output_package(output_package: str) -> None:
    parts = output_package.split(".")
    if not output_package or any(not part.isidentifier() for part in parts):
        raise FunctionExtractionError(
            f"Invalid output package '{output_package}'. Use a dotted Python package path like 'modules' or 'generated'."
        )


def _validate_split_outputs(file_changes: list[FileChange]) -> None:
    for change in file_changes:
        if not change.after_text:
            continue
        try:
            ast.parse(change.after_text)
        except SyntaxError as exc:
            raise FunctionExtractionError(
                f"Validation failed for '{change.path.name}': {exc.msg} at line {exc.lineno}."
            ) from exc


def _build_preview_diffs(file_changes: list[FileChange]) -> list[str]:
    diffs: list[str] = []
    for change in file_changes:
        before = change.before_text.splitlines()
        after = change.after_text.splitlines()
        diff_lines = list(
            difflib.unified_diff(
                before,
                after,
                fromfile=str(change.path),
                tofile=str(change.path),
                lineterm="",
            )
        )
        if diff_lines:
            diffs.append("\n".join(diff_lines))
    return diffs



def _collect_dependency_names(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    definitions: dict[str, ast.stmt],
) -> set[str]:
    pending = [name for name in _find_module_level_references(node) if name in definitions]
    collected: set[str] = set()

    while pending:
        name = pending.pop()
        if name in collected:
            continue

        collected.add(name)
        stmt = definitions[name]
        for dependency in _find_module_level_references(stmt):
            if dependency in definitions and dependency not in collected:
                pending.append(dependency)

    return collected


def _detect_local_dependency_cycle(
    function_name: str,
    dependency_names: set[str],
    definitions: dict[str, ast.stmt],
    module_file: Path,
) -> None:
    for dependency_name in sorted(dependency_names):
        path = _find_dependency_path_to_target(dependency_name, function_name, definitions)
        if path is None:
            continue

        cycle_path = " -> ".join([function_name, *path])
        raise FunctionExtractionError(
            f"Cannot split '{function_name}' from '{module_file}' because it participates in a local dependency cycle: {cycle_path}."
        )


def _find_dependency_path_to_target(
    start_name: str,
    target_name: str,
    definitions: dict[str, ast.stmt],
) -> list[str] | None:
    stack: list[tuple[str, list[str]]] = [(start_name, [start_name])]
    visited: set[str] = set()

    while stack:
        current_name, path = stack.pop()
        if current_name in visited:
            continue
        visited.add(current_name)

        stmt = definitions.get(current_name)
        if stmt is None:
            continue

        for dependency in sorted(_find_module_level_references(stmt)):
            if dependency == target_name:
                return path + [target_name]
            if dependency in definitions and dependency not in visited:
                stack.append((dependency, path + [dependency]))

    return None



def _find_module_level_references(node: ast.AST) -> set[str]:
    collector = _ModuleLevelReferenceCollector()
    collector.visit(node)
    return collector.references



def _iter_assigned_names(node: ast.AST) -> list[str]:
    names: list[str] = []
    targets: list[ast.AST] = []

    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, ast.AnnAssign):
        targets = [node.target]
    elif isinstance(node, ast.AugAssign):
        targets = [node.target]

    for target in targets:
        for child in ast.walk(target):
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                names.append(child.id)

    return names



def _statement_start_lineno(node: ast.stmt) -> int:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.decorator_list:
        return min(decorator.lineno for decorator in node.decorator_list)
    return node.lineno



def _collect_bound_names_in_function(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> set[str]:
    bound: set[str] = set()

    args = node.args
    for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
        bound.add(arg.arg)
    if args.vararg is not None:
        bound.add(args.vararg.arg)
    if args.kwarg is not None:
        bound.add(args.kwarg.arg)

    collector = _FunctionBoundNameCollector()
    if isinstance(node, ast.Lambda):
        collector.visit(node.body)
    else:
        for stmt in node.body:
            collector.visit(stmt)
    bound.update(collector.names)
    return bound



class _FunctionBoundNameCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.names.add(node.id)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.names.add(node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.names.add(node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.names.add(node.name)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        return None

    def visit_SetComp(self, node: ast.SetComp) -> None:
        return None

    def visit_DictComp(self, node: ast.DictComp) -> None:
        return None

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        return None

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return None



class _ModuleLevelReferenceCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.references: set[str] = set()
        self._scopes: list[set[str]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        self._visit_function_signature(node)
        self._visit_scoped_body(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        self._visit_function_signature(node)
        self._visit_scoped_body(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._visit_function_signature(node)
        self._visit_scoped_expression(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword)

        self._scopes.append({node.name})
        for stmt in node.body:
            self.visit(stmt)
        self._scopes.pop()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load) and not self._is_bound(node.id):
            self.references.add(node.id)

    def _visit_function_signature(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> None:
        args = node.args
        defaults = [*args.defaults, *args.kw_defaults]
        for default in defaults:
            if default is not None:
                self.visit(default)

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
                if arg.annotation is not None:
                    self.visit(arg.annotation)
            if args.vararg is not None and args.vararg.annotation is not None:
                self.visit(args.vararg.annotation)
            if args.kwarg is not None and args.kwarg.annotation is not None:
                self.visit(args.kwarg.annotation)
            if node.returns is not None:
                self.visit(node.returns)

    def _visit_scoped_body(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._scopes.append(_collect_bound_names_in_function(node))
        for stmt in node.body:
            self.visit(stmt)
        self._scopes.pop()

    def _visit_scoped_expression(self, node: ast.Lambda) -> None:
        self._scopes.append(_collect_bound_names_in_function(node))
        self.visit(node.body)
        self._scopes.pop()

    def _is_bound(self, name: str) -> bool:
        return any(name in scope for scope in reversed(self._scopes))



def _remove_function_block(source_text: str, start_lineno: int, end_lineno: int) -> str:
    lines = source_text.splitlines(keepends=True)
    del lines[start_lineno - 1 : end_lineno]

    updated = "".join(lines)
    while "\n\n\n" in updated:
        updated = updated.replace("\n\n\n", "\n\n")
    return updated.lstrip("\n")


def _remove_function_blocks(source_text: str, ranges: list[tuple[int, int]]) -> str:
    """Remove multiple line ranges (1-indexed, inclusive) in a single pass."""
    lines = source_text.splitlines(keepends=True)
    for start, end in sorted(ranges, key=lambda r: r[0], reverse=True):
        del lines[start - 1 : end]

    updated = "".join(lines)
    while "\n\n\n" in updated:
        updated = updated.replace("\n\n\n", "\n\n")
    return updated.lstrip("\n")



def _compute_replacement_import(resolved: ResolvedTarget, output_package: str) -> str:
    if resolved.package_mode:
        return f"from .{output_package} import {resolved.spec.function_name}"
    return f"from {output_package} import {resolved.spec.function_name}"



def _insert_import(source_text: str, import_statement: str) -> str:
    lines = source_text.splitlines(keepends=True)
    tree = ast.parse(source_text) if source_text.strip() else ast.parse("")

    merged_source = _merge_with_existing_package_import(tree, lines, import_statement)
    if merged_source is not None:
        return merged_source

    insert_after = 0
    body = getattr(tree, "body", [])

    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        insert_after = body[0].end_lineno or 0

    for stmt in body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            insert_after = max(insert_after, stmt.end_lineno or 0)

    new_line = import_statement + "\n"

    if insert_after == 0:
        return new_line + ("\n" if source_text.strip() else "") + source_text

    lines.insert(insert_after, new_line)
    updated = "".join(lines)
    while "\n\n\n" in updated:
        updated = updated.replace("\n\n\n", "\n\n")
    return updated



def _merge_with_existing_package_import(
    tree: ast.Module,
    lines: list[str],
    import_statement: str,
) -> str | None:
    parsed_import = ast.parse(import_statement).body[0]
    if not isinstance(parsed_import, ast.ImportFrom) or parsed_import.module is None:
        return None

    body = getattr(tree, "body", [])
    for stmt in body:
        if not isinstance(stmt, ast.ImportFrom):
            continue
        if stmt.end_lineno is None:
            continue
        if stmt.level != parsed_import.level or stmt.module != parsed_import.module:
            continue

        imported_names = [alias.name for alias in stmt.names]
        new_name = parsed_import.names[0].name
        if new_name in imported_names:
            return None

        imported_names.append(new_name)
        replacement = _format_import_from(stmt.level, stmt.module, imported_names)
        start_lineno = _statement_start_lineno(stmt)
        lines[start_lineno - 1 : stmt.end_lineno] = [replacement]

        updated = "".join(lines)
        while "\n\n\n" in updated:
            updated = updated.replace("\n\n\n", "\n\n")
        return updated

    return None



def _format_import_from(level: int, module: str | None, names: list[str]) -> str:
    prefix = "." * level
    module_path = f"{prefix}{module or ''}"
    unique_names = sorted(set(names))
    return f"from {module_path} import {', '.join(unique_names)}\n"