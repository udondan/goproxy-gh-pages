#!/usr/bin/env bash
# End to end: packs modules, builds a site, serves it over HTTP and resolves the modules with the
# go command, through GOPROXY and, if port 80 can be used, through the go-import pages.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
python="${PYTHON:-python3}"
tool=("$python" -I "$root/goproxy_gh_pages")

work="$(mktemp -d)"
pids=()
cleanup() {
  for pid in "${pids[@]}"; do
    kill "$pid" 2> /dev/null || sudo -n kill "$pid" 2> /dev/null || true
  done
  chmod -R u+w "$work" 2> /dev/null || true
  rm -rf "$work"
}
trap cleanup EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

pass() {
  echo "ok: $*"
}

# the JSON object the tool prints after its log
outputs() {
  sed -n '/^{/,$p'
}

field() {
  "$python" -c 'import json, sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"
}

write_module() {
  local dir="$1" module="$2" version="$3"
  mkdir -p "$dir/sub"
  printf 'module %s\n\ngo 1.21\n' "$module" > "$dir/go.mod"
  printf 'package %s\n\nconst Version = "%s"\n' "$(basename "$module")" "$version" > "$dir/$(basename "$module").go"
  printf 'package sub\n\nconst Name = "sub"\n' > "$dir/sub/sub.go"
}

wait_for_server() {
  local url="$1"
  for _ in $(seq 1 50); do
    if curl -fsS -o /dev/null "$url" 2> /dev/null; then
      return 0
    fi
    sleep 0.1
  done
  fail "server at $url did not start"
}

export GOPATH="$work/gopath" GOMODCACHE="$work/modcache" GOFLAGS="-mod=mod -modcacherw"
export GOSUMDB=off GOTOOLCHAIN=local GOENV=off

# modules: example.com/hello in two versions, and one below the site URL for the go-import pages
host_module="127.0.0.1/site/hi"
pack_hello() {
  write_module "$work/src/hello-$1" example.com/hello "$1"
  "${tool[@]}" pack "$work/src/hello-$1" "$1" -o "$work/zips/hello-$1.zip" --time 2024-01-02T03:04:05Z \
    | outputs | field h1
}
h1_old="$(pack_hello v1.0.0)"
h1_new="$(pack_hello v1.1.0)"
write_module "$work/src/hi" "$host_module" v0.1.0
"${tool[@]}" pack "$work/src/hi" v0.1.0 -o "$work/zips/hi-v0.1.0.zip" --time 2024-01-02T03:04:05Z > /dev/null

modules="$work/modules.txt"
cat > "$modules" << EOF
example.com/hello
$host_module
127.0.0.1/site/vanity git https://github.com/example/vanity
127.0.0.1/site/external mod https://proxy.example.com
EOF

"${tool[@]}" build \
  --output "$work/www/site" \
  --base-url http://127.0.0.1/site \
  --modules "@$modules" \
  --zip "$work/zips/*.zip" \
  --no-release-assets --no-mirror --fallback-proxy '' --no-verify-sumdb \
  --home-url https://example.com/home > "$work/build.log" || {
  cat "$work/build.log"
  fail "build"
}
pass "build"

port="$("$python" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
"$python" -m http.server "$port" --bind 127.0.0.1 --directory "$work/www" > "$work/server.log" 2>&1 &
pids+=($!)
wait_for_server "http://127.0.0.1:$port/site/go/index.json"

# through GOPROXY
export GOPROXY="http://127.0.0.1:$port/site/go"
mkdir -p "$work/consumer"
cd "$work/consumer"
go mod init consumer > /dev/null 2>&1

download="$(go mod download -json example.com/hello@v1.0.0)"
sum="$(field Sum <<< "$download")"
[[ "$sum" == "$h1_old" ]] || fail "go mod download: Sum $sum, pack reported $h1_old"
pass "go mod download: $sum"

versions="$(go list -m -versions example.com/hello)"
[[ "$versions" == "example.com/hello v1.0.0 v1.1.0" ]] || fail "go list -m -versions: $versions"
pass "go list -m -versions: $versions"

latest="$(go list -m -json example.com/hello@latest | field Version)"
[[ "$latest" == v1.1.0 ]] || fail "@latest is $latest"
pass "@latest: $latest"

cat > main.go << 'EOF'
package main

import (
	"fmt"

	"example.com/hello"
	"example.com/hello/sub"
)

func main() {
	fmt.Println(hello.Version, sub.Name)
}
EOF
go get example.com/hello@latest > /dev/null 2>&1 || fail "go get example.com/hello@latest"
grep -q "example.com/hello v1.1.0" go.mod || fail "go.mod does not require v1.1.0: $(cat go.mod)"
grep -q "example.com/hello v1.1.0 $h1_new" go.sum || fail "go.sum does not have the h1 of pack: $(cat go.sum)"
go build -o consumer . || fail "go build"
[[ "$(./consumer)" == "v1.1.0 sub" ]] || fail "consumer prints $(./consumer)"
pass "go get, go build and run a consumer"
go mod verify > /dev/null || fail "go mod verify"
pass "go mod verify"

# the go-import pages
page() {
  curl -fsS "http://127.0.0.1:$port/site/$1"
}
page hi/ | grep -q "<meta name=\"go-import\" content=\"$host_module mod http://127.0.0.1/site/go\">" \
  || fail "go-import page of $host_module: $(page hi/)"
page hi/sub/ | grep -q "<meta name=\"go-import\" content=\"$host_module mod http://127.0.0.1/site/go\">" \
  || fail "go-import page of $host_module/sub: $(page hi/sub/)"
page vanity/ | grep -q '<meta name="go-import" content="127.0.0.1/site/vanity git https://github.com/example/vanity">' \
  || fail "vanity page: $(page vanity/)"
page vanity/ | grep -q '<meta name="go-source"' || fail "vanity page has no go-source: $(page vanity/)"
page external/ | grep -q '<meta name="go-import" content="127.0.0.1/site/external mod https://proxy.example.com">' \
  || fail "external page: $(page external/)"
pass "go-import pages"

# the go command reads the go-import pages only from the host of the module path, without a port,
# so this needs port 80
server80=()
if "$python" -c 'import socket; socket.socket().bind(("127.0.0.1", 80))' 2> /dev/null; then
  server80=("$python")
elif sudo -n true 2> /dev/null; then
  server80=(sudo -n "$python")
fi
if [[ ${#server80[@]} -eq 0 ]]; then
  echo "skip: resolving through go-import pages needs port 80"
  exit 0
fi
"${server80[@]}" -m http.server 80 --bind 127.0.0.1 --directory "$work/www" > "$work/server80.log" 2>&1 &
pids+=($!)
wait_for_server "http://127.0.0.1/site/hi/?go-get=1"

GOPROXY=direct GOINSECURE=127.0.0.1 GOPRIVATE=127.0.0.1 go mod download -json "$host_module@v0.1.0" > "$work/direct.json" \
  || fail "go mod download through the go-import page: $(cat "$work/direct.json")"
pass "go-import page resolves through mod mode: $(field Version < "$work/direct.json")"
