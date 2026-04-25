from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from manasplice.cli import main


def test_splitall_splits_one_file(tmp_path: Path, capsys) -> None:
    source = tmp_path / "main.py"
    source.write_text(
        "import math\n\n"
        "def area(r):\n"
        "    return math.pi * r * r\n\n"
        "def hello(name):\n"
        "    return f'Hello, {name}'\n\n"
        "def orchestrate(r, name):\n"
        "    return area(r), hello(name)\n",
        encoding="utf-8",
    )

    exit_code = main(["splitall", "main.py", "--cwd", str(tmp_path)])

    assert exit_code == 0
    updated = source.read_text(encoding="utf-8")
    assert "from modules import area, hello, orchestrate" in updated

    init_text = (tmp_path / "modules" / "__init__.py").read_text(encoding="utf-8")
    assert "from .area import area" in init_text
    assert "from .hello import hello" in init_text
    assert "from .orchestrate import orchestrate" in init_text

    output = capsys.readouterr().out
    assert "Split 3 function(s)." in output


def test_splitall_handles_utf8_bom_source_files(tmp_path: Path, capsys) -> None:
    source = tmp_path / "main.py"
    source.write_text(
        "def area(r):\n"
        "    return r * r\n",
        encoding="utf-8-sig",
    )

    exit_code = main(["splitall", "main.py", "--cwd", str(tmp_path)])

    assert exit_code == 0
    assert "from modules import area" in source.read_text(encoding="utf-8")
    output = capsys.readouterr().out
    assert "Split 1 function(s)." in output


def test_splitall_splits_python_files_in_directory(tmp_path: Path, capsys) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "alpha.py").write_text(
        "def alpha():\n    return 'a'\n",
        encoding="utf-8",
    )
    (pkg / "beta.py").write_text(
        "def beta():\n    return 'b'\n",
        encoding="utf-8",
    )

    exit_code = main(["splitall", "--dir", "pkg", "--cwd", str(tmp_path)])

    assert exit_code == 0
    assert "from modules import alpha" in (pkg / "alpha.py").read_text(encoding="utf-8")
    assert "from modules import beta" in (pkg / "beta.py").read_text(encoding="utf-8")

    init_text = (pkg / "modules" / "__init__.py").read_text(encoding="utf-8")
    assert "from .alpha import alpha" in init_text
    assert "from .beta import beta" in init_text

    output = capsys.readouterr().out
    assert "Split 2 function(s)." in output


def test_splitall_generated_modules_use_direct_submodule_imports(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text(
        "from functools import wraps\n\n"
        "def log_call(func):\n"
        "    @wraps(func)\n"
        "    def wrapper(*args, **kwargs):\n"
        "        return func(*args, **kwargs)\n"
        "    return wrapper\n\n"
        "def require_positive_numbers(values):\n"
        "    if any(v < 0 for v in values):\n"
        "        raise ValueError('negative values are not allowed')\n\n"
        "@log_call\n"
        "def compute(values):\n"
        "    require_positive_numbers(values)\n"
        "    return sum(values)\n",
        encoding="utf-8",
    )

    exit_code = main(["splitall", "main.py", "--cwd", str(tmp_path)])

    assert exit_code == 0
    compute_module = (tmp_path / "modules" / "compute.py").read_text(encoding="utf-8")
    assert "from modules.log_call import log_call" in compute_module
    assert "from modules.require_positive_numbers import require_positive_numbers" in compute_module
    assert "from modules import" not in compute_module


def test_splitall_auto_group_keeps_related_functions_together(tmp_path: Path, capsys) -> None:
    source = tmp_path / "main.py"
    source.write_text(
        "def parse_value(raw):\n"
        "    return raw.strip()\n\n"
        "def validate_value(value):\n"
        "    if not value:\n"
        "        raise ValueError('missing value')\n"
        "    return value\n\n"
        "def format_value(raw):\n"
        "    value = parse_value(raw)\n"
        "    return validate_value(value).upper()\n\n"
        "def unrelated():\n"
        "    return 42\n",
        encoding="utf-8",
    )

    exit_code = main(["splitall", "main.py", "--cwd", str(tmp_path), "--auto-group"])

    assert exit_code == 0
    updated = source.read_text(encoding="utf-8")
    assert "from modules import format_value, parse_value, unrelated, validate_value" in updated
    assert "def format_value" not in updated
    assert "def unrelated" not in updated

    grouped_module = (tmp_path / "modules" / "parse_value.py").read_text(encoding="utf-8")
    assert "def parse_value(raw):" in grouped_module
    assert "def validate_value(value):" in grouped_module
    assert "def format_value(raw):" in grouped_module
    assert not (tmp_path / "modules" / "format_value.py").exists()
    assert (tmp_path / "modules" / "unrelated.py").exists()

    init_text = (tmp_path / "modules" / "__init__.py").read_text(encoding="utf-8")
    assert "from .parse_value import format_value, parse_value, validate_value" in init_text
    assert "from .unrelated import unrelated" in init_text

    output = capsys.readouterr().out
    assert "Split related group ['parse_value', 'validate_value', 'format_value'] -> parse_value.py" in output
    assert "Split 4 function(s)." in output


def test_check_recommends_auto_group_for_related_functions(tmp_path: Path, capsys) -> None:
    source = tmp_path / "main.py"
    original = (
        "def parse_value(raw):\n"
        "    return raw.strip()\n\n"
        "def format_value(raw):\n"
        "    return parse_value(raw).upper()\n"
    )
    source.write_text(original, encoding="utf-8")

    exit_code = main(["check", "main.py", "--cwd", str(tmp_path)])

    assert exit_code == 0
    assert source.read_text(encoding="utf-8") == original
    assert not (tmp_path / "modules").exists()

    output = capsys.readouterr().out
    assert "Related functions detected:" in output
    assert "parse_value, format_value" in output
    assert "Recommendation: use --auto-group to keep related functions together." in output


def test_splitall_preview_does_not_write_files(tmp_path: Path, capsys) -> None:
    source = tmp_path / "main.py"
    original = "def area(r):\n    return r * r\n\ndef hello(name):\n    return f'Hello, {name}'\n"
    source.write_text(original, encoding="utf-8")

    exit_code = main(["splitall", "main.py", "--cwd", str(tmp_path), "--preview"])

    assert exit_code == 0
    assert source.read_text(encoding="utf-8") == original
    assert not (tmp_path / "modules").exists()

    output = capsys.readouterr().out
    assert "Would split 'area' successfully." in output
    assert "Plan:" in output
    assert "Functions: area" in output
    assert "Output module:" in output
    assert "Create:" in output
    assert "Would split 'hello' successfully." in output
    assert "Would split 2 function(s)." in output
    assert "@@" in output
    assert "+from modules import area" in output


def test_check_splitfunc_reports_plan_without_writing(tmp_path: Path, capsys) -> None:
    source = tmp_path / "main.py"
    original = "def area(r):\n    return r * r\n"
    source.write_text(original, encoding="utf-8")

    exit_code = main(["check", "main.area", "--cwd", str(tmp_path)])

    assert exit_code == 0
    assert source.read_text(encoding="utf-8") == original
    assert not (tmp_path / "modules").exists()
    output = capsys.readouterr().out
    assert "Would split 'area' successfully." in output
    assert "Plan:" in output
    assert "Functions: area" in output
    assert "Check passed: 1 function(s) can be split." in output
    assert "@@" not in output


def test_check_file_reports_multiple_plans_without_writing(tmp_path: Path, capsys) -> None:
    source = tmp_path / "main.py"
    original = "def one():\n    return 1\n\n\ndef two():\n    return 2\n"
    source.write_text(original, encoding="utf-8")

    exit_code = main(["check", "main.py", "--cwd", str(tmp_path)])

    assert exit_code == 0
    assert source.read_text(encoding="utf-8") == original
    assert not (tmp_path / "modules").exists()
    output = capsys.readouterr().out
    assert "Functions: one" in output
    assert "Functions: two" in output
    assert "Check passed: 2 function(s) can be split." in output


def test_check_reports_safety_failures_without_writing(tmp_path: Path, capsys) -> None:
    source = tmp_path / "main.py"
    original = "CACHE = {}\n\n\ndef remember(value):\n    CACHE['value'] = value\n    return value\n"
    source.write_text(original, encoding="utf-8")

    exit_code = main(["check", "main.remember", "--cwd", str(tmp_path)])

    assert exit_code == 1
    assert source.read_text(encoding="utf-8") == original
    assert not (tmp_path / "modules").exists()
    output = capsys.readouterr().out
    assert "mutable module global" in output


def test_project_check_detects_source_module_self_import(tmp_path: Path, capsys) -> None:
    source = tmp_path / "main.py"
    source.write_text("import main\n\n\ndef area(r):\n    return r * r\n", encoding="utf-8")

    exit_code = main(["check", "main.area", "--cwd", str(tmp_path), "--project-check"])

    assert exit_code == 1
    assert "Potential circular import" in capsys.readouterr().out
    assert not (tmp_path / "modules").exists()


def test_git_commit_preflight_does_not_write_outside_git(tmp_path: Path, capsys) -> None:
    del tmp_path
    temp_dir = tempfile.TemporaryDirectory(prefix="manasplice_no_git_")
    project = Path(temp_dir.name)
    source = project / "main.py"
    original = "def area(r):\n    return r * r\n"
    source.write_text(original, encoding="utf-8")

    exit_code = main(["splitfunc", "main.area", "--cwd", str(project), "--git-commit"])

    assert exit_code == 1
    assert "--git-commit was requested" in capsys.readouterr().out
    assert source.read_text(encoding="utf-8") == original
    assert not (project / "modules").exists()
    temp_dir.cleanup()


def test_git_commit_still_commits_inside_git(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "qa@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "QA"], cwd=tmp_path, check=True)
    source = tmp_path / "main.py"
    source.write_text("def area(r):\n    return r * r\n", encoding="utf-8")
    subprocess.run(["git", "add", "main.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)

    exit_code = main(["splitfunc", "main.area", "--cwd", str(tmp_path), "--git-commit"])

    assert exit_code == 0
    log = subprocess.run(["git", "log", "--oneline", "-2"], cwd=tmp_path, check=True, capture_output=True, text=True)
    assert "manasplice splitfunc main.area" in log.stdout


def test_git_commit_does_not_run_repository_hooks(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "qa@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "QA"], cwd=tmp_path, check=True)
    source = tmp_path / "main.py"
    source.write_text("def area(r):\n    return r * r\n", encoding="utf-8")
    subprocess.run(["git", "add", "main.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)
    marker = tmp_path / "hook_marker.txt"
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\necho hook-ran > {marker.as_posix()!r}\n", encoding="utf-8")
    hook.chmod(0o755)

    exit_code = main(["splitfunc", "main.area", "--cwd", str(tmp_path), "--git-commit"])

    assert exit_code == 0
    assert not marker.exists()


def test_undo_rejects_history_path_traversal_write(tmp_path: Path, capsys) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("SAFE", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    history = [
        {
            "command": "attacker",
            "changes": [
                {
                    "path": "../victim.txt",
                    "existed_before": True,
                    "before_text": "PWNED",
                    "after_text": "SAFE",
                }
            ],
        }
    ]
    (project / ".manasplice_history.json").write_text(json.dumps(history), encoding="utf-8")

    exit_code = main(["undo", "--cwd", str(project)])

    assert exit_code == 1
    assert "outside the project root" in capsys.readouterr().out
    assert victim.read_text(encoding="utf-8") == "SAFE"


def test_undo_rejects_history_path_traversal_delete(tmp_path: Path, capsys) -> None:
    victim = tmp_path / "delete_me.txt"
    victim.write_text("SAFE", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    history = [
        {
            "command": "attacker",
            "changes": [
                {
                    "path": "../delete_me.txt",
                    "existed_before": False,
                    "before_text": "",
                    "after_text": "SAFE",
                }
            ],
        }
    ]
    (project / ".manasplice_history.json").write_text(json.dumps(history), encoding="utf-8")

    exit_code = main(["undo", "--cwd", str(project)])

    assert exit_code == 1
    assert "outside the project root" in capsys.readouterr().out
    assert victim.exists()


def test_splitall_supports_include_exclude_and_public_only_filters(tmp_path: Path, capsys) -> None:
    source = tmp_path / "main.py"
    source.write_text(
        "def public_alpha():\n"
        "    return 'a'\n\n"
        "def public_beta():\n"
        "    return 'b'\n\n"
        "def main():\n"
        "    return public_alpha(), public_beta()\n\n"
        "def _helper():\n"
        "    return 'hidden'\n",
        encoding="utf-8",
    )

    exit_code = main(
        [
            "splitall",
            "main.py",
            "--cwd",
            str(tmp_path),
            "--public-only",
            "--include",
            "public_*",
            "--exclude",
            "*beta",
        ]
    )

    assert exit_code == 0
    updated = source.read_text(encoding="utf-8")
    assert "from modules import public_alpha" in updated
    assert "public_beta" in updated
    assert "def _helper" in updated
    assert not (tmp_path / "modules" / "public_beta.py").exists()
    assert (tmp_path / "modules" / "public_alpha.py").exists()

    output = capsys.readouterr().out
    assert "Split 1 function(s)." in output


def test_splitfunc_supports_custom_output_package(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text(
        "def area(r):\n    return r * r\n",
        encoding="utf-8",
    )

    exit_code = main(["splitfunc", "main.area", "--cwd", str(tmp_path), "--output-package", "generated"])

    assert exit_code == 0
    assert "from generated import area" in source.read_text(encoding="utf-8")
    assert (tmp_path / "generated" / "area.py").exists()
    assert (tmp_path / "generated" / "__init__.py").exists()


def test_splitfunc_supports_custom_output_and_name(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text("def area(r):\n    return r * r\n", encoding="utf-8")

    exit_code = main(
        [
            "splitfunc",
            "main.area",
            "--cwd",
            str(tmp_path),
            "--output",
            "modules/geometry.py",
            "--name",
            "circle_area",
        ]
    )

    assert exit_code == 0
    assert "from modules.geometry import circle_area as area" in source.read_text(encoding="utf-8")
    assert "def circle_area(r):" in (tmp_path / "modules" / "geometry.py").read_text(encoding="utf-8")
    assert "from .geometry import circle_area" in (tmp_path / "modules" / "__init__.py").read_text(encoding="utf-8")


def test_splitfunc_into_appends_existing_module(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text("def area(r):\n    return r * r\n", encoding="utf-8")
    modules = tmp_path / "modules"
    modules.mkdir()
    (modules / "geometry.py").write_text("def diameter(r):\n    return 2 * r\n", encoding="utf-8")

    exit_code = main(["splitfunc", "main.area", "--cwd", str(tmp_path), "--into", "modules/geometry.py"])

    assert exit_code == 0
    geometry = (modules / "geometry.py").read_text(encoding="utf-8")
    assert "def diameter(r):" in geometry
    assert "def area(r):" in geometry
    assert "from modules.geometry import area" in source.read_text(encoding="utf-8")


def test_splitall_directory_recursive(tmp_path: Path) -> None:
    nested = tmp_path / "pkg" / "nested"
    nested.mkdir(parents=True)
    (nested / "leaf.py").write_text("def leaf():\n    return 1\n", encoding="utf-8")

    exit_code = main(["splitall", "--dir", "pkg", "--cwd", str(tmp_path), "--recursive"])

    assert exit_code == 0
    assert "from modules import leaf" in (nested / "leaf.py").read_text(encoding="utf-8")


def test_splitall_recursive_skips_generated_modules(tmp_path: Path) -> None:
    source = tmp_path / "pkg" / "feature.py"
    generated = tmp_path / "pkg" / "modules" / "already.py"
    source.parent.mkdir(parents=True)
    generated.parent.mkdir(parents=True)
    source.write_text("def feature():\n    return 'feature'\n", encoding="utf-8")
    generated.write_text("def generated_helper():\n    return 'generated'\n", encoding="utf-8")

    exit_code = main(["splitall", "--dir", "pkg", "--cwd", str(tmp_path), "--recursive"])

    assert exit_code == 0
    assert (tmp_path / "pkg" / "modules" / "feature.py").exists()
    assert not (tmp_path / "pkg" / "modules" / "modules" / "generated_helper.py").exists()


def test_splitall_manual_group(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text(
        "def area(r):\n    return r * r\n\n"
        "def diameter(r):\n    return 2 * r\n\n"
        "def slugify(value):\n    return value.lower()\n",
        encoding="utf-8",
    )

    exit_code = main(
        [
            "splitall",
            "main.py",
            "--cwd",
            str(tmp_path),
            "--group",
            "area,diameter",
            "--module",
            "geometry",
        ]
    )

    assert exit_code == 0
    geometry = (tmp_path / "modules" / "geometry.py").read_text(encoding="utf-8")
    assert "def area(r):" in geometry
    assert "def diameter(r):" in geometry
    assert (tmp_path / "modules" / "slugify.py").exists()


def test_splitall_manual_group_keeps_later_package_imports_resolvable(tmp_path: Path) -> None:
    package = tmp_path / "app"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    source = package / "service.py"
    source.write_text(
        "def area(r):\n"
        "    return r * r\n\n"
        "def diameter(r):\n"
        "    return 2 * r\n\n"
        "def use(r):\n"
        "    return area(r) + diameter(r)\n",
        encoding="utf-8",
    )

    exit_code = main(
        [
            "splitall",
            "app/service.py",
            "--cwd",
            str(tmp_path),
            "--group",
            "area,diameter",
            "--module",
            "geometry",
        ]
    )

    assert exit_code == 0
    generated_use = (package / "modules" / "use.py").read_text(encoding="utf-8")
    assert "from .geometry import area" in generated_use
    assert "from .geometry import diameter" in generated_use

    import sys

    sys.path.insert(0, str(tmp_path))
    try:
        import app.service as service

        assert service.area(2) == 4
        assert service.diameter(2) == 4
        assert service.use(2) == 8
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("app.service", None)
        sys.modules.pop("app", None)


def test_splitall_manual_group_rejects_missing_function(tmp_path: Path, capsys) -> None:
    source = tmp_path / "main.py"
    source.write_text("def area(r):\n    return r * r\n", encoding="utf-8")

    exit_code = main(["splitall", "main.py", "--cwd", str(tmp_path), "--group", "missing", "--module", "geometry"])

    assert exit_code == 1
    assert "Manual group function(s) not found" in capsys.readouterr().out
    assert "def area" in source.read_text(encoding="utf-8")


def test_preview_json_is_machine_readable(tmp_path: Path, capsys) -> None:
    source = tmp_path / "main.py"
    source.write_text("def area(r):\n    return r * r\n", encoding="utf-8")

    exit_code = main(["splitall", "main.py", "--cwd", str(tmp_path), "--preview", "--json"])

    assert exit_code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "ok"
    assert data["count"] == 1
    assert data["results"][0]["functions"] == ["area"]


def test_check_json_reports_function_kinds(tmp_path: Path, capsys) -> None:
    source = tmp_path / "main.py"
    source.write_text(
        "async def fetch_user():\n    return 1\n\n"
        "def stream_rows():\n    yield 1\n",
        encoding="utf-8",
    )

    exit_code = main(["check", "main.py", "--cwd", str(tmp_path), "--json"])

    assert exit_code == 0
    data = json.loads(capsys.readouterr().out)
    kinds = {result["functions"][0]: result["function_kinds"] for result in data["results"]}
    assert kinds["fetch_user"]["fetch_user"]["async"] is True
    assert kinds["stream_rows"]["stream_rows"]["generator"] is True


def test_check_reports_async_and_generator_functions(tmp_path: Path, capsys) -> None:
    source = tmp_path / "main.py"
    source.write_text(
        "async def fetch_user():\n    return 1\n\n"
        "def stream_rows():\n    yield 1\n",
        encoding="utf-8",
    )

    exit_code = main(["check", "main.py", "--cwd", str(tmp_path)])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Found async function: fetch_user" in output
    assert "Found generator function: stream_rows" in output


def test_splitfunc_moves_overloads_with_implementation(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text(
        "from typing import overload\n\n"
        "@overload\n"
        "def parse(value: str) -> int: ...\n\n"
        "@overload\n"
        "def parse(value: int) -> int: ...\n\n"
        "def parse(value):\n"
        "    return int(value)\n",
        encoding="utf-8",
    )

    exit_code = main(["splitfunc", "main.parse", "--cwd", str(tmp_path)])

    assert exit_code == 0
    updated = source.read_text(encoding="utf-8")
    assert "@overload" not in updated
    assert "from modules import parse" in updated
    helper = (tmp_path / "modules" / "parse.py").read_text(encoding="utf-8")
    assert "from typing import overload" in helper
    assert helper.count("@overload") == 2


def test_strip_decorators_removes_extracted_decorators(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text(
        "def deco(func):\n    return func\n\n"
        "@deco\n"
        "def route_handler():\n    return 1\n",
        encoding="utf-8",
    )

    exit_code = main(["splitfunc", "main.route_handler", "--cwd", str(tmp_path), "--strip-decorators"])

    assert exit_code == 0
    extracted = (tmp_path / "modules" / "route_handler.py").read_text(encoding="utf-8")
    assert "@deco" not in extracted


def test_splitmethod_creates_forwarding_wrapper(tmp_path: Path) -> None:
    source = tmp_path / "service.py"
    source.write_text(
        "class UserService:\n"
        "    def normalize_name(self, name):\n"
        "        return name.strip().title()\n",
        encoding="utf-8",
    )

    exit_code = main(["splitmethod", "service.UserService.normalize_name", "--cwd", str(tmp_path)])

    assert exit_code == 0
    updated = source.read_text(encoding="utf-8")
    assert "from modules.user_service_normalize_name import normalize_name" in updated
    assert "return normalize_name(self, name)" in updated
    assert (tmp_path / "modules" / "user_service_normalize_name.py").exists()


def test_splitmethod_copies_imports_and_constants(tmp_path: Path) -> None:
    source = tmp_path / "service.py"
    source.write_text(
        "import math\n\n"
        "PREFIX = 'area:'\n\n"
        "class Calculator:\n"
        "    def label_area(self, radius):\n"
        "        return PREFIX + str(round(math.pi * radius * radius, 2))\n",
        encoding="utf-8",
    )

    exit_code = main(["splitmethod", "service.Calculator.label_area", "--cwd", str(tmp_path)])

    assert exit_code == 0
    helper = (tmp_path / "modules" / "calculator_label_area.py").read_text(encoding="utf-8")
    assert "import math" in helper
    assert "PREFIX = 'area:'" in helper

    import sys

    sys.path.insert(0, str(tmp_path))
    try:
        import service

        assert service.Calculator().label_area(2) == "area:12.57"
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("service", None)


def test_splitmethod_supports_multiline_signature(tmp_path: Path) -> None:
    source = tmp_path / "service.py"
    source.write_text(
        "class UserService:\n"
        "    def normalize_name(\n"
        "        self,\n"
        "        name,\n"
        "    ):\n"
        "        return name.strip().title()\n",
        encoding="utf-8",
    )

    exit_code = main(["splitmethod", "service.UserService.normalize_name", "--cwd", str(tmp_path)])

    assert exit_code == 0
    assert "return normalize_name(self, name)" in source.read_text(encoding="utf-8")


def test_splitmethod_rejects_custom_decorators(tmp_path: Path, capsys) -> None:
    source = tmp_path / "service.py"
    source.write_text(
        "def traced(func):\n"
        "    return func\n\n"
        "class UserService:\n"
        "    @traced\n"
        "    def normalize_name(self, name):\n"
        "        return name.strip().title()\n",
        encoding="utf-8",
    )

    exit_code = main(["splitmethod", "service.UserService.normalize_name", "--cwd", str(tmp_path)])

    assert exit_code == 1
    assert "Refusing to split decorated method" in capsys.readouterr().out


def test_paradigm_oop_restructures_top_level_functions_with_wrappers(tmp_path: Path, capsys) -> None:
    source = tmp_path / "main.py"
    source.write_text(
        "VALUE = 3\n\n"
        "def helper(x):\n"
        "    return x + VALUE\n\n"
        "def area(r):\n"
        "    return helper(r * r)\n\n"
        "async def fetch_user(user_id: int) -> int:\n"
        "    return area(user_id)\n\n"
        "def stream(limit):\n"
        "    for index in range(limit):\n"
        "        yield area(index)\n",
        encoding="utf-8",
    )

    exit_code = main(["paradigm", "OOP", "main.py", "--cwd", str(tmp_path), "--validate"])

    assert exit_code == 0
    updated = source.read_text(encoding="utf-8")
    assert "class MainOperations:" in updated
    assert "    @staticmethod\n    def helper(x):" in updated
    assert "def area(r):\n    return MainOperations.area(r)" in updated
    assert "async def fetch_user(user_id: int) -> int:" in updated
    assert "return await MainOperations.fetch_user(user_id)" in updated
    assert "def stream(limit):\n    return MainOperations.stream(limit)" in updated
    assert "Restructured 4 function(s) for OOP." in capsys.readouterr().out

    sys.path.insert(0, str(tmp_path))
    try:
        import main as transformed_main

        assert transformed_main.area(2) == 7
        assert transformed_main.MainOperations.area(2) == 7
        assert list(transformed_main.stream(3)) == [3, 4, 7]
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("main", None)


def test_paradigm_oop_preview_does_not_write(tmp_path: Path, capsys) -> None:
    source = tmp_path / "main.py"
    original = "def area(r):\n    return r * r\n"
    source.write_text(original, encoding="utf-8")

    exit_code = main(["paradigm", "OOP", "main.py", "--cwd", str(tmp_path), "--preview"])

    assert exit_code == 0
    assert source.read_text(encoding="utf-8") == original
    output = capsys.readouterr().out
    assert "Would restructure" in output
    assert "+class MainOperations:" in output


def test_paradigm_oop_defaults_to_recursive_project_directory(tmp_path: Path) -> None:
    package = tmp_path / "app"
    package.mkdir()
    (package / "service.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    hidden = tmp_path / ".venv"
    hidden.mkdir()
    (hidden / "ignored.py").write_text("def ignored():\n    return 1\n", encoding="utf-8")

    exit_code = main(["paradigm", "OOP", "--cwd", str(tmp_path)])

    assert exit_code == 0
    assert "class ServiceOperations:" in (package / "service.py").read_text(encoding="utf-8")
    assert "class IgnoredOperations:" not in (hidden / "ignored.py").read_text(encoding="utf-8")


def test_paradigm_oop_skips_decorated_and_overloaded_functions(tmp_path: Path, capsys) -> None:
    source = tmp_path / "main.py"
    original = (
        "from typing import overload\n\n"
        "from framework import deco\n\n"
        "@deco\n"
        "def route():\n"
        "    return 1\n\n"
        "@overload\n"
        "def parse(value: str) -> int: ...\n\n"
        "@overload\n"
        "def parse(value: int) -> int: ...\n\n"
        "def parse(value):\n"
        "    return int(value)\n"
    )
    source.write_text(original, encoding="utf-8")

    exit_code = main(["paradigm", "OOP", "main.py", "--cwd", str(tmp_path), "--validate"])

    assert exit_code == 0
    assert source.read_text(encoding="utf-8") == original
    output = capsys.readouterr().out
    assert "Skipped" in output
    assert "Restructured 0 function(s) for OOP." in output


def test_paradigm_oop_can_be_undone(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    original = "def area(r):\n    return r * r\n"
    source.write_text(original, encoding="utf-8")

    split_exit = main(["paradigm", "OOP", "main.py", "--cwd", str(tmp_path)])
    undo_exit = main(["undo", "--cwd", str(tmp_path)])

    assert split_exit == 0
    assert undo_exit == 0
    assert source.read_text(encoding="utf-8") == original


def test_paradigm_oop_rejects_existing_generated_class_without_writing(tmp_path: Path, capsys) -> None:
    source = tmp_path / "main.py"
    original = "class MainOperations:\n    pass\n\n\ndef run():\n    return 1\n"
    source.write_text(original, encoding="utf-8")

    exit_code = main(["paradigm", "OOP", "main.py", "--cwd", str(tmp_path)])

    assert exit_code == 1
    assert source.read_text(encoding="utf-8") == original
    assert "already exists" in capsys.readouterr().out


def test_paradigm_oop_places_generated_class_after_later_annotation_types(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text(
        "class Node:\n"
        "    pass\n\n"
        "def make_node() -> Node:\n"
        "    return Node()\n",
        encoding="utf-8",
    )

    exit_code = main(["paradigm", "OOP", "main.py", "--cwd", str(tmp_path), "--validate"])

    assert exit_code == 0
    updated = source.read_text(encoding="utf-8")
    assert updated.index("class Node:") < updated.index("class MainOperations:")

    sys.path.insert(0, str(tmp_path))
    try:
        import main as transformed_main

        assert isinstance(transformed_main.make_node(), transformed_main.Node)
        assert isinstance(transformed_main.MainOperations.make_node(), transformed_main.Node)
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("main", None)


def test_paradigm_oop_skips_functions_used_during_class_initialization(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    original = (
        "def build_value(value):\n"
        "    return value * 2\n\n"
        "class UsesHelper:\n"
        "    value = build_value(3)\n\n"
        "def later(value):\n"
        "    return value + 1\n"
    )
    source.write_text(original, encoding="utf-8")

    exit_code = main(["paradigm", "OOP", "main.py", "--cwd", str(tmp_path), "--validate"])

    assert exit_code == 0
    updated = source.read_text(encoding="utf-8")
    assert "def build_value(value):\n    return value * 2" in updated
    assert "    @staticmethod\n    def later(value):" in updated

    sys.path.insert(0, str(tmp_path))
    try:
        import main as transformed_main

        assert transformed_main.UsesHelper.value == 6
        assert transformed_main.later(2) == 3
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("main", None)


def test_paradigm_oop_skips_functions_with_multiline_string_literals(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    original = (
        "def render_code():\n"
        "    code = '''\\\n"
        "def example():\n"
        "    return 1'''\n"
        "    return code\n\n"
        "def plain(value):\n"
        "    return value\n"
    )
    source.write_text(original, encoding="utf-8")

    exit_code = main(["paradigm", "OOP", "main.py", "--cwd", str(tmp_path), "--validate"])

    assert exit_code == 0
    updated = source.read_text(encoding="utf-8")
    assert "def render_code():\n    code = '''\\\ndef example():" in updated
    assert "    @staticmethod\n    def plain(value):" in updated

    sys.path.insert(0, str(tmp_path))
    try:
        import main as transformed_main

        assert transformed_main.render_code() == "def example():\n    return 1"
        assert transformed_main.plain(3) == 3
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("main", None)


def test_paradigm_functional_adds_functional_facade(tmp_path: Path, capsys) -> None:
    source = tmp_path / "main.py"
    source.write_text(
        "def double(value):\n"
        "    return value * 2\n\n"
        "def increment(value):\n"
        "    return value + 1\n",
        encoding="utf-8",
    )

    exit_code = main(["paradigm", "functional", "main.py", "--cwd", str(tmp_path), "--validate"])

    assert exit_code == 0
    updated = source.read_text(encoding="utf-8")
    assert "FUNCTIONAL_API = {" in updated
    assert '"double": double' in updated
    assert "def pipe(value, *functions):" in updated
    assert "Restructured 2 function(s) for functional." in capsys.readouterr().out

    sys.path.insert(0, str(tmp_path))
    try:
        import main as transformed_main

        assert transformed_main.FUNCTIONAL_API["double"](3) == 6
        assert transformed_main.pipe(3, transformed_main.double, transformed_main.increment) == 7
        composed = transformed_main.compose_functions(transformed_main.increment, transformed_main.double)
        assert composed(3) == 7
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("main", None)


def test_paradigm_event_driven_adds_dispatch_facade(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text(
        "def user_created(name):\n"
        "    return f'created:{name}'\n\n"
        "def user_deleted(name):\n"
        "    return f'deleted:{name}'\n",
        encoding="utf-8",
    )

    exit_code = main(["paradigm", "events", "main.py", "--cwd", str(tmp_path), "--validate"])

    assert exit_code == 0
    updated = source.read_text(encoding="utf-8")
    assert "EVENT_HANDLERS = {" in updated
    assert '"user_created": user_created' in updated
    assert "def dispatch_event(event_name, *args, **kwargs):" in updated

    sys.path.insert(0, str(tmp_path))
    try:
        import main as transformed_main

        assert transformed_main.dispatch_event("user_created", "Ada") == "created:Ada"
        assert transformed_main.EVENT_HANDLERS["user_deleted"]("Ada") == "deleted:Ada"
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("main", None)


def test_paradigm_procedural_flattens_generated_oop_class(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text("def area(r):\n    return r * r\n", encoding="utf-8")

    oop_exit = main(["paradigm", "OOP", "main.py", "--cwd", str(tmp_path), "--validate"])
    procedural_exit = main(["paradigm", "imperative", "main.py", "--cwd", str(tmp_path), "--validate"])

    assert oop_exit == 0
    assert procedural_exit == 0
    updated = source.read_text(encoding="utf-8")
    assert updated.index("def area(r):") < updated.index("class MainOperations:")
    assert "return MainOperations.area(r)" not in updated
    assert "area = staticmethod(area)" in updated

    sys.path.insert(0, str(tmp_path))
    try:
        import main as transformed_main

        assert transformed_main.area(3) == 9
        assert transformed_main.MainOperations.area(3) == 9
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("main", None)


def test_paradigm_facades_refuse_name_collisions_without_writing(tmp_path: Path, capsys) -> None:
    source = tmp_path / "main.py"
    original = "FUNCTIONAL_API = {}\n\n\ndef area(r):\n    return r * r\n"
    source.write_text(original, encoding="utf-8")

    exit_code = main(["paradigm", "fp", "main.py", "--cwd", str(tmp_path)])

    assert exit_code == 1
    assert source.read_text(encoding="utf-8") == original
    assert "name(s) already exist" in capsys.readouterr().out


def test_config_init_and_show(tmp_path: Path, capsys) -> None:
    init_exit = main(["config", "init", "--cwd", str(tmp_path)])
    show_exit = main(["config", "show", "--cwd", str(tmp_path), "--json"])

    assert init_exit == 0
    assert show_exit == 0
    assert "[tool.manasplice]" in (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    output_lines = capsys.readouterr().out.strip().splitlines()
    data = json.loads("\n".join(output_lines[1:]))
    assert data["config"]["output_package"] == "modules"


def test_splitfunc_validate_rejects_invalid_generated_output(tmp_path: Path, monkeypatch, capsys) -> None:
    source = tmp_path / "main.py"
    original = "def area(r):\n    return r * r\n"
    source.write_text(original, encoding="utf-8")

    def broken_insert_import(source_text: str, import_statement: str) -> str:
        del import_statement
        return source_text + "\nthis is not valid python(\n"

    monkeypatch.setattr("manasplice.splitter._insert_import", broken_insert_import)

    exit_code = main(["splitfunc", "main.area", "--cwd", str(tmp_path), "--validate"])

    assert exit_code == 1
    assert source.read_text(encoding="utf-8") == original
    assert not (tmp_path / "modules").exists()

    output = capsys.readouterr().out
    assert "Validation failed" in output


def test_undo_rolls_back_last_splitfunc_operation(tmp_path: Path, capsys) -> None:
    source = tmp_path / "main.py"
    original = (
        "import math\n\ndef area(r):\n    return math.pi * r * r\n\ndef hello(name):\n    return f'Hello, {name}'\n"
    )
    source.write_text(original, encoding="utf-8")

    split_exit = main(["splitfunc", "main.area", "--cwd", str(tmp_path)])

    assert split_exit == 0
    assert (tmp_path / "modules" / "area.py").exists()
    history_file = tmp_path / ".manasplice_history.json"
    history = json.loads(history_file.read_text(encoding="utf-8"))
    assert len(history) == 1

    undo_exit = main(["undo", "--cwd", str(tmp_path)])

    assert undo_exit == 0
    assert source.read_text(encoding="utf-8") == original
    assert not (tmp_path / "modules").exists()
    assert json.loads(history_file.read_text(encoding="utf-8")) == []

    output = capsys.readouterr().out
    assert "Rolled back 1 operation(s)." in output


def test_undo_rolls_back_splitall_as_one_operation(tmp_path: Path, capsys) -> None:
    source = tmp_path / "main.py"
    original = (
        "def area(r):\n"
        "    return r * r\n\n"
        "def hello(name):\n"
        "    return f'Hello, {name}'\n\n"
        "def orchestrate(value, name):\n"
        "    return area(value), hello(name)\n"
    )
    source.write_text(original, encoding="utf-8")

    split_exit = main(["splitall", "main.py", "--cwd", str(tmp_path)])

    assert split_exit == 0
    history_file = tmp_path / ".manasplice_history.json"
    history = json.loads(history_file.read_text(encoding="utf-8"))
    assert len(history) == 1

    undo_exit = main(["undo", "--cwd", str(tmp_path)])

    assert undo_exit == 0
    assert source.read_text(encoding="utf-8") == original
    assert not (tmp_path / "modules").exists()
    assert json.loads(history_file.read_text(encoding="utf-8")) == []

    output = capsys.readouterr().out
    assert "Split 3 function(s)." in output
    assert "Rolled back 1 operation(s)." in output


def test_undo_preserves_original_line_endings(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    original = b"def one():\r\n    return 1\r\n\r\ndef two():\r\n    return 2"
    source.write_bytes(original)

    split_exit = main(["splitall", "main.py", "--cwd", str(tmp_path)])
    undo_exit = main(["undo", "--cwd", str(tmp_path)])

    assert split_exit == 0
    assert undo_exit == 0
    assert source.read_bytes() == original
