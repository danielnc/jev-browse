.PHONY: check check-secrets clean-traces install doctor docs-gen

HARNESS_TMP = $${BH_TMP_DIR:-$${BH_HOME:-$${BROWSER_HARNESS_HOME:-$${XDG_CONFIG_HOME:-$$HOME/.config}/browser-harness}}/tmp}

check:
	uv run ruff check . && uv run pytest -q && node --check jev_browse/snapshot.js

check-secrets:
	gitleaks detect --source . --redact --log-opts="--all"

# Removes jev-browse traces, run files, and screenshots. Never touches the owned-tab
# registry (jev-browse-owned-*) and skips run files of live, unfinished runs.
clean-traces:
	rm -rf bench/results/raw .local/traces
	uv run python -m jev_browse.cleanup "$(HARNESS_TMP)"

install:
	python3 -m jev_browse install

doctor:
	python3 -m jev_browse doctor

# Regenerate docs/config.example.toml and the settings table in docs/configuration.md from jev_browse/config.py.
docs-gen:
	python3 -m jev_browse config --example > docs/config.example.toml
	python3 scripts/gen_config_docs.py
