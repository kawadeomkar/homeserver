# Versions, tags and the stamps on the machines

The version that answers "what is this machine running" is a commit. Every `just` run works it out
(`_deploy_version` in the justfile): the newest commit on the checked-out branch's first-parent chain (`main`'s, when
deploying from `main`) that changed a file Ansible may read, named by `git describe`. A commit that touches only
`k8s/` (Flux's), `tests/`, `.github/`, `scripts/ci/`, the linters', hooks' and tests' configuration (`ruff.toml`,
`.pre-commit-config.yaml`, `.gitleaks.toml` and the like) or Markdown leaves it alone, since no playbook reads those
files, so a Renovate merge or a linter's change moves no machine's stamp. In short, with the full list of what is
left out in the justfile:

```bash
commit=$(git -c log.showSignature=false log -1 --first-parent --format=%H -- . ':(exclude)k8s' ':(exclude)tests' ':(exclude).github' ':(exclude,glob)**/*.md')
git describe --first-parent --always --abbrev=12 --match 2026.10.0 --match 2026.11.0 "$commit"   # each CalVer tag, by name
```

Before the first tag it is the commit alone (`afb72ddc4ca1`); after one, the tag, the merges on `main` since and the
commit (`2026.11.0-3-g5de78852f353`); `-dirty` when Ansible's files have uncommitted or untracked changes. git
lengthens the hash past 12 digits when 12 are ambiguous. A shallow clone, whose history may end before that commit,
has no version: every run refuses there until `git fetch --unshallow`.

**What it does not count**: the git-ignored files Ansible reads, `group_vars/all/local.yml`, the vault and
`inventory`. They are the setup's, not the repository's, so a deploy after changing only them (a VM's size, the apps
a host runs) leaves every stamp as it was, and a stamp says which commit's code and tracked settings a machine got,
not which setup's. So does a run given `-e` overrides.

## Where it shows

- **The run's log and output.** Every playbook run says `Version: homeserver <describe> (committed <date>)`, and its
  log in `logs/` starts with `homeserver <describe> (committed <date>)`.
- **The machines' Notes**, in the Proxmox web UI, on each one's Summary. The last role or play of each deploy writes
  one block, marked as homeserver's, in the Notes of what it deployed: `just deploy proxmox` the host's (the node's
  Notes), `just deploy homeassistant` Home Assistant's VM, `just deploy kubernetes` the cluster's VMs. Text outside
  the block is never touched, so notes of your own, or another project's block, stay as they are
  (`roles/proxmox_notes`). The block's hidden begin marker carries the commit, its describe string and the commit's
  date, which `just status` reads back; its text says the same, and the command that deploys the machine. A run
  narrowed with `--tags`, `--limit` or `--skip-tags`, one started part way with `--start-at-task` or `--step`, and a
  run started without `just`, leave the stamps alone, and the run says why. `just talos-upgrade` leaves them alone
  too: it moves the nodes to the pins and applies nothing else of a deploy, and `just status` compares the pins on
  its own. A stamp says where the last deploy came from, not all a machine runs: Flux applies `main` to the cluster
  after any deploy, and Home Assistant updates itself.
- **`just status`**, for every machine at once: each stamp against the commit this checkout would deploy (this
  checkout's, behind it, ahead of it, from another branch, from uncommitted files, none yet, not read when its Notes
  could not be, not placed when git could not say, or broken when its Notes hold a block the next deploy would
  refuse). A machine behind is a thing to do, with the command that brings it up to date. Flux's line beside it shows
  the commit the cluster applied against the one its branch points at.

The stamp is written last, after everything else the run deploys, and only if the Notes are as the run read them a
moment before: a change made meanwhile, in the web UI or by another project, is kept, never overwritten. Proxmox
also refuses to change a locked VM's Notes, as during the nightly backup at 02:00. Either way the run fails there,
with the deploy itself done and Proxmox's message saying why, and the next run writes the stamp.

## Deploying from uncommitted files

A real `just deploy` refuses uncommitted changes to what Ansible reads, so that what reaches the machines is a commit
you can find again and their stamps name it, and a tree that is no git checkout (an unpacked archive), whose stamp
could name no commit at all. That holds for a deploy that stamps nothing too (`just deploy truenas`, a run narrowed
with `--limit`). A dry run never refuses, `just check` or `just deploy … --check` (or `-C`, `-CD`), and nor does a
run that only lists or checks syntax (`--list-tasks`, `--syntax-check`). To deploy uncommitted changes anyway, say so
after the playbook; the stamp then ends in `-dirty`. Any other recipe refuses the flag, since it refuses nothing:

```bash
just deploy proxmox --allow-dirty
just deploy site --allow-dirty      # every playbook: name site, as for anything else passed with it
```

## Cutting a tag

A tag gives a commit a name, so a stamp reads as `2026.11.0-3-g…` ("three merges after the November tag") rather
than a bare hash. Tags are cut by hand, whenever a name helps: after a stack of pull requests merges, or before a
`just talos-upgrade`.

- **The name**: CalVer `YYYY.M.PATCH`, unpadded and without a `v`, the patch counting from 0 each month
  (`2026.11.0`, `2026.11.1`), the year from 2000 on. Any other shape (`v1.0`, `2026.11.0-rc1`, `2026.11`,
  `2026.11.0.1`, `2026.13.0`, `2026.11.01`) is never picked as the version: `_deploy_version` names to `git
  describe` only the tags of this shape.
- **Annotated**, on a merge commit on `main`: `git describe` counts annotated tags alone, and the tags GitHub's release
  page makes are lightweight.

```bash
git fetch origin && git checkout main && git pull
git tag -a 2026.11.0 -m '2026.11.0' origin/main
git push origin 2026.11.0
gh release create 2026.11.0 --verify-tag --generate-notes   # optional: release notes, the PRs merged since the last tag
```

`--verify-tag` makes the release use the annotated tag rather than create a lightweight one of its own. When a VM
shows `2026.10.0` and `main` is at `2026.11.0`, the release page says what changed between them.

**When a new tag shows in the stamps.** A stamp names the last commit that changed Ansible's files, so a tag shows
there only once that commit is the tagged one or comes after it. Tag a merge that touched only `k8s/` or
documentation, and the machines keep the previous name until the next Ansible change lands and is deployed. The
stamp is still exact; to see the new name at once, tag the last merge that changed Ansible's files instead.

**A tag is write-once.** The tag ruleset (below) refuses to move or delete one, which `git` would otherwise do with
`--force`. A tag pushed by mistake stays: cut the next patch (`2026.11.1`) rather than reuse the name. Deleting one
means switching the ruleset off by hand for the moment it takes. The ruleset restricts neither who creates a tag nor
its name, on purpose: only the owner pushes today, no workflow has `contents: write`, and a name of any other shape
never counts. Should a collaborator or an app with write access arrive (Mend Renovate, in `TODO.md`), add a second
tag ruleset with a `creation` rule and the owner as its one bypass: in this one, which has no bypass, a creation rule
would refuse every new tag.

## Applying the rulesets

GitHub never reads `.github/rulesets/`: each file is a ruleset's reviewed body, applied by hand.

- **`tags.json`, "Protect tags"**: every tag write-once, no bypass (`deletion`, `update` and `non_fast_forward`
  rules on `~ALL`). The first time it is created, which gives it its ID:

  ```bash
  gh api -X POST repos/kawadeomkar/homeserver/rulesets --input .github/rulesets/tags.json --jq .id
  ```

  Then record that ID here, and update it with `gh api -X PUT repos/kawadeomkar/homeserver/rulesets/<id> --input
  .github/rulesets/tags.json`.
- **`main.json`, "Protect main"**: pull requests merge with merge commits alone, which keeps the count after a tag
  the number of merges on `main`.

  ```bash
  gh api -X PUT repos/kawadeomkar/homeserver/rulesets/24555579 --input .github/rulesets/main.json
  ```

Read the live rulesets (`gh api repos/kawadeomkar/homeserver/rulesets`) before relying on them.
