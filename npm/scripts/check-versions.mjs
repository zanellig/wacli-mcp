// Fails unless every file that names the release version agrees with pyproject.toml.
// The npm package runs the PyPI release with its own version, so the two must match.
import { readFileSync } from "node:fs";

const root = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, root), "utf8");
const json = (path) => JSON.parse(read(path));

const expected = read("pyproject.toml").match(/^version = "([^"]+)"/m)?.[1];
if (!expected) throw new Error("pyproject.toml has no version");

const server = json("server.json");
const found = {
  "npm/package.json": json("npm/package.json").version,
  "server.json version": server.version,
  ...Object.fromEntries(server.packages.map((p) => [`server.json ${p.registryType} package`, p.version])),
  "plugins/wacli-mcp/.claude-plugin/plugin.json": json("plugins/wacli-mcp/.claude-plugin/plugin.json").version,
  "plugins/wacli-mcp/.codex-plugin/plugin.json": json("plugins/wacli-mcp/.codex-plugin/plugin.json").version,
  "plugins/wacli-mcp/.mcp.json": json("plugins/wacli-mcp/.mcp.json").mcpServers.whatsapp.args.at(-1).split("==")[1],
};

const wrong = Object.entries(found).filter(([, version]) => version !== expected);
for (const [where, version] of wrong) console.error(`${where} has version ${version}, pyproject.toml has ${expected}`);
if (wrong.length) process.exit(1);
console.log(`All versions are ${expected}`);
