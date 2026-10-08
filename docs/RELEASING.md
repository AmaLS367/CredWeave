# Releasing CredWeave

Releases are published to PyPI by `.github/workflows/release.yml`, using PyPI Trusted Publishing
(OpenID Connect). No PyPI API token is stored in GitHub or in this repository. The workflow runs
only when a GitHub Release is **published**. Pushes, pull requests and drafts never publish.

## What the workflow does

1. **validate-tag**: the release tag must be exactly `vX.Y.Z` and must equal the `version` in
   `pyproject.toml`.
2. **checks**: Ruff, format check, strict mypy and the full pytest suite on Python 3.10 to 3.13,
   with the 90% branch-coverage gate on 3.13. Every matrix leg must pass.
3. **build**: from a clean checkout, builds the sdist and the wheel with `python -m build`, checks
   the file names against the tag, runs `twine check --strict`, and installs each artifact into a
   fresh virtual environment outside the workspace to confirm that `credweave.__version__` matches
   the tag and that a pool can acquire and report a lease. Only then are the distributions uploaded
   as the `python-distributions` artifact.
4. **publish**: runs in the `pypi` GitHub environment with `id-token: write` as its only permission,
   downloads the artifact and publishes it with `pypa/gh-action-pypi-publish@release/v1`.

Any failure stops the run before the publish job starts. The `pypi` environment is the only job
that holds an OIDC token.

## One-time setup

### 1. Create the PyPI trusted publisher

The project `credweave` does not exist on PyPI yet, so register a **pending publisher**. It creates
the project on the first successful publish.

1. Sign in to PyPI with an account that has two-factor authentication enabled.
2. Open <https://pypi.org/manage/account/publishing/> and choose **Add a new pending publisher**.
3. Enter these values exactly:

   | Field | Value |
   | :--- | :--- |
   | PyPI Project Name | `credweave` |
   | Owner | `AmaLS367` |
   | Repository name | `CredWeave` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

4. Save. Do not create an API token for this project.

### 2. Create the GitHub environment

1. Open the repository on GitHub: **Settings → Environments → New environment**.
2. Name it exactly `pypi`. The name is case-sensitive and must match step 1.
3. Recommended protection:
   - **Required reviewers**: add yourself, so every publish needs an explicit approval.
   - **Deployment branches and tags**: choose *Selected branches and tags* and add the tag rule
     `v*`. The release workflow runs on the tag, so a branch rule alone would block it.
4. Do not add any `PYPI_*` or `*_TOKEN` secret. Trusted Publishing needs none.

## Release procedure

1. Confirm `master` is green in CI and contains everything the release needs.
2. Set `version = "X.Y.Z"` in `pyproject.toml` and add a `## X.Y.Z` section to `CHANGELOG.md`.
   Commit and push to `master`. The version in the tag must match this value.
3. Optional local rehearsal from a clean tree:

   ```bash
   python -m pip install build twine
   python -m build --sdist --wheel --outdir dist/
   python -m twine check --strict dist/*
   ```

4. Create the release on GitHub: **Releases → Draft a new release**.
   - **Choose a tag**: type `vX.Y.Z` and select *Create new tag on publish*.
   - **Target**: `master` (the commit that carries the version bump).
   - Title and notes: paste the matching `CHANGELOG.md` section.
   - Click **Publish release**. A saved draft does not trigger the workflow.
5. Open the **Release** workflow run in the Actions tab. Approve the `pypi` deployment when prompted.
6. Verify the publication in a fresh environment:

   ```bash
   python -m venv /tmp/credweave-check
   /tmp/credweave-check/bin/python -m pip install credweave==X.Y.Z
   /tmp/credweave-check/bin/python -c "import credweave; print(credweave.__version__)"
   ```

   Also check <https://pypi.org/project/credweave/>.

## If something goes wrong

- **Failure before the publish job** (tag mismatch, lint, tests, build, or import check): nothing
  was uploaded. Fix the problem on `master`. Delete the release and its tag with
  `gh release delete vX.Y.Z --cleanup-tag`, then publish again with the same version.
- **Upload fails after some files are accepted**: PyPI never accepts a file name twice, so a
  partial or bad version cannot be replaced. Bump the version (for example to `X.Y.Z+1`), release
  again, and yank the bad version on PyPI.
- **A published version is faulty**: yank it on PyPI (*Manage → Releases → Options → Yank*). Yanking
  keeps existing pins working but stops new resolution. Deleting a published file is not possible
  and should not be attempted.
- **Trusted publisher rejected** (`invalid-publisher`): compare the workflow name, environment name
  and repository owner with the values in the table above. The workflow file must be on the tagged
  commit.

## Notes

- The workflow pins official `actions/*` releases by major version and publishes with the
  `release/v1` branch of `pypa/gh-action-pypi-publish`, as PyPI documents.
- `ci.yml` is unchanged. It still runs on pushes and pull requests to `master`.
- Local artifacts in `dist/` are gitignored and are never published by hand.
