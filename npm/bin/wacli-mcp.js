#!/usr/bin/env node
// Runs the wacli-mcp server from PyPI with uv, so `npx wacli-mcp` works like `uvx wacli-mcp`.
// If uv is not on PATH, downloads the uv release pinned in uv.json, checks its SHA-256, and caches it.
// stdout belongs to the MCP protocol: every message from this script goes to stderr.
"use strict";

const { spawn, spawnSync } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const pkg = require("../package.json");
const uvPin = require("../uv.json");

const isWindows = process.platform === "win32";
const exe = (name) => (isWindows ? `${name}.exe` : name);

function log(message) {
  process.stderr.write(`wacli-mcp: ${message}\n`);
}

function fail(message) {
  log(message);
  process.exit(1);
}

function findOnPath(name) {
  for (const dir of (process.env.PATH || "").split(path.delimiter)) {
    if (!dir) continue;
    const candidate = path.join(dir, exe(name));
    try {
      if (fs.statSync(candidate).isFile()) return candidate;
    } catch {
      // Not in this directory.
    }
  }
  return null;
}

function cacheRoot() {
  if (process.env.WACLI_MCP_CACHE_DIR) return process.env.WACLI_MCP_CACHE_DIR;
  if (isWindows) return path.join(process.env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local"), "wacli-mcp");
  if (process.platform === "darwin") return path.join(os.homedir(), "Library", "Caches", "wacli-mcp");
  return path.join(process.env.XDG_CACHE_HOME || path.join(os.homedir(), ".cache"), "wacli-mcp");
}

function findFile(dir, name) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      const found = findFile(full, name);
      if (found) return found;
    } else if (entry.name === name) {
      return full;
    }
  }
  return null;
}

async function downloadUv() {
  const asset = uvPin.assets[`${process.platform}-${process.arch}`];
  if (!asset) {
    fail(`no uv download for ${process.platform}-${process.arch}. Install uv (https://docs.astral.sh/uv/) and try again.`);
  }

  const versionDir = path.join(cacheRoot(), "uv", uvPin.version);
  const uvPath = path.join(versionDir, exe("uv"));
  if (fs.existsSync(uvPath)) return uvPath;

  const url = `https://github.com/astral-sh/uv/releases/download/${uvPin.version}/${asset.name}`;
  log(`uv is not on PATH; downloading uv ${uvPin.version} to ${versionDir}`);
  let body;
  try {
    const response = await fetch(url);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    body = Buffer.from(await response.arrayBuffer());
  } catch (error) {
    fail(`could not download ${url}: ${error.message}. Install uv (https://docs.astral.sh/uv/) and try again.`);
  }

  const digest = crypto.createHash("sha256").update(body).digest("hex");
  if (digest !== asset.sha256) {
    fail(`${asset.name} has SHA-256 ${digest}, expected ${asset.sha256}. Not running it.`);
  }

  // Extract into a private directory, then rename it into place, so a concurrent start never sees half a uv.
  fs.mkdirSync(path.dirname(versionDir), { recursive: true });
  const work = fs.mkdtempSync(path.join(path.dirname(versionDir), `.${uvPin.version}-`));
  try {
    const archive = path.join(work, asset.name);
    fs.writeFileSync(archive, body);
    const extracted = path.join(work, "extracted");
    fs.mkdirSync(extracted);
    // Windows 10 1803 and later ship bsdtar, which also reads zip files. Use it by full path so another tar
    // on PATH (Git for Windows, MSYS) does not take its place.
    const tar = isWindows ? path.join(process.env.SystemRoot || "C:\\Windows", "System32", "tar.exe") : "tar";
    const result = spawnSync(tar, ["-xf", archive, "-C", extracted], { stdio: ["ignore", "ignore", "inherit"] });
    if (result.error || result.status !== 0) {
      throw new Error(`could not extract ${asset.name}${result.error ? `: ${result.error.message}` : ""}.`);
    }

    const staged = path.join(work, "uv");
    fs.mkdirSync(staged);
    for (const name of ["uv", "uvx"]) {
      const found = findFile(extracted, exe(name));
      if (!found) throw new Error(`${asset.name} does not contain ${exe(name)}.`);
      const target = path.join(staged, exe(name));
      fs.copyFileSync(found, target);
      fs.chmodSync(target, 0o755);
    }

    try {
      fs.renameSync(staged, versionDir);
    } catch (error) {
      // Another process finished first; use its copy.
      if (!fs.existsSync(uvPath)) throw error;
    }
  } finally {
    fs.rmSync(work, { recursive: true, force: true });
  }
  return uvPath;
}

function fromSpec() {
  if (process.env.WACLI_MCP_FROM) return process.env.WACLI_MCP_FROM;
  const extras = (process.env.WACLI_MCP_EXTRAS || "").replace(/\s+/g, "");
  if (extras && !/^[a-z0-9-]+(,[a-z0-9-]+)*$/.test(extras)) {
    fail(`WACLI_MCP_EXTRAS must be a comma-separated list such as "transcribe", got "${extras}".`);
  }
  return extras ? `wacli-mcp[${extras}]==${pkg.version}` : `wacli-mcp==${pkg.version}`;
}

async function main() {
  const uv = findOnPath("uv") || (await downloadUv());
  const args = ["tool", "run", "--from", fromSpec()];
  // onnxruntime, which the transcription extras need, has no wheels for the newest Python.
  if (/transcribe/.test(process.env.WACLI_MCP_EXTRAS || "") && !process.env.UV_PYTHON) {
    args.push("--python", "3.13");
  }
  args.push("wacli-mcp", ...process.argv.slice(2));

  const child = spawn(uv, args, { stdio: "inherit", windowsHide: true });
  for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"]) {
    process.on(signal, () => child.kill(signal));
  }
  child.on("error", (error) => fail(`could not start ${uv}: ${error.message}`));
  child.on("exit", (code, signal) => process.exit(code ?? (signal ? 1 : 0)));
}

main().catch((error) => fail(error.message));
