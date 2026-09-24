# Contributing to jev-browse

Thanks for helping. Bug reports, benchmark results from your setup, and new text backends are especially welcome.

## Setup

```bash
git clone https://github.com/danielnc/jev-browse && cd jev-browse
uv sync                 # pytest and ruff; the package itself is stdlib-only
make check              # ruff, the offline test suite, and a syntax check of snapshot.js
pre-commit install      # optional: the gitleaks hook blocks commits that contain keys
```

`make check` needs no network, no TypeSafe key, and no browser: every external service is faked.

To try your changes in a real browser, install from your checkout (`python3 -m jev_browse install`) and run
`python3 -m jev_browse doctor`. See `install.md`.

## Making a change

1. Open an issue first for anything larger than a bug fix, so we can agree on the approach.
2. Write a failing test, then the change. Tests live in `tests/`, one file per module, with fakes in
   `tests/fakes.py`.
3. Keep the runtime standard-library only.
4. New setting? Add it to the registry in `jev_browse/config.py`, then run `make docs-gen`. A test fails if
   `docs/config.example.toml` or the table in `docs/configuration.md` drifts.
5. Behaviour a calling agent relies on (arguments, hand-back reasons, printed lines)? Update `skill/SKILL.md` and
   `skill/reference.md` too.
6. Add a line to `CHANGELOG.md` under "Unreleased".
7. `make check` must pass.

Agents contributing code: `AGENTS.md` has the same rules in checklist form.

## Adding a text backend

See "Adding a backend" in `docs/backends.md`. Include `bench/text_eval.py` results for at least one model in the
pull request, labelled with your hardware or provider. If you cannot measure it, mark it unmeasured in the docs.

## Sharing benchmark results

Results from other machines make the benchmark more honest. Run `bench/run_bench.py` or `bench/text_eval.py` (see
`docs/benchmarking.md`) and open an issue with the summary: hardware, OS, calling model, N, and the rows file.
**Do not attach `bench/results/raw/`**: it contains page text from your browser.

## Privacy and secrets

- Never commit keys, `.env` files, real names, emails, hostnames, IPs, or home paths. Use `example.com`,
  Wikipedia, and `127.0.0.1` in tests and docs.
- Private URLs (a local model server, a custom endpoint) must never appear in errors, traces, or `doctor` output.

## Security issues

Please don't open a public issue for a vulnerability, for example a way to make jev-browse click a commit button
without authorisation, leak a sensitive field, or act on a tab it does not own. Report it privately through GitHub's
"Report a vulnerability" (Security tab) on this repository.

## License

By contributing you agree that your contributions are licensed under the MIT License (see `LICENSE`).
