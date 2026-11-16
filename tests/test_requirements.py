"""Regresi M-8: dependensi runtime harus lengkap dan sinkron dengan lock."""

import ast
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = BASE_DIR / "backend"

# Nama modul impor -> nama distribusi di requirements (PEP 503).
DISTRIBUTION_NAMES = {
    "dotenv": "python-dotenv",
}


def _canonical(name):
    return name.strip().lower().replace("_", "-")


def _requirements(path):
    """Nama distribusi -> baris persyaratan (komentar dan opsi -r dilewati)."""
    entries = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        requirement = line.split(";", 1)[0].strip()
        name = requirement
        for separator in ("==", ">=", "<=", "~=", "!=", "<", ">", "["):
            name = name.split(separator, 1)[0]
        entries[_canonical(name)] = requirement
    return entries


def _runtime_third_party_imports():
    """Modul pihak ketiga yang benar-benar diimpor kode backend."""
    local_modules = {path.stem for path in BACKEND_DIR.glob("*.py")}
    local_modules.update(
        path.name
        for path in BACKEND_DIR.iterdir()
        if path.is_dir() and (path / "__init__.py").is_file()
    )
    imports = set()
    for path in BACKEND_DIR.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif (
                isinstance(node, ast.ImportFrom)
                and node.level == 0
                and node.module
            ):
                imports.add(node.module.split(".", 1)[0])
    return imports - set(sys.stdlib_module_names) - local_modules


def test_every_runtime_third_party_import_is_declared():
    declared = _requirements(BASE_DIR / "requirements.txt")
    missing = []
    for module in sorted(_runtime_third_party_imports()):
        distribution = _canonical(DISTRIBUTION_NAMES.get(module, module))
        if distribution not in declared:
            missing.append(f"{module} ({distribution})")
    assert not missing, (
        "Dependensi runtime belum tercantum di requirements.txt: "
        + ", ".join(missing)
    )


def test_waitress_is_declared_and_pinned_in_lock():
    runtime = _requirements(BASE_DIR / "requirements.txt")
    locked = _requirements(BASE_DIR / "requirements.lock")

    assert "waitress" in runtime, "waitress wajib ada di requirements.txt"
    assert "waitress" in locked, "waitress wajib ada di requirements.lock"
    assert "==" in locked["waitress"], "waitress di lock harus dipin versi pasti"


def test_lock_covers_every_runtime_requirement_with_exact_version():
    runtime = _requirements(BASE_DIR / "requirements.txt")
    locked = _requirements(BASE_DIR / "requirements.lock")

    missing = sorted(name for name in runtime if name not in locked)
    assert not missing, f"Belum ada di requirements.lock: {', '.join(missing)}"
    unpinned = sorted(name for name in runtime if "==" not in locked[name])
    assert not unpinned, f"Belum dipin versi pasti: {', '.join(unpinned)}"


def test_serve_entrypoint_imports_declared_waitress():
    tree = ast.parse(
        (BACKEND_DIR / "serve.py").read_text(encoding="utf-8"),
        filename="backend/serve.py",
    )
    imports_waitress = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "waitress"
        and any(alias.name == "serve" for alias in node.names)
        for node in ast.walk(tree)
    )
    assert imports_waitress, "backend/serve.py harus memakai waitress di produksi"
    assert "waitress" in _requirements(BASE_DIR / "requirements.txt")
