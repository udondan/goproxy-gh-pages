// Checks the module zips of goproxy-gh-pages against golang.org/x/mod, the code the go command
// uses: the zips must pass zip.CheckZip, have the same files as zip.CreateFromDir, and have the same
// h1 hashes as dirhash, which the checksum database records.
package compat

import (
	stdzip "archive/zip"
	"bytes"
	"encoding/json"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"slices"
	"strings"
	"testing"

	"golang.org/x/mod/module"
	"golang.org/x/mod/sumdb/dirhash"
	"golang.org/x/mod/zip"
)

const versionTime = "2024-01-02T03:04:05Z"

type fixture struct {
	name    string
	module  string
	version string
	files   map[string]string
	// prepare adds what can't be a plain file, like symbolic links
	prepare func(t *testing.T, dir string)
	// invalid: the files are not a valid module, both implementations must refuse it
	invalid bool
	// needsCaseSensitive: the fixture needs a file system that tells A and a apart
	needsCaseSensitive bool
}

var fixtures = []fixture{
	{
		name:    "basic",
		module:  "example.com/basic",
		version: "v1.2.3",
		files: map[string]string{
			"basic.go":        "package basic\n",
			"basic_test.go":   "package basic\n",
			"LICENSE":         "license text\n",
			"README.md":       "# basic\n",
			"sub/sub.go":      "package sub\n",
			"sub/deep/x.go":   "package deep\n",
			"sub/data/empty":  "",
			"testdata/in.txt": "input\n",
			".gitignore":      "*.out\n",
		},
	},
	{
		name:    "major version",
		module:  "example.com/major/v2",
		version: "v2.1.0",
		files:   map[string]string{"major.go": "package major\n"},
	},
	{
		name:    "prerelease and build-free version",
		module:  "example.com/pre",
		version: "v0.1.0-rc.1",
		files:   map[string]string{"pre.go": "package pre\n"},
	},
	{
		name:    "vendor directories",
		module:  "example.com/vendored",
		version: "v1.0.0",
		files: map[string]string{
			"v.go":                        "package v\n",
			"vendor/modules.txt":          "# example.com/dep v1.0.0\n",
			"vendor/example.com/dep/d.go": "package dep\n",
			"sub/vendor/x/x.go":           "package x\n",
			"sub/vendor/top.go":           "package vendor\n",
			"notvendor/vendorish/file.go": "package vendorish\n",
			"vendorx/kept.go":             "package vendorx\n",
			"sub/vendor.go":               "package sub\n",
		},
	},
	{
		name:    "nested modules",
		module:  "example.com/outer",
		version: "v1.0.0",
		files: map[string]string{
			"outer.go":            "package outer\n",
			"inner/go.mod":        "module example.com/outer/inner\n",
			"inner/inner.go":      "package inner\n",
			"inner/deep/deep.go":  "package deep\n",
			"other/sub/go.mod":    "module example.com/outer/other/sub\n",
			"other/sub/x.go":      "package sub\n",
			"other/kept.go":       "package other\n",
			"tools/go.mod/README": "a directory named go.mod is not a module\n",
			"tools/tools.go":      "package tools\n",
		},
	},
	{
		name:    "version control directories",
		module:  "example.com/vcs",
		version: "v1.0.0",
		files: map[string]string{
			"vcs.go":               "package vcs\n",
			".git/HEAD":            "ref: refs/heads/main\n",
			".git/config":          "[core]\n",
			".hg/store":            "x\n",
			"sub/.svn/entries":     "x\n",
			"sub/.bzr/branch":      "x\n",
			"sub/sub.go":           "package sub\n",
			".hg_archival.txt":     "repo: x\n",
			".github/workflow.yml": "on: push\n",
		},
	},
	{
		name:    "punctuation and unicode in file names",
		module:  "example.com/names",
		version: "v1.0.0",
		files: map[string]string{
			"names.go":          "package names\n",
			"a b.txt":           "space\n",
			"x!y.txt":           "bang\n",
			"Mixed/Case.go":     "package Mixed\n",
			"file+name~1.txt":   "plus tilde\n",
			"@at.txt":           "at\n",
			"(paren)[b]{c}.txt": "brackets\n",
			"ü/ö.txt":           "umlaut\n",
			"#hash%pct&amp.txt": "punct\n",
			"=eq^caret,comma":   "more\n",
			"UPPER.TXT":         "upper\n",
		},
	},
	{
		name:    "uppercase module path",
		module:  "github.com/Owner/Repo",
		version: "v1.0.0",
		files:   map[string]string{"repo.go": "package repo\n"},
	},
	{
		name:    "no go files",
		module:  "example.com/empty",
		version: "v1.0.0",
		files:   map[string]string{},
	},
	{
		name:    "large file",
		module:  "example.com/large",
		version: "v1.0.0",
		files:   map[string]string{"large.go": "package large\n", "blob.bin": strings.Repeat("0123456789abcdef", 1<<16)},
	},
	{
		name:    "symbolic link",
		module:  "example.com/links",
		version: "v1.0.0",
		files:   map[string]string{"links.go": "package links\n"},
		prepare: func(t *testing.T, dir string) {
			if runtime.GOOS == "windows" {
				t.Skip("symbolic links need privileges on Windows")
			}
			if err := os.Symlink("links.go", filepath.Join(dir, "link.go")); err != nil {
				t.Fatal(err)
			}
			if err := os.Symlink(".", filepath.Join(dir, "loop")); err != nil {
				t.Fatal(err)
			}
		},
	},
	{
		name:               "case collision",
		module:             "example.com/collision",
		version:            "v1.0.0",
		files:              map[string]string{"c.go": "package c\n", "file.txt": "a\n", "FILE.txt": "b\n"},
		invalid:            true,
		needsCaseSensitive: true,
	},
	{
		// Go folds σ and ς together, a plain lowercase does not
		name:               "final sigma collision",
		module:             "example.com/sigma",
		version:            "v1.0.0",
		files:              map[string]string{"s.go": "package s\n", "σ.txt": "a\n", "ς.txt": "b\n"},
		invalid:            true,
		needsCaseSensitive: true,
	},
	{
		// from go 1.24 on, vendor/modules.txt is left out and nested vendor dirs are matched correctly
		name:    "vendor directories with go 1.24",
		module:  "example.com/vendored124",
		version: "v1.0.0",
		files: map[string]string{
			"go.mod":                      "module example.com/vendored124\n\ngo 1.24\n",
			"v.go":                        "package v\n",
			"vendor/modules.txt":          "# example.com/dep v1.0.0\n",
			"vendor/example.com/dep/d.go": "package dep\n",
			"pkg/vendor/vendor.go":        "package vendor\n",
			"pkg/vendor/foo/foo.go":       "package foo\n",
			"sub/vendor/x/x.go":           "package x\n",
		},
	},
	{
		name:    "reserved windows name",
		module:  "example.com/reserved",
		version: "v1.0.0",
		files:   map[string]string{"r.go": "package r\n", "con.txt": "x\n"},
		invalid: true,
	},
	{
		name:    "invalid character",
		module:  "example.com/quote",
		version: "v1.0.0",
		files:   map[string]string{"q.go": "package q\n", "it's.txt": "x\n"},
		invalid: true,
	},
	{
		// a go.mod in any case makes a directory another module
		name:    "uppercase go.mod in a subdirectory",
		module:  "example.com/gomodcase",
		version: "v1.0.0",
		files:   map[string]string{"g.go": "package g\n", "sub/GO.MOD": "module x\n", "sub/s.go": "package s\n"},
	},
	{
		name:               "uppercase go.mod next to go.mod",
		module:             "example.com/gomodroot",
		version:            "v1.0.0",
		files:              map[string]string{"g.go": "package g\n", "Go.Mod": "module x\n"},
		invalid:            true,
		needsCaseSensitive: true,
	},
}

// relative makes the paths of CheckDir and CheckZip relative to the module root
func relative(paths []string, prefix string) []string {
	result := make([]string, 0, len(paths))
	for _, p := range paths {
		result = append(result, filepath.ToSlash(strings.TrimPrefix(p, prefix)))
	}
	slices.Sort(result)
	return result
}

func repoRoot(t *testing.T) string {
	t.Helper()
	root, err := filepath.Abs(filepath.Join("..", ".."))
	if err != nil {
		t.Fatal(err)
	}
	return root
}

func python() string {
	if p := os.Getenv("PYTHON"); p != "" {
		return p
	}
	if runtime.GOOS == "windows" {
		return "python"
	}
	return "python3"
}

// tool runs goproxy-gh-pages with the given arguments and returns its standard output
func tool(t *testing.T, args ...string) (string, error) {
	t.Helper()
	cmd := exec.Command(python(), append([]string{"-I", filepath.Join(repoRoot(t), "goproxy_gh_pages")}, args...)...)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	cmd.Env = append(os.Environ(), "SOURCE_DATE_EPOCH=", "GITHUB_ACTIONS=")
	err := cmd.Run()
	if err != nil {
		t.Logf("%v: %v\n%s", args, err, stderr.String())
	}
	return stdout.String(), err
}

type packed struct {
	Zip     string `json:"zip"`
	Module  string `json:"module"`
	Version string `json:"version"`
	Time    string `json:"time"`
	H1      string `json:"h1"`
	ModH1   string `json:"mod-h1"`
}

func pack(t *testing.T, dir, version, output string) (packed, error) {
	t.Helper()
	out, err := tool(t, "pack", dir, version, "-o", output, "--time", versionTime)
	if err != nil {
		return packed{}, err
	}
	// the outputs are the JSON object at the end, after the log
	start := strings.LastIndex(out, "\n{")
	var result packed
	if err := json.Unmarshal([]byte(out[start+1:]), &result); err != nil {
		t.Fatalf("pack output %q: %v", out, err)
	}
	return result, nil
}

var verifyLine = regexp.MustCompile(`^(.+): (\S+)@(\S+) (\S+|unknown time) (h1:\S+) go\.mod (h1:\S+)$`)

type verified struct {
	module, version, time, h1, modH1 string
}

// verify reads a zip with goproxy-gh-pages, which checks it like a downloaded zip
func verify(t *testing.T, path string) verified {
	t.Helper()
	out, err := tool(t, "verify", path)
	if err != nil {
		t.Fatalf("verify %s: %v", path, err)
	}
	match := verifyLine.FindStringSubmatch(strings.TrimSpace(out))
	if match == nil {
		t.Fatalf("unexpected verify output %q", out)
	}
	return verified{match[2], match[3], match[4], match[5], match[6]}
}

func goModHash(data []byte) (string, error) {
	return dirhash.Hash1([]string{"go.mod"}, func(string) (io.ReadCloser, error) {
		return io.NopCloser(bytes.NewReader(data)), nil
	})
}

func caseSensitive(t *testing.T) bool {
	t.Helper()
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "a"), nil, 0o644); err != nil {
		t.Fatal(err)
	}
	_, err := os.Stat(filepath.Join(dir, "A"))
	return err != nil
}

func writeFixture(t *testing.T, f fixture) string {
	t.Helper()
	dir := filepath.Join(t.TempDir(), "module")
	files := map[string]string{"go.mod": "module " + f.module + "\n\ngo 1.21\n"}
	for name, content := range f.files {
		files[name] = content
	}
	for name, content := range files {
		path := filepath.Join(dir, filepath.FromSlash(name))
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	if f.prepare != nil {
		f.prepare(t, dir)
	}
	return dir
}

func TestZipsMatchXMod(t *testing.T) {
	for _, f := range fixtures {
		t.Run(f.name, func(t *testing.T) {
			if f.needsCaseSensitive && !caseSensitive(t) {
				t.Skip("the file system is not case-sensitive")
			}
			dir := writeFixture(t, f)
			out := t.TempDir()
			mv := module.Version{Path: f.module, Version: f.version}

			reference := filepath.Join(out, "reference.zip")
			referenceFile, err := os.Create(reference)
			if err != nil {
				t.Fatal(err)
			}
			referenceErr := zip.CreateFromDir(referenceFile, mv, dir)
			if err := referenceFile.Close(); err != nil {
				t.Fatal(err)
			}

			ours := filepath.Join(out, "ours.zip")
			result, oursErr := pack(t, dir, f.version, ours)

			if f.invalid {
				if referenceErr == nil {
					t.Fatalf("x/mod accepts the invalid fixture")
				}
				if oursErr == nil {
					t.Fatalf("pack accepts files x/mod refuses: %v", referenceErr)
				}
				return
			}
			if referenceErr != nil {
				t.Fatalf("x/mod: %v", referenceErr)
			}
			if oursErr != nil {
				t.Fatalf("pack failed")
			}
			if result.Module != f.module || result.Version != f.version || result.Time != versionTime {
				t.Errorf("pack reports %s@%s %s", result.Module, result.Version, result.Time)
			}

			checked, err := zip.CheckZip(mv, ours)
			if err != nil {
				t.Fatalf("CheckZip: %v (invalid %v, omitted %v)", err, checked.Invalid, checked.Omitted)
			}
			if len(checked.Omitted) > 0 || len(checked.Invalid) > 0 {
				t.Errorf("CheckZip: omitted %v, invalid %v", checked.Omitted, checked.Invalid)
			}
			want, err := zip.CheckDir(dir)
			if err != nil {
				t.Fatalf("CheckDir: %v", err)
			}
			got := relative(checked.Valid, f.module+"@"+f.version+"/")
			if expected := relative(want.Valid, dir+string(filepath.Separator)); !slices.Equal(got, expected) {
				t.Errorf("files differ from x/mod\n got: %q\nwant: %q", got, expected)
			}

			referenceH1, err := dirhash.HashZip(reference, dirhash.Hash1)
			if err != nil {
				t.Fatal(err)
			}
			oursH1, err := dirhash.HashZip(ours, dirhash.Hash1)
			if err != nil {
				t.Fatal(err)
			}
			if result.H1 != referenceH1 || oursH1 != referenceH1 {
				t.Errorf("h1: pack reports %s, dirhash of our zip is %s, of the x/mod zip %s", result.H1, oursH1, referenceH1)
			}
			goMod, err := os.ReadFile(filepath.Join(dir, "go.mod"))
			if err != nil {
				t.Fatal(err)
			}
			modH1, err := goModHash(goMod)
			if err != nil {
				t.Fatal(err)
			}
			if result.ModH1 != modH1 {
				t.Errorf("go.mod h1: pack reports %s, dirhash %s", result.ModH1, modH1)
			}

			// zips of x/mod are read with the same hashes
			if v := verify(t, reference); v.h1 != referenceH1 || v.modH1 != modH1 || v.module != f.module || v.version != f.version {
				t.Errorf("verify of the x/mod zip: %+v", v)
			}

			// the same files and time make the same zip
			again, err := pack(t, dir, f.version, filepath.Join(out, "again.zip"))
			if err != nil {
				t.Fatal(err)
			}
			first, _ := os.ReadFile(ours)
			second, _ := os.ReadFile(again.Zip)
			if !bytes.Equal(first, second) {
				t.Errorf("packing twice made different zips")
			}
		})
	}
}

// Zips that a simple script made before: no archive comment, other entry order, directory
// entries. They must keep working, with the hashes the go command computes.
func TestLegacyZips(t *testing.T) {
	legacy, err := filepath.Glob(filepath.Join("testdata", "legacy", "*.zip"))
	if err != nil {
		t.Fatal(err)
	}
	if len(legacy) == 0 {
		t.Fatal("no legacy zips in testdata/legacy")
	}
	for _, path := range legacy {
		t.Run(filepath.Base(path), func(t *testing.T) {
			v := verify(t, path)
			checked, err := zip.CheckZip(module.Version{Path: v.module, Version: v.version}, path)
			if err != nil {
				t.Fatalf("CheckZip: %v", err)
			}
			if len(checked.Valid) == 0 {
				t.Fatal("no files")
			}
			h1, err := dirhash.HashZip(path, dirhash.Hash1)
			if err != nil {
				t.Fatal(err)
			}
			if v.h1 != h1 {
				t.Errorf("h1 is %s, dirhash %s", v.h1, h1)
			}
			// zip times have a resolution of two seconds: 07:08:09 is stored as 07:08:08
			if v.time != "2023-05-06T07:08:08Z" {
				t.Errorf("time %s, expected the time of go.mod in the zip", v.time)
			}
		})
	}
}

// Zip tools like Info-ZIP store UTF-8 names without the UTF-8 flag. Go reads the names as UTF-8
// either way, so the zip must be read with the same names and hash.
func TestUnflaggedUTF8Names(t *testing.T) {
	const prefix = "example.com/utf8@v1.0.0/"
	entries := []zipEntry{
		{&stdzip.FileHeader{Name: prefix + "go.mod", NonUTF8: true}, "module example.com/utf8\n"},
		{&stdzip.FileHeader{Name: prefix + "é.go", NonUTF8: true}, "package utf8\n"},
		{&stdzip.FileHeader{Name: prefix + "dir/ñ/x.go", NonUTF8: true}, "package ñ\n"},
	}
	checkLikeGo(t, writeZip(t, entries, nil), module.Version{Path: "example.com/utf8", Version: "v1.0.0"}, 3)
}

// Go accepts a directory entry that is in a zip more than once, and dirhash hashes each of them
func TestRepeatedDirectoryEntries(t *testing.T) {
	const prefix = "example.com/dirs@v1.0.0/"
	entries := []zipEntry{
		{&stdzip.FileHeader{Name: prefix + "go.mod"}, "module example.com/dirs\n"},
		{&stdzip.FileHeader{Name: prefix + "pkg/"}, ""},
		{&stdzip.FileHeader{Name: prefix + "pkg/"}, ""},
		{&stdzip.FileHeader{Name: prefix + "pkg/p.go"}, "package pkg\n"},
	}
	checkLikeGo(t, writeZip(t, entries, nil), module.Version{Path: "example.com/dirs", Version: "v1.0.0"}, 2)
}

// archive/zip reads only stored and deflated files, so a zip with other methods must be rejected
func TestUnsupportedCompression(t *testing.T) {
	const prefix = "example.com/bzip2@v1.0.0/"
	const bzip2 = 12
	entries := []zipEntry{
		{&stdzip.FileHeader{Name: prefix + "go.mod"}, "module example.com/bzip2\n"},
		{&stdzip.FileHeader{Name: prefix + "b.go", Method: bzip2}, "package bzip2\n"},
	}
	path := writeZip(t, entries, func(writer *stdzip.Writer) {
		// the content doesn't matter: Go has no bzip2 decompressor for zips
		writer.RegisterCompressor(bzip2, func(w io.Writer) (io.WriteCloser, error) { return nopCloser{w}, nil })
	})
	reader, err := stdzip.OpenReader(path)
	if err != nil {
		t.Fatal(err)
	}
	defer reader.Close()
	if _, err := reader.File[1].Open(); err != stdzip.ErrAlgorithm {
		t.Fatalf("Go opened the bzip2 file: %v", err)
	}
	if out, err := tool(t, "verify", path); err == nil {
		t.Errorf("verify accepted a zip Go can't read: %s", out)
	}
}

type zipEntry struct {
	header  *stdzip.FileHeader
	content string
}

type nopCloser struct{ io.Writer }

func (nopCloser) Close() error { return nil }

// writeZip writes a zip with archive/zip; entries without a method are deflated
func writeZip(t *testing.T, entries []zipEntry, configure func(*stdzip.Writer)) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "m.zip")
	file, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	writer := stdzip.NewWriter(file)
	if configure != nil {
		configure(writer)
	}
	for _, entry := range entries {
		if entry.header.Method == stdzip.Store && !strings.HasSuffix(entry.header.Name, "/") {
			entry.header.Method = stdzip.Deflate
		}
		w, err := writer.CreateHeader(entry.header)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := io.WriteString(w, entry.content); err != nil {
			t.Fatal(err)
		}
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	if err := file.Close(); err != nil {
		t.Fatal(err)
	}
	return path
}

// checkLikeGo checks that Go accepts a zip and that goproxy-gh-pages reads it with the same hash
func checkLikeGo(t *testing.T, path string, mv module.Version, files int) {
	t.Helper()
	checked, err := zip.CheckZip(mv, path)
	if err != nil || len(checked.Valid) != files {
		t.Fatalf("CheckZip: %v (valid %v, invalid %v)", err, checked.Valid, checked.Invalid)
	}
	h1, err := dirhash.HashZip(path, dirhash.Hash1)
	if err != nil {
		t.Fatal(err)
	}
	if v := verify(t, path); v.h1 != h1 || v.module != mv.Path || v.version != mv.Version {
		t.Errorf("verify: %+v, dirhash %s", v, h1)
	}
}
