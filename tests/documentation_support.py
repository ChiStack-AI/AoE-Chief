"""Read the complete user documentation for existing phrase assertions."""
from pathlib import Path


def read_documentation():
    root = Path(__file__).resolve().parent.parent
    paths = [root / "README.md"] + sorted((root / "docs").glob("*.md"))
    return "\n\n".join(path.read_text(encoding="utf-8") for path in paths)
