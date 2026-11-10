// Package greet is a package below the module root, which gets its own go-import page.
package greet

// Greeting returns a greeting.
func Greeting(name string) string {
	return "Hello, " + name + "!"
}
