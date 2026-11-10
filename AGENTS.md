# Contributing

## Layout

| Path                    | Content                                                                         |
| ----------------------- | ------------------------------------------------------------------------------- |
| `action.yml`            | build action                                                                    |
| `pack/action.yml`       | pack action                                                                     |
| `seed/action.yml`       | seed action                                                                     |
| `run.sh`                | finds Python 3.10+ on the runner and runs the package with `-I`                 |
| `goproxy_gh_pages/`     | the tool, run as `python3 goproxy_gh_pages <command>`                           |
| `├ module.py`           | module paths, versions, escaping, `go.mod` parsing (port of `golang.org/x/mod`) |
| `├ modzip.py`           | module zip rules, creation, reading and `h1` hashes                             |
| `├ net.py`, `github.py` | HTTP client with retries, GitHub API and Actions helpers                        |
| `├ sources.py`          | local zips, live site, release assets, fallback proxy                           |
| `├ site.py`             | writes the proxy files, `go-import` pages and the manifest                      |
| `├ build.py`            | the build: collect, select, fetch, verify, write                                |
| `├ seed.py`             | seeding and waiting for the public proxy, checksum database, index, pkg.go.dev  |
| `├ pack.py`, `cli.py`   | pack command and command line                                                   |
| `tests/unit/`           | pytest tests, with a fake HTTP server in `conftest.py`                          |
| `tests/compat/`         | checks against the go command and `golang.org/x/mod`                            |
| `docs/`                 | documentation                                                                   |

## Rules

- **Standard library only.** The actions run on the runner's Python with nothing installed. Don't add runtime dependencies. Dev tools (pytest, ruff, mypy, coverage) are in the `dev` group of `pyproject.toml`.
- **Python 3.10** is the oldest supported version. Don't use newer syntax or modules.
- **Types:** `mypy --strict` must pass.
- **Inputs** reach the code as `INPUT_<NAME>` environment variables, set in the `env` of each action step. Never put `${{ }}` expressions into `run` scripts.
- **Compatibility with the go command** comes first. Zip rules and hashes follow `golang.org/x/mod`. When changing them, extend `tests/compat`.
- Docs describe the problem and the action in general terms, without references to specific projects.

## Development

[uv](https://docs.astral.sh/uv/) runs the dev tools, [mise](https://mise.jdx.dev/) installs the others.

```sh
make lint    # ruff, mypy, actionlint, zizmor, prettier
make test    # unit tests with coverage
make compat  # needs Go
make bench   # build benchmark with many large versions
```

## Commits

Conventional commits. `fix:` is for bugs in the actions or the tool, `feat:` for new behavior, `chore:` for tooling, CI and dev dependencies, `docs:` for documentation. release-please creates the releases and moves the `v1` tag.
