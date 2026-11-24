import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "wacli-mcp"


def test_registry_and_plugin_versions_match_the_release():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]
    version = project["version"]
    registry = json.loads((ROOT / "server.json").read_text("utf-8"))
    (package,) = registry["packages"]
    assert registry["version"] == package["version"] == version
    assert package["identifier"] == project["name"]
    assert f"<!-- mcp-name: {registry['name']} -->" in (ROOT / project["readme"]).read_text("utf-8")

    for client in (".claude-plugin", ".codex-plugin"):
        manifest = json.loads((PLUGIN / client / "plugin.json").read_text("utf-8"))
        assert manifest["version"] == version

    server = json.loads((PLUGIN / ".mcp.json").read_text("utf-8"))["mcpServers"]["whatsapp"]
    assert server["args"] == [f"{project['name']}=={version}"]
