# Releasing

jev-browse is published to [PyPI](https://pypi.org/project/jev-browse/) by the
[`publish.yml`](../.github/workflows/publish.yml) workflow whenever a `v*` tag is pushed. The workflow uses PyPI
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/): PyPI trusts this repository's workflow through
OpenID Connect, so no API token is stored anywhere. Only a maintainer can publish.

## One-time setup (maintainer)

Do this once, before the first release.

1. **Register a pending trusted publisher on PyPI.** The project does not exist on PyPI yet, so add a *pending*
   publisher: sign in at [pypi.org](https://pypi.org), open **Your account → Publishing**
   ([pypi.org/manage/account/publishing](https://pypi.org/manage/account/publishing/)), and under
   **Add a new pending publisher → GitHub** enter exactly:

   | Field | Value |
   |---|---|
   | PyPI Project Name | `jev-browse` |
   | Owner | `danielnc` |
   | Repository name | `jev-browse` |
   | Workflow name | `publish.yml` |
   | Environment name | `pypi` |

   The first successful upload creates the project and turns the pending publisher into a normal one. A pending
   publisher does not reserve the name: until that first upload, anyone can still register `jev-browse`.
   After the project exists, the publisher is managed under the project's **Settings → Publishing**.

2. **Create the `pypi` environment on GitHub.** In the repository, open **Settings → Environments → New
   environment**, name it `pypi`, and (recommended) add yourself under **Required reviewers**, so every publish waits
   for your approval. Optionally, limit **Deployment branches and tags** to the tag pattern `v*`.

3. (Optional) Try the whole flow against [TestPyPI](https://test.pypi.org) first: register the same pending
   publisher there, and temporarily add `with: repository-url: https://test.pypi.org/legacy/` to the publish step.

## Cutting a release

1. On a branch, bump `__version__` in `jev_browse/__init__.py` (the only place the version lives), move the
   `[Unreleased]` entries in `CHANGELOG.md` under the new version and date, and bump `version` in
   `.claude-plugin/plugin.json` to match. Merge the PR.
2. Check the package locally: `make check`, then `uv build` and install the wheel into a fresh venv
   (`uv venv /tmp/jb && uv pip install --python /tmp/jb/bin/python dist/*.whl && /tmp/jb/bin/jev-browse --help`).
3. Tag the merge commit on `main` and push the tag:

   ```bash
   git tag -a v0.1.0 -m "jev-browse 0.1.0"
   git push origin v0.1.0
   ```

4. The workflow checks that the tag equals `v` + `jev_browse.__version__`, runs the linter and tests, builds the
   sdist and wheel, smoke-tests the wheel in a clean venv, and then (after your approval, if you set reviewers)
   uploads to PyPI. A mismatched tag fails before anything is built.
5. Verify: `uv tool install jev-browse` (or `uv tool upgrade jev-browse`), then `jev-browse doctor`.

A version can be uploaded to PyPI only once. To fix a bad release, bump the version and release again. Yank the
bad version on PyPI if needed; don't delete it.
