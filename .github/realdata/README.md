# Real-data check (CI)

Records about real, partly living people must never enter this
repository or its GitHub text. Local sessions check what they push
against the owner's out-of-repo denylist. Cloud agents never run that
local check, so every pull request into `main` is also checked in CI by
`.github/workflows/realdata-check.yml`.

## What is checked

Everything a pull request would add, and nothing already upstream:

- the added lines of its diff (removed and context lines are ignored);
- the paths of files it adds, renames or copies;
- its commit messages;
- its title and body (re-checked when they are edited).

A hit fails the check and is reported as `location: category`, for
example `docs/notes.md:12: person_name`,
`commit abc1234 message:3: date`, `pr-title-and-body:5: place` or
`new path of diff file #2: organisation`. The matched text is never
printed. Fix the pull request by replacing the real data with fictional
examples; do not quote the matched text in comments.

## How it works

`hashes.txt` holds, for every denylist entry in every category
(person names, places, organisations, each rendering of each date),
`sha256(salt + canonical entry)` and its category. The salt is the
`REALDATA_SALT` Actions secret, so without it the list cannot be
tested against guesses such as common surnames. The canonical form
lowercases, applies NFKD, drops combining marks and keeps the `\w+`
words joined by single spaces; `max-ngram` records the longest entry
in words.

`scripts/ci/realdata_check.py` (standard library only) canonicalises
the added text the same way, hashes every run of 1 to `max-ngram`
consecutive words, and looks each hash up. Consecutive added lines are
one word stream, so an entry wrapped across lines is still found. Case,
accents and punctuation between words do not matter (`2999-02-31`
matches `2999/02/31`), but each date rendering (`31 February 2999`,
`2999-02-31`) is its own entry.

## Security model

`pull_request_target` runs the workflow as it is on `main`, with the
secret available. The job checks out the base branch only, reads the
pull request as text through the API with a read-only token, and runs
the base branch's checker on those files. Pull-request code is never
checked out, installed or executed, and no pull-request value is
interpolated into a shell command. A pull request cannot change the
check that runs on it; changes to the workflow, checker or hash list
take effect once merged.

## Failing closed

The check fails (exit 2) rather than passing when it cannot check:
`REALDATA_SALT` is unset or empty, the secret does not match the
list's `salt-check` line (wrong secret, or salt rotated without
regenerating the list), `hashes.txt` is missing or malformed, or the
diff cannot be fetched (the API refuses very large diffs: split the
pull request).

## Maintenance (owner only)

`hashes.txt` is generated out of repo from the private denylist with
the private salt; never edit it by hand. After a denylist change the
owner regenerates it and lands the new file through a pull request.
The procedure, the salt and a fictional canary token for testing the
check live only in the owner's private workspace.

To run the check locally against a branch, with the salt in the
environment:

```sh
git diff origin/main...HEAD > /tmp/pr.diff
REALDATA_SALT=... python3 scripts/ci/realdata_check.py --diff /tmp/pr.diff
```
