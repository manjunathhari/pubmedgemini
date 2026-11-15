// Renders the gate's custom pages the way oauth2-proxy v7.15.6 does, for the tests.
//
// It copies three things from oauth2-proxy's pkg/app/pagewriter: the template set
// (html/template with the ToUpper/ToLower FuncMap, sign_in.html then error.html
// parsed with ParseFiles) and the two data structs passed to Execute. Standard
// library only, so it needs no download.
//
// Usage: go run ./tests/gate_render <template dir>  < cases.json  > pages.json
// cases.json: [{"page": "sign_in.html"|"error.html", "data": {...}}, ...]
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"html/template"
	"os"
	"path/filepath"
	"strings"
)

// sign_in_page.go, WriteSignInPage
type signInData struct {
	ProxyPrefix   string
	StatusCode    int
	Title         string
	Redirect      string
	CustomLogin   bool
	SignInMessage template.HTML
	ProviderName  string
	LogoData      template.HTML
	Footer        template.HTML
	Version       string
}

// error_page.go, WriteErrorPage
type errorData struct {
	Title       string
	Message     string
	ProxyPrefix string
	StatusCode  int
	Redirect    string
	RequestID   string
	Footer      template.HTML
	Version     string
}

type testCase struct {
	Page string          `json:"page"`
	Data json.RawMessage `json:"data"`
}

func main() {
	if len(os.Args) != 2 {
		fmt.Fprintln(os.Stderr, "usage: gate_render <template dir>")
		os.Exit(2)
	}
	dir := os.Args[1]
	t := template.New("").Funcs(template.FuncMap{"ToUpper": strings.ToUpper, "ToLower": strings.ToLower})
	for _, name := range []string{"sign_in.html", "error.html"} {
		var err error
		if t, err = t.ParseFiles(filepath.Join(dir, name)); err != nil {
			fmt.Fprintln(os.Stderr, "parse:", err)
			os.Exit(1)
		}
	}
	var cases []testCase
	if err := json.NewDecoder(os.Stdin).Decode(&cases); err != nil {
		fmt.Fprintln(os.Stderr, "cases:", err)
		os.Exit(2)
	}
	out := make([]string, 0, len(cases))
	for _, c := range cases {
		var data any
		switch c.Page {
		case "sign_in.html":
			d := signInData{}
			if err := json.Unmarshal(c.Data, &d); err != nil {
				fmt.Fprintln(os.Stderr, "data:", err)
				os.Exit(2)
			}
			data = d
		case "error.html":
			d := errorData{}
			if err := json.Unmarshal(c.Data, &d); err != nil {
				fmt.Fprintln(os.Stderr, "data:", err)
				os.Exit(2)
			}
			data = d
		default:
			fmt.Fprintln(os.Stderr, "unknown page:", c.Page)
			os.Exit(2)
		}
		var buf bytes.Buffer
		if err := t.ExecuteTemplate(&buf, c.Page, data); err != nil {
			fmt.Fprintln(os.Stderr, "execute:", err)
			os.Exit(1)
		}
		out = append(out, buf.String())
	}
	if err := json.NewEncoder(os.Stdout).Encode(out); err != nil {
		os.Exit(1)
	}
}
