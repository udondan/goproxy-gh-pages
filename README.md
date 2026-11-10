# goproxy-gh-pages

Publish Go modules from GitHub Pages as a static Go module proxy, without committing the module source to a repository.

## The problem

The go command fetches modules from version control. `go get example.com/mod@v1.2.3` resolves the import path to a repository and expects a tag in it that matches the version. That works well when the module is the source in the repository. It fits badly when the module is a build output:

- code generated at release time (API clients, bindings, protobuf output)
- a module in a subdirectory of a repository that is mostly something else
- a module whose layout or module path differs from the repository

For these cases the module has to be committed somewhere before anyone can `go get` it. Usually that means a second repository that a release job pushes generated code and tags into, or generated code committed next to the source it was generated from.

## The solution

The Go module proxy protocol (`GOPROXY`) is plain HTTP GET requests for static files: a version list, a small JSON file per version, the `go.mod` and a zip. GitHub Pages serves static files. This action builds those files from your module zips and you deploy them with Pages.

To make the plain module path work for consumers, the site also serves `go-import` pages with the `mod` mode, which tell the go command "this module is served by that proxy". As a result:

- Consumers run `go get <module path>` with default settings. proxy.golang.org follows the `go-import` page to your site, fetches the module once and caches it. sum.golang.org records its checksums.
- `GOPROXY=direct` and `GOPRIVATE` setups also work, because the go command reads the same `go-import` page itself.
- Nothing generated is committed. The zips live as release assets, as files in the workflow, or on the site itself.

The action runs on the Python of the runner and uses only the standard library. It installs and compiles nothing.

## Quick start

A module path must start with the host and path of the Pages site. For a repository `octo/widgets` the site is `https://octo.github.io/widgets`, so a module could be `octo.github.io/widgets/client`. The zips are served below `https://octo.github.io/widgets/go`.

1. In the repository settings, set Pages to deploy from **GitHub Actions**.
2. Give the generated module that path in its `go.mod`: `module octo.github.io/widgets/client`.
3. Add a release workflow:

```yaml
name: release
on:
  release:
    types: [published]

permissions: {}

jobs:
  go:
    runs-on: ubuntu-latest
    permissions:
      contents: write # upload the release asset
      pages: write
      id-token: write
    environment:
      name: github-pages
      url: ${{ steps.deploy.outputs.page_url }}
    steps:
      - uses: actions/checkout@v7

      # generate the module into ./dist/go here

      - uses: udondan/goproxy-gh-pages/pack@v1
        with:
          module-dir: dist/go
          version: ${{ github.event.release.tag_name }}
          release-tag: ${{ github.event.release.tag_name }}

      - id: build
        uses: udondan/goproxy-gh-pages@v1

      - uses: actions/upload-pages-artifact@v5
        with:
          path: ${{ steps.build.outputs.path }}

      - id: deploy
        uses: actions/deploy-pages@v5

      - uses: udondan/goproxy-gh-pages/seed@v1
        with:
          versions: ${{ steps.build.outputs.new-versions }}
          site-url: ${{ steps.build.outputs.proxy-url }}
          wait-for: proxy,sumdb
```

After the workflow ran, `go get octo.github.io/widgets/client@v1.0.0` works from anywhere.

What the steps do:

- **pack** writes a reproducible module zip of `dist/go` and uploads it to the release.
- **build** collects all module versions from local zips, release assets and the site that is live now, and writes the site into `_site`.
- **upload-pages-artifact** and **deploy-pages** publish it.
- **seed** waits until the new versions are live on the site, requests them from proxy.golang.org and sum.golang.org, and waits until they are available there. Later steps and jobs can use the new version right away.

## Examples

### Local zips, no releases

Build the zips in the workflow and pass them to the build. The live site keeps the earlier versions, so nothing has to be stored anywhere else.

```yaml
- id: pack
  uses: udondan/goproxy-gh-pages/pack@v1
  with:
    module-dir: dist/go
    version: ${{ github.ref_name }}

- id: build
  uses: udondan/goproxy-gh-pages@v1
  with:
    zips: ${{ steps.pack.outputs.zip }}
    release-assets: ''
```

With `mirror: true` (the default), the build keeps every version the live site serves. It reads the site's `index.json` and copies the zips of earlier versions from the site.

### Rebuild the site without a new version

Running the build with no new zips republishes the current site. Use this after changing settings or adding vanity pages, or to redeploy after Pages was turned off.

### Vanity import pages for a repository

`go-import` pages can also point to a repository, for modules that are normal source in a repository but should have a path on your domain.

```yaml
- uses: udondan/goproxy-gh-pages@v1
  with:
    base-url: https://go.example.com
    modules: |
      go.example.com/tool git https://github.com/octo/tool
      go.example.com/lib git https://github.com/octo/monorepo lib
```

The optional fourth field is the subdirectory of the module in the repository. For repositories on GitHub the pages also get a `go-source` tag, so pkg.go.dev links to the source.

### Module served by another proxy

```yaml
modules: |
  go.example.com/sdk mod https://goproxy.example.net
```

The site serves only the `go-import` page. The go command downloads the module from that proxy.

### Mixed

Each line of `modules` is a module path or path prefix and how it is served. When `modules` lists proxy modules, zips of other modules are ignored.

```yaml
modules: |
  go.example.com/client               # served from this site
  go.example.com/client/v2 proxy      # the same, spelled out
  go.example.com/tool git https://github.com/octo/tool
  go.example.com/sdk mod https://goproxy.example.net
```

### Add the proxy to an existing site

With `merge: true` the build adds its files to whatever is in the output directory already, such as a documentation site built in an earlier step. An existing index page and 404 page are kept.

```yaml
- run: make docs # writes _site
- uses: udondan/goproxy-gh-pages@v1
  with:
    merge: true
```

The go command needs the `go-import` tags in the 404 page to resolve packages that have no page of their own. If your site brings its own 404 page, the build warns about it.

### Custom domain

Without `base-url`, the build asks the Pages API for the URL of the site, which includes a custom domain. Set `base-url` when the token can't read the Pages settings:

```yaml
with:
  base-url: https://go.example.com
```

Module paths then start with `go.example.com/`. See [security](docs/security.md#custom-domains) about verifying the domain.

### Wait for a version in another workflow

The seed action can wait without seeding, for example in a workflow that consumes the module:

```yaml
- uses: udondan/goproxy-gh-pages/seed@v1
  with:
    versions: octo.github.io/widgets/client@v1.4.0
    seed: false
    wait-for: latest
```

## Actions

### `udondan/goproxy-gh-pages@v1` (build)

| Input            | Default                                | Description                                                                                                     |
| ---------------- | -------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `output`         | `_site`                                | Directory to write the site to. Must be empty unless `merge` is set.                                            |
| `base-url`       | URL of the Pages site                  | URL of the site and root of the module paths. Read from the Pages API, else `https://<owner>.github.io/<repo>`. |
| `path`           | `go`                                   | Path of the proxy below the base URL. The GOPROXY URL is `<base-url>/<path>`.                                   |
| `modules`        |                                        | Module lines, see [modes](docs/modes.md). Empty serves every module that is found.                              |
| `zips`           |                                        | Module zips to add, one path or glob per line.                                                                  |
| `repository`     | `${{ github.repository }}`             | Repository to read release assets from.                                                                         |
| `release-assets` | `*.zip`                                | Globs of release asset names that may be module zips, one per line. Empty disables release assets.              |
| `releases`       | `30`                                   | Number of the newest releases to read assets from.                                                              |
| `include-drafts` | `true`                                 | Include assets of draft releases.                                                                               |
| `mirror`         | `true`                                 | Keep the versions the live site serves, also when no other source has them anymore.                             |
| `fallback-proxy` | `https://proxy.golang.org/cached-only` | Proxy to download listed versions from that no other source has. Empty disables it.                             |
| `keep`           | `30`                                   | Newest versions to keep per module. The newest version of each major version is always kept, and so are `zips`. |
| `verify-sumdb`   | `true`                                 | Fail if a version differs from what sum.golang.org recorded.                                                    |
| `merge`          | `false`                                | Keep files that are in the output directory already.                                                            |
| `home-url`       | the repository                         | Where the root page and the links on the pages point to.                                                        |
| `redirect-root`  | `true`                                 | Make the root page a redirect to `home-url` instead of a list of the modules.                                   |
| `not-found-page` | `true`                                 | Write a `404.html` with `go-import` tags for import paths without a page.                                       |
| `max-size`       | `1GiB`                                 | Size limit of the site. The build warns at 80% and fails above it.                                              |
| `parallelism`    | `8`                                    | Parallel downloads.                                                                                             |
| `token`          | `${{ github.token }}`                  | Token to read releases with.                                                                                    |

| Output         | Description                                                                  |
| -------------- | ---------------------------------------------------------------------------- |
| `path`         | Directory of the site.                                                       |
| `base-url`     | URL of the site.                                                             |
| `proxy-url`    | URL of the proxy, for `GOPROXY` and the `site-url` input of the seed action. |
| `modules`      | JSON object of the modules and their versions.                               |
| `new-versions` | JSON list of `module@version` that the live site doesn't serve yet.          |

### `udondan/goproxy-gh-pages/pack@v1`

| Input               | Default                          | Description                                                                                                                              |
| ------------------- | -------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `module-dir`        | required                         | Directory with the `go.mod` of the module.                                                                                               |
| `version`           | required                         | Version, with or without the leading `v`.                                                                                                |
| `output`            | `{name}-go-module-{version}.zip` | Path of the zip. Placeholders: `{name}` (repository name, else the last element of the module path), `{module}`, `{version}`, `{major}`. |
| `time`              | see description                  | Time of the version, RFC 3339 or Unix seconds. Defaults to `SOURCE_DATE_EPOCH`, else the time of the HEAD commit.                        |
| `compression-level` | `9`                              | Deflate level, 0 to 9.                                                                                                                   |
| `release-tag`       |                                  | Upload the zip as an asset of this release. Draft releases work too.                                                                     |
| `asset-name`        | file name of the zip             | Name of the release asset, with the same placeholders.                                                                                   |
| `repository`        | `${{ github.repository }}`       | Repository of the release.                                                                                                               |
| `token`             | `${{ github.token }}`            | Token to upload the asset with. Needs `contents: write`.                                                                                 |

| Output    | Description                                          |
| --------- | ---------------------------------------------------- |
| `zip`     | Path of the zip.                                     |
| `module`  | Module path.                                         |
| `version` | Version.                                             |
| `time`    | Time of the version.                                 |
| `h1`      | Hash of the zip as the checksum database records it. |
| `mod-h1`  | Hash of the `go.mod`.                                |

The same module directory, version and time always give the same zip, byte for byte.

### `udondan/goproxy-gh-pages/seed@v1`

| Input             | Default                    | Description                                                                                   |
| ----------------- | -------------------------- | --------------------------------------------------------------------------------------------- |
| `versions`        | required                   | `module@version` lines, or the `new-versions` output of the build.                            |
| `site-url`        |                            | Proxy URL of the site (`proxy-url` output). Seeding starts once the site serves the versions. |
| `seed`            | `true`                     | Request the versions from the public proxy. `false` only waits.                               |
| `wait-for`        | `proxy`                    | Comma separated targets to wait for (see below), or `none`.                                   |
| `wait-interval`   | `10`                       | Seconds between polls. Polls back off to up to six times this.                                |
| `wait-timeout`    | `900`                      | Seconds to wait in total.                                                                     |
| `fail-on-timeout` | `true`                     | Fail when a target doesn't have every version in time. `false` only warns.                    |
| `proxy`           | `https://proxy.golang.org` | Public module proxy.                                                                          |
| `sumdb`           | `https://sum.golang.org`   | Checksum database.                                                                            |

| Target    | Available when                                                                     |
| --------- | ---------------------------------------------------------------------------------- |
| `proxy`   | proxy.golang.org serves the version. `go get module@v1.2.3` works.                 |
| `latest`  | The version is in the proxy's version list. `go get module@latest` resolves to it. |
| `sumdb`   | sum.golang.org has the hashes, and they match the site.                            |
| `index`   | index.golang.org lists the version.                                                |
| `pkgsite` | pkg.go.dev shows the documentation. This can take many minutes.                    |

| Output      | Description                                                              |
| ----------- | ------------------------------------------------------------------------ |
| `available` | JSON object: target → `module@version` → seconds until it was available. |

## Command line

The tool also runs outside of GitHub Actions, with Python 3.10 or newer and nothing to install:

```sh
git clone https://github.com/udondan/goproxy-gh-pages
python3 goproxy-gh-pages/goproxy_gh_pages pack ./dist/go v1.2.3
python3 goproxy-gh-pages/goproxy_gh_pages build --base-url https://octo.github.io/widgets --zip '*.zip' --no-release-assets
python3 goproxy-gh-pages/goproxy_gh_pages verify widgets-go-module-v1.2.3.zip --sumdb https://sum.golang.org
python3 goproxy-gh-pages/goproxy_gh_pages wait octo.github.io/widgets/client@v1.2.3 --wait-for latest
```

| Command  | Purpose                                                                     |
| -------- | --------------------------------------------------------------------------- |
| `pack`   | Write the module zip of a module directory.                                 |
| `build`  | Write the site.                                                             |
| `seed`   | Request versions from the public proxy and wait for them.                   |
| `wait`   | Only wait.                                                                  |
| `verify` | Check module zips, print their hashes and compare with a checksum database. |

`--help` on each command lists its options. With `GITHUB_TOKEN` set, `build` can read release assets of private repositories.

To try a built site locally:

```sh
python3 -m http.server -d _site 8000
GOPROXY=http://localhost:8000/go GOSUMDB=off go get octo.github.io/widgets/client@v1.2.3
```

## Requirements and limits

- **Python 3.10 or newer** on the runner. GitHub-hosted Linux, macOS and Windows runners have it. On self-hosted runners without it, run `actions/setup-python` first.
- **The Pages site must be public.** The go command and proxy.golang.org fetch without credentials.
- **Pages limits:** a site can be up to 1 GB, and Pages has a soft bandwidth limit of 100 GB per month. Most downloads go to proxy.golang.org, which fetches each version once. `keep` limits how many versions the site holds.
- **Versions never change.** proxy.golang.org caches a version forever and sum.golang.org records its hashes forever. A version that changes after it was fetched breaks for everyone. The build refuses to serve a version whose content differs from the live site or the checksum database. Publish a new version instead.

## More

- [How it works](docs/how-it-works.md)
- [Modes](docs/modes.md)
- [Security](docs/security.md)
- [Alternatives](docs/alternatives.md)

## License

[Apache-2.0](LICENSE)
