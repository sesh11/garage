import ast
import sys
import subprocess
from pathlib import Path


def test_generic_service_has_no_ambient_home_imports_or_app_prompt_terms() -> None:
    service_root = Path(__file__).parents[1] / "src" / "garage"
    for path in service_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported = [node.module or ""]
            else:
                continue
            assert all(not name.startswith("ambient_home") for name in imported), path
    forbidden = ("ambient-home", "launchctl", "reachy", "my-mac", "alfred")
    for path in service_root.rglob("*.py"):
        content = path.read_text(encoding="utf-8").lower()
        for term in forbidden:
            assert term not in content, f"{term} found in {path}"


def test_service_imports_with_ambient_home_blocked() -> None:
    script = """
import importlib.abc
import sys
class BlockAmbient(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "ambient_home" or fullname.startswith("ambient_home."):
            raise ModuleNotFoundError("ambient_home intentionally blocked")
        return None
sys.meta_path.insert(0, BlockAmbient())
import garage, garage.cli, garage.ui, garage.dispatch
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
