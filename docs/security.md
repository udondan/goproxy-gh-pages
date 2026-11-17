# Security

## Versions are immutable

Go assumes a version never changes. The first time proxy.golang.org fetches a version, it keeps it, and sum.golang.org records its hashes in a public, append-only log. Every consumer's `go.sum` holds the same hashes. If the site later served different bytes for that version, the go command would refuse them with a checksum mismatch.

The build enforces this before anything is deployed:

- A version the live site serves can't be replaced. A local zip or release asset with a different `h1` for it fails the build.
- Two sources with different content for one version fail the build.
- With `verify-sumdb` (on by default), every served version that sum.golang.org knows must match its record.

To fix a broken release, publish a new version, and add a `retract` directive to the `go.mod` of a later version if consumers should avoid the broken one.

## What consumers trust

Consumers don't need to trust the site more than any other module origin. With the default `GOSUMDB`, the go command checks every download against sum.golang.org. Once a version is recorded there, changing the site, the release assets or the repository can't change what consumers get for it.

A version nobody has fetched yet isn't recorded. Seeding right after the deploy closes that gap: it makes sum.golang.org record the version you just built.

Consumers who set `GONOSUMDB`, `GOPRIVATE` or `GOSUMDB=off` for your modules only get the `go.sum` check against their own earlier downloads.

## Token

The build uses the token to list releases and to download release assets. Read access to contents is enough. The pack action needs `contents: write` to upload a release asset. The token is only sent to the GitHub API host. Asset downloads that redirect to storage hosts don't get it.

Job permissions for a release workflow:

```yaml
permissions:
  contents: write # only for pack with release-tag
  pages: write
  id-token: write
```

## Custom domains

A Pages site on a custom domain is only yours while the domain's DNS points to GitHub and the domain is linked to your repository. If the Pages site is removed but the DNS records stay, someone else may be able to claim the domain for their own Pages site and serve modules under your import paths.

- [Verify the domain](https://docs.github.com/en/pages/configuring-a-custom-domain-for-your-github-pages-site/verifying-your-custom-domain-for-github-pages) for your account or organization. Only you can then use it for Pages.
- Remove DNS records before you remove the site.

Already recorded versions are safe either way, because of the checksum database. New import paths and versions are not.

## Keep the site published

proxy.golang.org caches versions, but other consumers fetch from the site: `GOPROXY=direct`, `GOPRIVATE`, mirrors that don't share the cache, and the go command resolving the import path through the `go-import` page. If the site goes away, those builds break even though their `go.sum` entries stay valid. Keep the Pages site published as long as the modules are in use.

With `mirror`, the live site is a source for the next build. A deploy that drops earlier versions, for example from another workflow writing to the same Pages site, removes that source; the next build then needs the release assets or the fallback proxy to restore them.

## Running the action

The action runs Python with `-I` (isolated mode), so files in the checked out repository can't replace its modules and `PYTHON*` variables don't affect it. Inputs are passed as environment variables and never placed into scripts. It has no dependencies to download.
