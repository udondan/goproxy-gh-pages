# How it works

## The files of a Go module proxy

A Go module proxy answers GET requests below its URL ([reference](https://go.dev/ref/mod#goproxy-protocol)). For a module `example.com/mod`, the site has:

| File                                | Content                           |
| ----------------------------------- | --------------------------------- |
| `go/example.com/mod/@v/list`        | the versions, one per line        |
| `go/example.com/mod/@v/v1.2.3.info` | `{"Version":"v1.2.3","Time":"…"}` |
| `go/example.com/mod/@v/v1.2.3.mod`  | the `go.mod` of the version       |
| `go/example.com/mod/@v/v1.2.3.zip`  | the module zip                    |
| `go/example.com/mod/@latest`        | the `.info` of the newest version |

`@latest` is the highest release, else the highest tagged prerelease. A module with only pseudo-versions gets the one of the newest commit, which is the version the go command picks from the list.

Upper case letters in module paths and versions are written as `!` and the lower case letter, as the protocol requires (`Example.com` becomes `!example.com`), so the files work on case-insensitive file systems.

## go-import pages

To find the source of an import path that has no proxy configured for it, the go command requests `https://<import path>?go-get=1` and reads the `go-import` meta tag of the page. With the `mod` mode, the tag names a module proxy:

```html
<meta
  name="go-import"
  content="octo.github.io/widgets/client mod https://octo.github.io/widgets/go"
/>
```

The build writes such a page (`index.html`) for each module and for each package directory in it, since an import path can name any package. proxy.golang.org follows the same tags, so with the default `GOPROXY` the module is fetched from the site once and then served from proxy.golang.org.

GitHub Pages answers paths without a file with `404.html`. The go command reads meta tags of 404 responses too, so the build writes a `404.html` with a tag for every module. This covers import paths the build doesn't know, such as packages of a repository behind a vanity import. The go command fails if two tags match an import path. Paths under a vanity or external prefix need its tag alone, so the tag of a module that has one of them nested under it is left out of the 404 page; every package of a module served by the site has its own page. A path without a page under a module served by the site matches two tags then, which the go command treats as a module that doesn't exist. A vanity or external prefix under another one can't work this way, so the build rejects it.

## The manifest

`go/index.json` lists every module and version with its time, `h1` hashes, `go.mod` hash, SHA-256 and size of the zip, the id of the release asset it came from, and whether sum.golang.org has confirmed it. The next build reads it to decide what to do without probing every source.

## Sources and incremental builds

The build collects versions from four sources. When several have a version, it takes the first one that delivers a valid zip, in this order:

1. **Local zips** (`zips`), read from disk.
2. **The live site** (`mirror`): the versions in the manifest, downloaded from the site over the Pages CDN, which needs no token and has no API rate limit.
3. **Release assets** (`release-assets`) of the newest `releases` releases, including drafts when the token can see them.
4. **The fallback proxy** (`fallback-proxy`): versions it lists that no other source has. The default `cached-only` endpoint of proxy.golang.org only returns what it has cached, and never fetches from the origin.

Only versions that end up on the site are downloaded, each once, in parallel. Retention (`keep`) is decided before anything is downloaded: the newest `keep` versions of each module, plus the newest version of each major version, plus all local zips.

### Identifying release assets without downloading them

Release asset names don't say which module and version a zip holds, so the build reads it from the zip. The entries of a module zip start with `<module>@<version>/`. The build fetches the last 64 KiB of the asset with an HTTP range request, finds the zip's central directory and reads the first entry name. If the central directory starts before that, one more range request reads it. Asset ids already in the manifest are not read again.

Any asset name works, as long as the content is a module zip. Assets that aren't module zips are skipped.

### Skipping work

A zip whose SHA-256 matches the manifest is not hashed again: its `h1` comes from the manifest. Zips are hard-linked into the site when the work directory is on the same file system. A version the manifest marks as confirmed by sum.golang.org is not checked again.

## Module zips

`pack` creates zips the way the go command does (`golang.org/x/mod/zip`):

- Files of nested modules (directories with their own `go.mod`), vendored packages (directories below `vendor`), version control directories, symbolic links and irregular files are left out.
- File paths must be valid module file paths. Paths that differ only in case are an error. A zip and its content can be up to 500 MiB, `go.mod` and `LICENSE` up to 16 MiB.

Zips are reproducible: the entries are sorted, all have mode 0644 and the same time. The time is the `time` input, else `SOURCE_DATE_EPOCH`, else the time of the HEAD commit. The same directory, version and time give the same bytes, so packing again in a rerun of a workflow uploads an identical asset.

The archive comment of the zip holds `{"Version":"…","Time":"…"}`, so the zip alone has all the data for the `.info` file. For zips without the comment, the time is the modification time of the `go.mod` entry.

## Hashes

The go command and sum.golang.org identify a module version by two hashes, both in the `h1:` format of `golang.org/x/mod/sumdb/dirhash`: one of the files in the zip and one of the `go.mod`. They don't depend on compression or file order, only on file names and contents. The build computes both and, with `verify-sumdb`, compares them with sum.golang.org for every version it already knows. Before the lookup, it asks the `cached-only` endpoint of proxy.golang.org whether the version is known, so the check never makes the proxy or the checksum database fetch a version.

The build fails before anything is deployed when:

- a local zip or release asset has a different hash than the version on the live site
- two sources have different content for the same version
- sum.golang.org has different hashes than the site would serve

## Seeding and waiting

proxy.golang.org fetches a version the first time someone asks for it. The seed action asks right after the deploy, so consumers don't wait for that first fetch, and sum.golang.org, index.golang.org and pkg.go.dev learn about the version.

1. With `site-url`, the action first waits until the site serves the `.info` file of each version. Pages takes a moment after the deploy, and a request that reaches proxy.golang.org too early makes it cache "not found" for a while.
2. It requests the `.info`, `.mod` and `.zip` files from proxy.golang.org and looks up the version on sum.golang.org.
3. It polls each `wait-for` target until it has every version, or until `wait-timeout`.

| Target    | Polls                                                                                |
| --------- | ------------------------------------------------------------------------------------ |
| `proxy`   | `@v/<version>.info` on the proxy                                                     |
| `latest`  | `@v/list` on the proxy, which is cached and lags behind `.info`                      |
| `sumdb`   | `lookup/<module>@<version>` on the checksum database, compared with the manifest     |
| `index`   | `index.golang.org/index?since=…`, starting 15 minutes back, reading only new entries |
| `pkgsite` | `pkg.go.dev/<module>@<version>`, after one fetch request to pkg.go.dev               |

Polls back off by a factor of 1.5 up to six times `wait-interval`, with jitter. The step summary shows how long each target took.
