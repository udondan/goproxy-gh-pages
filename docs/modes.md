# Modes

The `modules` input says how each module or import path prefix is served. One line per entry. `#` starts a comment.

| Line                                   | Mode     | The site serves                                                     |
| -------------------------------------- | -------- | ------------------------------------------------------------------- |
| `<module>` or `<module> proxy`         | proxy    | the module from its own proxy, and `go-import` pages pointing to it |
| `<prefix> <vcs> <repo-url> [<subdir>]` | vanity   | `go-import` pages pointing to a repository                          |
| `<prefix> mod <proxy-url>`             | external | `go-import` pages pointing to another proxy                         |

`<vcs>` is one of `git`, `hg`, `svn`, `bzr`, `fossil`.

Without `modules`, every module found in the sources is served in proxy mode. With proxy lines, only modules covered by them are served: a line covers its own path and every path below it.

## Proxy

The default. The site holds the zips and the protocol files, and `go-import` pages with `mod <proxy-url>` for the module and each of its packages.

The proxy itself works with any module path. Consumers can always use it explicitly with `GOPROXY=<proxy-url>,https://proxy.golang.org,direct`. The `go-import` pages, and with them the plain `go get`, need the module path to be under the host and path of the base URL. The build warns about modules that aren't.

## Vanity

```text
go.example.com/tool git https://github.com/octo/tool
go.example.com/lib git https://github.com/octo/monorepo lib
```

The go command clones the repository and uses the version tags in it. The fourth field is the subdirectory of the module in the repository (Go 1.25 and newer); without it, the module is at the root. Zips found for a module under a vanity prefix are ignored with a warning.

For repositories on github.com the pages also get a `go-source` tag with links to the source.

## External

```text
go.example.com/sdk mod https://goproxy.example.net
```

The site only redirects the import path to another proxy, which must serve the module. Use this to give modules on a hosted proxy a path on your domain.

## Mixing modes

All modes can be combined on one site. When prefixes overlap, the longest one wins for each page, the same way the go command picks the module with the longest matching path.

Modules served by this site can be nested under any prefix, such as a generated module in a subdirectory of a repository with a vanity import. A vanity or external prefix can't be nested under another vanity or external prefix: the paths under both have no pages of their own and resolve only through their tags in the 404 page, which GitHub Pages has only one of, and the go command fails if two tags match an import path. The build rejects that.

```text
go.example.com/client
go.example.com/client/v2
go.example.com/tool git https://github.com/octo/tool
go.example.com/sdk mod https://goproxy.example.net
```
