// Starts bin/wacli-mcp.js, sends an MCP initialize request, and checks the reply.
// It runs the server from this checkout (WACLI_MCP_FROM), so it tests the code in the repository.
// With --without-uv, it hides uv from PATH and uses an empty cache, so the wrapper must download uv.
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { delimiter, join } from "node:path";
import { fileURLToPath } from "node:url";

const npmDir = fileURLToPath(new URL("..", import.meta.url));
const repoRoot = join(npmDir, "..");
const withoutUv = process.argv.includes("--without-uv");
const exe = (name) => (process.platform === "win32" ? `${name}.exe` : name);

const env = { ...process.env, WACLI_MCP_FROM: process.env.WACLI_MCP_FROM || repoRoot };
let cacheDir;
if (withoutUv) {
  env.PATH = (env.PATH || "")
    .split(delimiter)
    .filter((dir) => dir && !existsSync(join(dir, exe("uv"))))
    .join(delimiter);
  cacheDir = mkdtempSync(join(tmpdir(), "wacli-mcp-smoke-"));
  env.WACLI_MCP_CACHE_DIR = cacheDir;
}

const child = spawn(process.execPath, [join(npmDir, "bin", "wacli-mcp.js")], {
  env,
  stdio: ["pipe", "pipe", "inherit"],
});

const timeout = setTimeout(() => {
  console.error("No initialize reply within 10 minutes.");
  child.kill();
  process.exit(1);
}, 600_000);

let stdout = "";
let replied = false;
child.stdout.on("data", (chunk) => {
  stdout += chunk;
  for (const line of stdout.split("\n").slice(0, -1)) {
    const message = JSON.parse(line); // Anything on stdout that is not JSON-RPC breaks MCP clients.
    if (message.id === 1) {
      if (!message.result?.serverInfo) throw new Error(`Unexpected reply: ${line}`);
      console.error(`Server replied: ${message.result.serverInfo.name} ${message.result.serverInfo.version}`);
      replied = true;
      child.stdin.end();
    }
  }
  stdout = stdout.slice(stdout.lastIndexOf("\n") + 1);
});

child.on("exit", (code) => {
  clearTimeout(timeout);
  if (cacheDir) {
    const downloaded = existsSync(join(cacheDir, "uv"));
    rmSync(cacheDir, { recursive: true, force: true });
    if (!downloaded) {
      console.error("Expected the wrapper to download uv into the cache.");
      process.exit(1);
    }
  }
  if (!replied) {
    console.error(`Server exited with code ${code} before replying.`);
    process.exit(1);
  }
  console.error("Smoke test passed.");
});

child.stdin.write(
  `${JSON.stringify({
    jsonrpc: "2.0",
    id: 1,
    method: "initialize",
    params: {
      protocolVersion: "2025-06-18",
      capabilities: {},
      clientInfo: { name: "wacli-mcp-smoke", version: "0" },
    },
  })}\n`,
);
