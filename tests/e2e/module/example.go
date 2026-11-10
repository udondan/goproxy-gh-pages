// Package example is published by the end-to-end test to the Pages site of this repository.
package example

import "udondan.github.io/goproxy-gh-pages/example/greet"

// Hello returns a greeting.
func Hello(name string) string {
	return greet.Greeting(name)
}
