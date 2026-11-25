import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "wacli-mcp"


def test_registry_and_plugin_versions_match_the_release():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]
    version = project["version"]
    registry = json.loads((ROOT / "server.json").read_text("utf-8"))
    packages = {package["registryType"]: package for package in registry["packages"]}
    assert set(packages) == {"pypi", "npm"}
    assert registry["version"] == version
    for package in packages.values():
        assert package["identifier"] == project["name"]
        assert package["version"] == version
    assert f"<!-- mcp-name: {registry['name']} -->" in (ROOT / project["readme"]).read_text("utf-8")

    # The npm package runs the PyPI release with its own version.
    npm = json.loads((ROOT / "npm" / "package.json").read_text("utf-8"))
    assert npm["name"] == project["name"]
    assert npm["version"] == version
    assert npm["mcpName"] == registry["name"]

    for client in (".claude-plugin", ".codex-plugin"):
        manifest = json.loads((PLUGIN / client / "plugin.json").read_text("utf-8"))
        assert manifest["version"] == version

    server = json.loads((PLUGIN / ".mcp.json").read_text("utf-8"))["mcpServers"]["whatsapp"]
    assert server["args"] == [f"{project['name']}=={version}"]
