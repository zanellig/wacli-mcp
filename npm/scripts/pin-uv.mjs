// Pins the uv release that bin/wacli-mcp.js downloads when uv is not on PATH.
// Usage: node scripts/pin-uv.mjs [version]   (default: the latest uv release)
// Reads the SHA-256 digests GitHub records for each release asset and writes uv.json.
import { writeFileSync } from "node:fs";

const targets = {
  "darwin-arm64": "uv-aarch64-apple-darwin.tar.gz",
  "darwin-x64": "uv-x86_64-apple-darwin.tar.gz",
  // The musl builds are static, so they also run on glibc distributions.
  "linux-arm64": "uv-aarch64-unknown-linux-musl.tar.gz",
  "linux-x64": "uv-x86_64-unknown-linux-musl.tar.gz",
  "win32-arm64": "uv-aarch64-pc-windows-msvc.zip",
  "win32-x64": "uv-x86_64-pc-windows-msvc.zip",
};

const version = process.argv[2];
const url = `https://api.github.com/repos/astral-sh/uv/releases/${version ? `tags/${version}` : "latest"}`;
const response = await fetch(url, { headers: { Accept: "application/vnd.github+json" } });
if (!response.ok) throw new Error(`${url}: HTTP ${response.status}`);
const release = await response.json();

const assets = {};
for (const [target, name] of Object.entries(targets)) {
  const asset = release.assets.find((a) => a.name === name);
  if (!asset?.digest?.startsWith("sha256:")) throw new Error(`release ${release.tag_name} has no digest for ${name}`);
  assets[target] = { name, sha256: asset.digest.slice("sha256:".length) };
}

const file = new URL("../uv.json", import.meta.url);
writeFileSync(file, `${JSON.stringify({ version: release.tag_name, assets }, null, 2)}\n`);
console.log(`Pinned uv ${release.tag_name}`);
