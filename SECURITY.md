# Security

This repository holds no plaintext credentials. The only secrets in it are the Kubernetes Secrets, encrypted
with SOPS for the cluster's age key; Ansible's vault is never committed (until 2026-10-20 it was, as Ansible Vault
ciphertext, which the history keeps).

To report a vulnerability, use GitHub's private **Report a vulnerability** form (Security tab).
Please do not open a public issue for anything that touches credentials or the network layout.
