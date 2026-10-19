#!/usr/bin/env node
/**
 * Validate a commit message against the Conventional Commits format.
 *
 * The allowed types come from the CALLING repo's commit-and-tag-version config,
 * so a commit that passes here is one its release tool can categorise. The
 * config is looked up in the working directory (the repo root), in whichever
 * flavour the repo uses: .versionrc.js (kastlan), .versionrc.cjs (ui-kit, an
 * ESM package), .versionrc.json (keksdose, kurvenschmiede). A repo with no
 * config gets the standard Conventional Commits types.
 *
 * Dependency-free on purpose (no commitlint), so it runs without an install.
 */
const fs = require("fs");
const path = require("path");

const DEFAULT_TYPES = [
  "feat", "fix", "perf", "refactor", "revert",
  "docs", "style", "chore", "test", "build", "ci",
];

function loadTypes(dir = process.cwd()) {
  for (const name of [".versionrc.js", ".versionrc.cjs"]) {
    const file = path.join(dir, name);
    if (fs.existsSync(file)) return { types: require(file).types, source: name };
  }
  const json = path.join(dir, ".versionrc.json");
  if (fs.existsSync(json)) {
    return { types: JSON.parse(fs.readFileSync(json, "utf8")).types, source: ".versionrc.json" };
  }
  return { types: null, source: null };
}

const loaded = loadTypes();
const TYPES = loaded.types ? loaded.types.map((t) => t.type) : DEFAULT_TYPES;
const SOURCE = loaded.source ?? "the Conventional Commits defaults";

// <type>(<scope>)?!?: <subject>  — scope and the breaking-change "!" are optional.
const HEADER = new RegExp(`^(${TYPES.join("|")})(\\([\\w .,/-]+\\))?!?: .+`);
const MAX_HEADER = 100;

/** Returns an error string if the message is invalid, or null when it's fine. */
function validate(message) {
  const firstLine =
    message.split("\n").find((l) => l.trim() && !l.startsWith("#")) ?? "";
  // git generates these itself; don't reject them.
  if (/^(Merge|Revert|fixup!|squash!)/.test(firstLine)) return null;
  if (firstLine.length > MAX_HEADER) {
    return `Header is ${firstLine.length} chars (max ${MAX_HEADER}).`;
  }
  if (!HEADER.test(firstLine)) {
    return [
      "Commit messages must follow Conventional Commits:",
      "  <type>(<scope>)?: <subject>",
      `  allowed types (from ${SOURCE}): ${TYPES.join(", ")}`,
      '  example: "feat(api): add pagination to the list endpoint"',
      `  got: "${firstLine}"`,
    ].join("\n");
  }
  return null;
}

module.exports = { validate, TYPES };

if (require.main === module) {
  const arg = process.argv[2];
  let message = arg ?? "";
  // A commit-msg hook passes a file path; also accept a raw string.
  try {
    if (arg && fs.existsSync(arg)) message = fs.readFileSync(arg, "utf8");
  } catch {
    /* not a path — treat the argument as the message itself */
  }
  const error = validate(message);
  if (error) {
    console.error(`✖ ${error.replace(/\n/g, "\n  ")}`);
    process.exit(1);
  }
}
