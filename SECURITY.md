# Security policy

## Reporting a vulnerability

Report it privately through GitHub. Go to the **Security** tab of
https://github.com/keivanmalhani/gcode-lint and choose **Report a
vulnerability** to open a private advisory. That goes to the maintainer and
stays private until there is a fix.

Please do not open a public issue for a vulnerability.

Include what you did, what happened, and what you expected. Whatever reproduces
it, an input file, a command line, a link, helps more than a description of it.

## Scope

This tool runs on your machine, on your own files, and makes no network
request. In scope is anything that lets an input it reads take control of the
process or reach beyond it:

- a crafted G-code file causing command execution, an unexpected file write, an
  outbound network request, or terminal escape sequences reaching your terminal
- a crafted input that causes an unbounded loop or unbounded allocation instead
  of an error
- writing outside the output path you asked for, or overwriting something you
  did not name
- a finding that quotes attacker controlled text out of the G-code back into
  the output without neutralising control characters
- anything in the release pipeline that could put code the maintainer did not
  write into a published artifact

Out of scope: wrong answers, which are bugs and belong in a normal issue;
missing input validation with no demonstrated impact; and output from an
automated scanner with no working proof.

## What this tool is

A local command line program with no server component and no telemetry. It
reads a sliced G-code file once, reports what it thinks will go wrong, and
writes those findings to stdout. It never modifies the file it read and makes
no network request.

There is no credential involved anywhere in it and nothing for it to leak.

## Supported versions

The most recent tagged release is the supported version. There is no
deployment: the tool runs on your machine from a checkout or an install. Older
tags do not get backported fixes.
