# Canonical Repository Gate Record

Source process: issue #2 "Canonical Repository State, Environment Reconciliation and Public-Release Gate" in the previous repository. This file is the record for the new organisation repository. All gate work is done here; the previous repository is relegated and read-only.

Date: 2026-10-03

## Gate 1 — Canonical Git state (complete)

| Item | Value |
|---|---|
| Canonical repository | https://github.com/gritnations/SaaS |
| Visibility | Public |
| Default branch | `main` (only branch) |
| Frozen head SHA | `2f436e45cd5c3b6aceb5a119ab40b8f03bd7ef53` ("Initial commit") |
| Primary working tree | `D:\Gritnations\SaaS-org` |
| Git metadata | `D:\Gritnations\SaaS-org\.git` |
| Remote | `origin` = `https://github.com/gritnations/SaaS.git` (fetch and push), only remote |
| Working-tree status | Clean, in sync with `origin/main`, no untracked files |
| Contents at freeze | `LICENSE` only |
| Local `main` vs remote `main` | Identical (`git ls-remote origin refs/heads/main` and GitHub API both return the frozen SHA) |

Previous repository (`D:\Gritnations\SaaS`, 24 commits, remote `GN-TJ/SaaS`) is relegated. It is not a source of truth. Nothing has been pushed from it to the new repository.

### Development environment

| Tool | Version |
|---|---|
| Node.js | v24.18.0 |
| npm | 11.16.0 |
| Firebase CLI | 15.30.2 (global) |
| Python | 3.14.6 |
| Java | OpenJDK 21.0.12.1 LTS (Microsoft Build), installed 2026-10-03 via winget, `C:\Program Files\Microsoft\jdk-21.0.12.101-hotspot` |
| IDE | `code` not on PATH; no IDE-specific clone identified. Only one working tree exists for this repository. |

Java was missing at the start of Gate 1 and was installed as a prerequisite for the Firestore emulator (Java 21+ required).

### Reproduce

```bash
git rev-parse HEAD
git remote -v
git status --short --branch
git ls-remote origin
gh api repos/gritnations/SaaS --jq '{default_branch, visibility}'
java -version
```

## Gate 2 — Independent human verification (complete)

- Reviewer 1: verified repository identity, working-tree location, frozen head SHA, remote configuration and working-tree status. **Accepted.**
- Reviewer 2: independently verified the same checks. **Accepted.**
- Accepted baseline SHA: `2f436e45cd5c3b6aceb5a119ab40b8f03bd7ef53`.
- Discrepancies: none.
- Reviewer 2 posts the verification record to issue #2 in the previous repository.

## Gate 3 — Dependencies and environments (awaiting verification)

Code origin: 77 tracked files copied from the previous repository at its HEAD `c65707d`, without history, into `SaaS-org`. `COMPETITION.md` was deliberately not copied (market research stays out of this public repository). The org `LICENSE` was kept.

### Manifests and lockfiles

| Suite | Manifest | Lockfile |
|---|---|---|
| `schedule-booking/functions` | `package.json` (Node 22 engine) | `package-lock.json` |
| `schedule-booking/rules-test` | `package.json` | `package-lock.json` |
| `schedule-booking/admin` | none (plain `node`) | none |

### What each suite needs

- **functions** (`npm ci && npm test`): Node only. Uses an in-memory Firestore stand-in. No Java, emulator or Firebase CLI. ERROR lines in the output come from failure-path tests; the exit code is the result.
- **admin** (`node test/lib.test.js`): Node only.
- **rules-test** (`npm ci && npm test`): runs `firebase emulators:exec --only firestore --project demo-booking` with `../firebase.json`. Needs the Firebase CLI and Java 21+.

### firebase-tools decision input

`firebase-tools` is not declared or version-locked in any `package.json` or lockfile. The rules test depends on a globally installed CLI. CI installs it unpinned with `npm install -g firebase-tools`. The global install is therefore currently required. Pinning is decided in Gate 4.

### Environment comparison

| | Local | CI (`tests.yml`) |
|---|---|---|
| Node | v24.18.0 | 22 |
| npm | 11.16.0 | bundled with Node 22 |
| Java | Microsoft OpenJDK 21.0.12.1 | Temurin 21 |
| Firebase CLI | 15.30.2 (global) | latest at run time (unpinned) |

Local Node (24) differs from the declared engine (22). Tests passed on 24.

### Secrets and environment variables

None needed for tests. The rules test uses the demo project id `demo-booking`. No secret values were read or recorded. Runtime secrets (payment, email, calendar keys) are Secret Manager values defined in code only.

### Test results from the copied baseline (2026-10-03, local)

| Suite | Result |
|---|---|
| functions (`npm test`, 8 test files) | exit 0; final file 89 passed, 0 failed |
| admin (`node test/lib.test.js`) | 153 passed, 0 failed |
| rules-test (`npm test`, emulator) | 103 passed, 0 failed |

`npm ci` printed an `allow-scripts` warning about install scripts (for example `protobufjs`). Scripts were not approved and tests still passed.

Reproduce:

```bash
cd schedule-booking/functions && npm ci && npm test
cd ../admin && node test/lib.test.js
cd ../rules-test && npm ci && npm test   # needs Java 21+ and firebase CLI
```

Exit criterion: dependencies, lockfiles and runtime requirements documented; tests reproduce from the baseline. No blockers.

## Gate 4 — CI workflow (awaiting verification)

Finding: the `rules` job in `.github/workflows/tests.yml` ran `npm install -g firebase-tools` (unpinned). The rules test script calls `firebase emulators:exec`, so the CLI is required, but nothing declared or locked it. Removing the global install without a declared dependency would have broken the job, so it was not removed on assumption.

Decision: make `firebase-tools` a declared, exact-pinned project devDependency of `schedule-booking/rules-test`, and remove the global install step. npm puts `node_modules/.bin` first on the path for scripts, so the pinned CLI is the one used.

Changes (smallest justified):

1. `schedule-booking/rules-test/package.json`: added devDependency `"firebase-tools": "15.30.2"` (the version already used in Gate 3).
2. `schedule-booking/rules-test/package-lock.json`: updated by `npm install --save-dev --save-exact`; locks `firebase-tools` 15.30.2.
3. `.github/workflows/tests.yml`: removed the `npm install -g firebase-tools` step from the `rules` job. Nothing else changed.

Local verification (2026-10-03): the global npm directory was removed from PATH (no `firebase` resolvable globally), then `npm ci && npm test` in `rules-test` ran. Result: 103 passed, 0 failed, exit 0. `npm exec -- firebase --version` reports 15.30.2 from `node_modules`.

CI verification: **not yet run.** A CI run needs the change pushed, which has not been done. Gate 4 exit criterion (CI checks pass, commit SHA recorded) stays open until the first push and CI run. Process deviation: the issue asks for a separate PR; by instruction this record and the change land in one commit. Commit SHA and CI outcome: to be recorded after push.

Known follow-up (not changed here): CI uses Node 22 and unpinned `ubuntu-latest`; local Node is 24.

## Gate 5 — Public visibility and release readiness (amended: push, then show commit before CI)

Baseline for this review: frozen head `2f436e45cd5c3b6aceb5a119ab40b8f03bd7ef53` plus the uncommitted gate changes in the working tree (78 files). The repository is already public, so anything pushed is published immediately.

### Secrets and credentials (files)

Scanned all non-`node_modules` files for private keys, cloud/API key formats, GitHub/Slack/AWS tokens, `client_secret`, hard-coded key/secret/password assignments, and sensitive filenames (`.env`, service accounts, `.pem`, `.key`, `.firebaserc`).

Result: **no real credentials found.** Matches reviewed:

- `demo/run-demo.js` `ADMIN_PASSWORD = 'demo-password-1'` and `README.md`: documented as existing only in the local Auth emulator.
- `admin/js/config.js` `apiKey: 'YOUR_WEB_API_KEY'`, project id `your-project-id`: placeholders. `demo/run-demo.js` swaps in `fake-demo-key`.
- `functions/lib/gateways/paymob.js`: a code reference to the `client_secret` field returned by the payment API, not a value.
- Secrets used at runtime are defined by name only (Secret Manager); no values are in the repository.
- `.firebaserc` is absent. `firestore-debug.log` exists on disk but is gitignored.

### Personal and client information

- Email addresses in files are all placeholders (`example.com`, `test.example`) or fake demo customers (`*@example.com`). Demo customer names in `demo/run-demo.js` are fictional.
- No client name, domain or founder name found in files (searched: client/founder names, Linear URLs, local paths).
- `GATE_RECORD.md` contains local Windows paths and the GitHub username `GN-TJ`. The username is already public.
- **Commit identity (decided):** the existing public initial commit shows a personal work email as author. History already published cannot be made private by a new commit. The gate commit uses the GitHub noreply address for the `GN-TJ` account, so no further email is published.

### Internal references (decided: publish, redacted)

Decision by the responsible humans: publish `CLAUDE.md` and `ARCHITECTURE.md`, with internal tracker references redacted. Done in this commit:

- Removed Linear project names, document titles, team name and issue ids (`AIL-n`) from `ARCHITECTURE.md`, `CLAUDE.md`, `README.md` and `schedule-booking/SAAS_README.md`, replaced with neutral wording ("the project tracker").
- Removed all references to `COMPETITION.md`, which is not in this repository (including its `README.md` section).
- Corrected stale statements: the repository is no longer described as private or as carrying subtree history.
- Kept generic role names (CEO, CTO, COO) and the statement that the original client is the first pilot customer; neither names a person or client. Added a rule to `CLAUDE.md` not to put tracker ids, project names or document titles in the repository.

### Git history

The new repository has one commit (`Initial commit`, `LICENSE` only). The previous repository's 24-commit history was not copied, so its history is not published. The gate commit will add one more commit.

### Repository configuration (GitHub API)

| Setting | Before | After (changed 2026-10-03 on instruction) |
|---|---|---|
| Visibility | public | public |
| Default branch | `main` | `main` |
| Branch protection on `main` | none | pull request required with 1 approval, stale approvals dismissed, force-push and deletion blocked, admins may bypass |
| Required status checks | none | none yet (the check names exist only after the first CI run; add them then) |
| Secret scanning | disabled | enabled |
| Push protection | disabled | enabled |
| Actions permissions | all actions allowed, SHA pinning not required | unchanged; consider restricting to GitHub-owned actions |
| Default workflow token permissions | read; Actions cannot approve PRs | unchanged (good) |
| Workflow `permissions` | `contents: read` | unchanged (good) |
| Triggers | `push` to `main`, `pull_request`, `workflow_dispatch` | unchanged; no `pull_request_target`, no secrets used |

### Gate 5 amendment (agreed by both reviewers)

Gate 5 is amended into two steps so that CI is never run before the pushed change has been shown:

1. **Push the changes.** The single commit named `gate 1` is pushed to a new branch `gate-1`, not to `main`. The `tests.yml` workflow runs only on `push` to `main` and on pull requests, so pushing the branch does not start CI.
2. **Show the commit.** The commit SHA and contents are shown to the humans for verification. Only after that is CI run (by opening a pull request or manually dispatching the workflow), and the outcome is recorded.

The baseline `2f436e45cd5c3b6aceb5a119ab40b8f03bd7ef53` remains the parent of the gate commit.

### Gate 5 status

Review evidence is complete and the repository settings are in place. The exit criterion is **not met** until:

1. The `gate 1` commit is pushed to `gate-1` and its SHA is shown and verified.
2. CI is run on it and passes (this also closes the Gate 4 CI criterion).
3. The responsible humans give explicit approval to keep the repository public, and the final pre-publication SHA is recorded here.

Approvals: none recorded.
