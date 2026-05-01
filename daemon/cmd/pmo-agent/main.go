// pmo-agent is the daemon that watches local AI coding agent transcripts
// and uploads completed turns to a pmo_agent backend.
//
// Subcommands (filled in across Milestone 1.x):
//   pmo-agent login     store PAT + server URL in ~/.pmo-agent/config.toml
//   pmo-agent start     watch transcripts, upload turns (foreground)
//   pmo-agent status    show watched files and recent uploads
//   pmo-agent update    update the local binary from the latest release
//
// Spec: docs/specs/2026-04-29-mvp-design.md §4.

package main

import (
	"flag"
	"fmt"
	"os"
)

const usage = `pmo-agent — public timeline of your local AI coding sessions

Usage:
  pmo-agent <command> [flags]

Commands:
  login      Browser-based authorization; writes ~/.pmo-agent/config.toml
  start      Watch transcripts and upload turns (foreground)
  status     Show watched files, recent uploads, and service status
  install    Install as a background service (macOS launchd)
  uninstall  Remove the background service
  update     Check for and install the latest pmo-agent release
  version    Print version info

Run "pmo-agent <command> -h" for command-specific flags.
`

// version is overridden at build time via -ldflags "-X main.version=...".
var version = "dev"

func main() {
	flag.Usage = func() { fmt.Fprint(os.Stderr, usage) }
	flag.Parse()

	if flag.NArg() == 0 {
		fmt.Fprint(os.Stderr, usage)
		os.Exit(2)
	}

	cmd, args := flag.Arg(0), flag.Args()[1:]
	switch cmd {
	case "login":
		exit(runLogin(args))
	case "start":
		exit(runStart(args))
	case "status":
		exit(runStatus(args))
	case "install":
		exit(runInstall(args))
	case "uninstall":
		exit(runUninstall(args))
	case "update":
		exit(runUpdate(args))
	case "version":
		fmt.Println("pmo-agent", version)
	case "help", "-h", "--help":
		fmt.Print(usage)
	default:
		fmt.Fprintf(os.Stderr, "pmo-agent: unknown command %q\n\n%s", cmd, usage)
		os.Exit(2)
	}
}

func exit(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, "pmo-agent:", err)
		os.Exit(1)
	}
}

// Subcommands are implemented in sibling files: login.go, start.go, status.go.
