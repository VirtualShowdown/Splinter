from __future__ import annotations

import json
from pathlib import Path

from splinter.cli import main


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


def test_splitfunc_validate_rejects_invalid_generated_output(tmp_path: Path, monkeypatch, capsys) -> None:
    source = tmp_path / "main.py"
    original = "def area(r):\n    return r * r\n"
    source.write_text(original, encoding="utf-8")

    def broken_insert_import(source_text: str, import_statement: str) -> str:
        del import_statement
        return source_text + "\nthis is not valid python(\n"

    monkeypatch.setattr("splinter.splitter._insert_import", broken_insert_import)

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
    history_file = tmp_path / ".splinter_history.json"
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
    history_file = tmp_path / ".splinter_history.json"
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
