from __future__ import annotations

import ast
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



def split_function(resolved: ResolvedTarget, *, preview: bool = False) -> SplitResult:
    source_text = resolved.module_file.read_text(encoding="utf-8")
    analysis = _analyze_module(source_text, resolved.spec.function_name, resolved.module_file)

    new_module_file = _build_new_module_file_path(resolved)
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
    import_block = _build_import_block(analysis.imports, analysis.source_text, resolved.package_mode)
    dependency_block = _build_dependency_block(analysis, resolved.spec.function_name)
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
    import_statement = _compute_replacement_import(resolved, new_module_file)
    updated_source = _insert_import(updated_source, import_statement)

    if not preview:
        new_module_file.parent.mkdir(parents=True, exist_ok=True)
        if init_text:
            init_file.write_text(init_text, encoding="utf-8")
        elif init_file.exists():
            init_file.write_text("", encoding="utf-8")
        new_module_file.write_text(new_module_text, encoding="utf-8")
        resolved.module_file.write_text(updated_source, encoding="utf-8")

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

    return SplitResult(
        module_file=resolved.module_file,
        new_module_file=new_module_file,
        function_name=resolved.spec.function_name,
        import_statement=import_statement,
        module_text=updated_source,
        new_module_text=new_module_text,
        init_file=init_file,
        init_text=init_text,
        preview=preview,
        file_changes=file_changes,
    )



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



def _build_new_module_file_path(resolved: ResolvedTarget) -> Path:
    return resolved.module_file.parent / "modules" / f"{resolved.spec.function_name}.py"



def _extract_lines(source_text: str, start_lineno: int, end_lineno: int) -> str:
    lines = source_text.splitlines(keepends=True)
    return "".join(lines[start_lineno - 1 : end_lineno])



def _build_import_block(imports: list[ast.stmt], source_text: str, package_mode: bool) -> str:
    lines = source_text.splitlines(keepends=True)
    blocks: list[str] = []
    for stmt in imports:
        if isinstance(stmt, ast.ImportFrom) and stmt.module == "modules":
            blocks.extend(_rewrite_package_import(stmt, package_mode))
            continue

        if stmt.end_lineno is None:
            continue
        blocks.append("".join(lines[stmt.lineno - 1 : stmt.end_lineno]).rstrip())

    if not blocks:
        return ""
    return "\n".join(blocks) + "\n\n"



def _rewrite_package_import(stmt: ast.ImportFrom, package_mode: bool) -> list[str]:
    prefix = "." if package_mode else ""
    rewritten: list[str] = []

    for alias in sorted(stmt.names, key=lambda item: item.name.casefold()):
        import_name = alias.name
        if alias.asname is None:
            rewritten.append(f"from {prefix}modules.{import_name} import {import_name}")
            continue

        rewritten.append(
            f"from {prefix}modules.{import_name} import {import_name} as {alias.asname}"
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



def _build_dependency_block(analysis: ModuleAnalysis, function_name: str) -> str:
    lines = analysis.source_text.splitlines(keepends=True)
    dependency_names = _collect_dependency_names(analysis.target.node, analysis.definitions)
    dependency_names.discard(function_name)

    blocks: list[tuple[int, str]] = []
    seen_nodes: set[int] = set()
    for name in dependency_names:
        stmt = analysis.definitions.get(name)
        if stmt is None or stmt.end_lineno is None:
            continue

        stmt_id = id(stmt)
        if stmt_id in seen_nodes:
            continue
        seen_nodes.add(stmt_id)
        blocks.append(
            (
                _statement_start_lineno(stmt),
                "".join(lines[_statement_start_lineno(stmt) - 1 : stmt.end_lineno]).rstrip(),
            )
        )

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



def _compute_replacement_import(resolved: ResolvedTarget, new_module_file: Path) -> str:
    del new_module_file
    if resolved.package_mode:
        return f"from .modules import {resolved.spec.function_name}"
    return f"from modules import {resolved.spec.function_name}"



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
    if not isinstance(parsed_import, ast.ImportFrom) or parsed_import.module != "modules":
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