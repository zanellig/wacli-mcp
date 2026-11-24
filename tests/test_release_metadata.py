import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_changelog_has_notes_for_the_current_version():
    version = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]["version"]
    changelog = (ROOT / "CHANGELOG.md").read_text("utf-8")
    assert re.search(rf"^## \[{re.escape(version)}\]", changelog, re.MULTILINE), (
        f"Add a '## [{version}]' section to CHANGELOG.md before releasing {version}."
    )
    assert f"[{version}]: https://github.com/zanellig/wacli-mcp/releases/tag/v{version}" in changelog
