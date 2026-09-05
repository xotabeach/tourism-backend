# Vendored semgrep rulesets

`p-python.yaml` and `p-owasp-top-ten.yaml` are local copies of the
`p/python` and `p/owasp-top-ten` Semgrep Registry rule packs.

CI used to fetch these from `semgrep.dev` on every `backend-semgrep` run.
That fetch turned out to fail intermittently and, on 2026-09-05, failed
6/6 retries in a row while a plain `curl` to the same registry URL kept
succeeding — a client-side/registry issue on semgrep's end, not a network
outage we could retry past. Vendoring removes the network dependency
(and the flakiness) entirely.

Refresh periodically to pick up new rules:

```sh
curl -sS https://semgrep.dev/c/p/python -o tools/semgrep-rules/p-python.yaml
curl -sS https://semgrep.dev/c/p/owasp-top-ten -o tools/semgrep-rules/p-owasp-top-ten.yaml
```

Then re-run `backend-semgrep`'s scan command locally against the repo to
confirm nothing new fires before committing the refresh.
