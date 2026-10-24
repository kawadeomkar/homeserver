#!/usr/bin/env bash
# Installs just in a CI job, from upstream's release archive at the version and checksum ci.yml pins, and puts it on
# the job's PATH from the next step on. Every job that installs the tooling runs this first: just install fills the
# virtualenv, so just cannot come from it.
#   JUST_VERSION          the release
#   JUST_SHA256_X86_64    its x86_64 archive's SHA-256 (the lint, docs, kubernetes and leaks jobs)
#   JUST_SHA256_AARCH64   its aarch64 archive's SHA-256 (the test and cluster jobs, on Arm)
set -euo pipefail

case "$(uname -m)" in
  x86_64) arch=x86_64 sum=$JUST_SHA256_X86_64 ;;
  aarch64) arch=aarch64 sum=$JUST_SHA256_AARCH64 ;;
  *) echo "no just pin for $(uname -m)" >&2; exit 1 ;;
esac
cd "$RUNNER_TEMP"
curl -fsSLo just.tgz \
  "https://github.com/casey/just/releases/download/$JUST_VERSION/just-$JUST_VERSION-$arch-unknown-linux-musl.tar.gz"
echo "$sum  just.tgz" | sha256sum -c -
tar -xzf just.tgz just
echo "$RUNNER_TEMP" >> "$GITHUB_PATH"
