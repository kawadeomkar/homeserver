# When a leak check fires

gitleaks runs over the staged changes, before a push and in CI's `leaks` job (`.gitleaks.toml`).

A pushed commit is published, even on a pull-request branch. Do not merge, and close the pull request if the finding
is real. Rotate a leaked secret at once: the TrueNAS API key through the role and the vault, and re-key the vault if
its password was involved. For an identifier, RFC 1918 addresses are low impact, while public addresses, tailnet
names, MACs and serials are higher; decide whether anything on the network needs to change. Ask GitHub Support to
purge cached views and the `refs/pull/*` copies. If it reached `main`, removing it means rewriting history, which
needs the ruleset's force-push rule switched off for the time and every clone re-made.

Then add a fixture to `tests/policy/test_gitleaks_rules.py` for whatever the checks missed. `SECURITY.md` tells others
how to report a problem privately.
