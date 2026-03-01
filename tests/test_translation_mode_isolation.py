from __future__ import annotations

import ast
from pathlib import Path


def _import_targets(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                targets.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            targets.add(node.module)
    return targets


def test_translation_mode_modules_do_not_import_agent_modules() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    translation_mode_files = (
        repo_root / "app" / "translation_bridge.py",
        repo_root / "app" / "nova_sonic.py",
        repo_root / "app" / "language_support.py",
    )

    for path in translation_mode_files:
        imports = _import_targets(path)
        forbidden = sorted(
            target for target in imports if target == "app.agent" or target.startswith("app.agent.")
        )
        assert not forbidden, (
            f"{path.name} should stay isolated from agent-mode code. "
            f"Found agent imports: {forbidden}"
        )
