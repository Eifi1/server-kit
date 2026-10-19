#!/usr/bin/env node
/**
 * The pre-push half of the commit-message rule: every commit a push would send must pass
 * `check-commit-msg.cjs` (Conventional Commits, a header of at most 100 characters) — the
 * same check this action runs in CI over the PR range.
 *
 * The commit-msg hook already checks each message as it is written, but `git commit
 * --no-verify` skips it, and so does every rewrite that doesn't re-run hooks. A push is the
 * last local moment to catch them. (From keksdose, where three such headers reached CI.)
 *
 * Use it from a pre-push hook, piping git's stdin through:
 *
 *     node scripts/check-pushed-commits.cjs "$1" < <stdin lines>
 *
 * Reads git's pre-push lines from stdin (`<local ref> <local sha> <remote ref> <remote
 * sha>`). The range is `<remote sha>..<local sha>`; for a branch the remote doesn't have
 * yet, everything not already on the remote's default branch (`<remote>/HEAD`, else
 * `<remote>/main`, else `<remote>/master`), or just the tip when none is known. A deletion
 * (local sha all zeros) checks nothing. The optional argument is the remote name (git
 * passes it to the hook as $1); it defaults to `origin`. Exit 1 with the offending commits
 * listed.
 *
 * Needs `check-commit-msg.cjs` next to it; dependency-free, so it runs without an install.
 */
const { execFileSync } = require("child_process");
const { validate } = require("./check-commit-msg.cjs");

const ZERO = /^0+$/;
const git = (...args) => execFileSync("git", args, { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim();

/** The remote's default branch as a ref (`origin/main`), or null when none is known. */
function defaultBranch(remote = "origin") {
  try {
    // refs/remotes/origin/HEAD -> refs/remotes/origin/<branch>, set by clone / `remote set-head`.
    const ref = git("symbolic-ref", "--quiet", `refs/remotes/${remote}/HEAD`);
    return ref.replace(/^refs\/remotes\//, "");
  } catch {
    /* no origin/HEAD — try the usual names */
  }
  for (const name of ["main", "master"]) {
    try {
      git("rev-parse", "--verify", "--quiet", `refs/remotes/${remote}/${name}`);
      return `${remote}/${name}`;
    } catch {
      /* not there */
    }
  }
  return null;
}

/** The rev-list range one pre-push line sends, or null when it sends nothing to check. */
function rangeOf(localSha, remoteSha, remote = "origin") {
  if (ZERO.test(localSha)) return null;
  if (!ZERO.test(remoteSha)) return `${remoteSha}..${localSha}`;
  const base = defaultBranch(remote);
  if (base) return `${base}..${localSha}`;
  // A root commit has no ~1; then the tip alone, via -1.
  try {
    git("rev-parse", "--verify", "--quiet", `${localSha}~1`);
    return `${localSha}~1..${localSha}`;
  } catch {
    return `-1 ${localSha}`;
  }
}

/** Every non-merge commit the pre-push input would send, deduplicated, in order. */
function pushedCommits(input, remote = "origin") {
  const shas = new Set();
  for (const line of input.split("\n")) {
    const [, localSha, , remoteSha] = line.trim().split(/\s+/);
    if (!localSha || !remoteSha) continue;
    const range = rangeOf(localSha, remoteSha, remote);
    if (!range) continue;
    let list;
    try {
      list = git("rev-list", "--no-merges", ...range.split(" "));
    } catch {
      // The remote sha is unknown locally (someone else pushed; not fetched): the tip only.
      list = git("rev-list", "--no-merges", "-1", localSha);
    }
    for (const sha of list.split("\n").filter(Boolean)) shas.add(sha);
  }
  return [...shas];
}

/** Returns the failures as [{ sha, subject, error }]. */
function check(input, remote = "origin") {
  const failures = [];
  for (const sha of pushedCommits(input, remote)) {
    const error = validate(git("log", "-1", "--format=%B", sha));
    if (error) failures.push({ sha, subject: git("log", "-1", "--format=%s", sha), error });
  }
  return failures;
}

module.exports = { defaultBranch, rangeOf, pushedCommits, check };

if (require.main === module) {
  const remote = process.argv[2] || "origin";
  const failures = check(require("fs").readFileSync(0, "utf8"), remote);
  for (const { sha, subject, error } of failures) {
    console.error(`✖ ${sha.slice(0, 8)} ${subject}`);
    console.error(`  ${error.replace(/\n/g, "\n  ")}`);
  }
  if (failures.length) {
    console.error(
      `\n${failures.length} commit message(s) would fail CI's Conventional Commits check. Reword ` +
        "them (git rebase -i, or git commit --amend for the last one) and push again.",
    );
    process.exit(1);
  }
}
