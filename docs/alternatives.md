# Alternatives

Ways to publish a Go module that isn't the plain source of a repository:

| Approach                                     | Commits generated code | Extra infrastructure                          | Plain `go get`         | Notes                                                                                                           |
| -------------------------------------------- | ---------------------- | --------------------------------------------- | ---------------------- | --------------------------------------------------------------------------------------------------------------- |
| Commit the module to the repository          | yes                    | none                                          | yes                    | Simple. Generated code adds noise to history and reviews, and the module path is tied to the repository layout. |
| Release branch in the same repository        | yes, on a side branch  | none                                          | yes                    | No second repository. Go tags are shared with the rest of the repository, so they may need a path prefix.       |
| Separate publishing repository               | yes                    | a second repository, a token with push access | yes                    | Common for generated SDKs. The release job pushes and tags. Two repositories to keep in sync.                   |
| Vanity import pages                          | depends                | a web server or Pages site                    | yes                    | Gives a module path on your domain, but still resolves to a repository with tags.                               |
| Hosted module proxy                          | no                     | a third-party service                         | with `go-import` pages | Upload zips to the service. Availability and retention depend on the provider.                                  |
| Self-hosted proxy server (Athens and others) | no                     | a server to run                               | with `go-import` pages | Full control. Something to operate, patch and pay for.                                                          |
| `GOPROXY=file://…`                           | no                     | none                                          | no                     | Proxy files on disk. Works for local builds and CI, not for other consumers.                                    |
| Static proxy on GitHub Pages (this action)   | no                     | a Pages site                                  | yes                    | No server. Pages limits apply: public site, 1 GB, 100 GB bandwidth per month.                                   |

## Commit the module to the repository

Generate the code and commit it, for example to a `go/` directory with its own `go.mod`, and tag `go/v1.2.3`. Nothing else to run. The downsides: generated code in every diff, tags per module subdirectory, and the module path is `github.com/<owner>/<repo>/go`.

## Release branch in the same repository

A release job generates the module and commits it to a branch that holds only the generated code, often an orphan branch such as `go-release`. Then it tags that commit. Go resolves versions from tags, not from branches, so `go get` finds the code even though it never lands on the default branch. The workflow's own token can push, so you don't need a second repository or extra credentials.

Things to watch:

- **Tags are shared with the whole repository.** A tag like `v1.2.3` may already mark the project's own release on the default branch. In that case, put the module in a subdirectory of the release branch and tag `go/v1.2.3`. The module path then ends in `/go`.
- **The generated code still lives in the repository.** It grows the history and the clone size, even if it stays out of reviews.
- **Branch rules need care.** Rulesets and tag protection must allow the release job to push the branch and the tags.
- **Major versions need a path suffix.** From v2 on, the module path needs a `/vN` suffix, as with any Go module.

This is the simplest option when committing generated code is acceptable but it shouldn't sit on the main branch.

## Separate publishing repository

A release job generates the module, commits it to a second repository and pushes a tag. Consumers see a normal Go repository. The job needs credentials with write access to that repository, and the two repositories have to stay in step.

## Vanity import pages

A page with a `go-import` tag maps a path on your domain to a repository. It changes the module path but not where the code comes from: the code still has to be in a repository with version tags. This action can write such pages too (see [modes](modes.md#vanity)).

## Hosted module proxy

Some services host Go modules for you. You upload zips and point a `go-import` page or `GOPROXY` at the service. No repository is needed, but the modules depend on an outside service and its terms.

## Self-hosted proxy server

Proxy servers such as Athens store and serve modules. They are built for caching and private modules in an organization, and need a server, storage and maintenance.

## `file://` proxy

`GOPROXY` accepts `file://` URLs with the same layout as this site. It's useful for testing or air-gapped builds, but it doesn't publish anything.

## When this action fits

- You build the module in CI and don't want to commit it.
- The module should be public and `go get`-able without consumer configuration.
- You want no infrastructure beyond the repository.

When the module is ordinary source in a repository, plain tags in that repository are simpler.
