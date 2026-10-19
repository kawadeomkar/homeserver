# Kubernetes secrets

Every Kubernetes Secret in this repository is a `*.sops.yaml` file encrypted with [SOPS](https://getsops.io) for the
cluster's age key, which Flux decrypts them with as it applies them. Ansible's secrets stay in Ansible Vault
(`group_vars/all/vault.yml`); never put a Kubernetes Secret there.

Only the values are encrypted (`data`, `stringData`), so a diff shows which keys changed. A plaintext Secret
manifest is never committed: `.gitignore` blocks `k8s/**/*secret*.yaml` outside the safe kinds, gitleaks refuses one
anywhere under `k8s/`, and since gitleaks allows every `*.sops.yaml`, `scripts/ci/check-sops-staged.sh` refuses one
that was never encrypted, before it is committed (the pre-commit hook), before it is pushed and in CI.

## The key

One age key, for this cluster alone, in a file of its own: `~/.config/sops/age/homeserver.agekey`, mode 0600, with a
copy in the password manager. Flux gets the whole file, so it must never be a keyring holding other projects' keys.

```bash
mkdir -p ~/.config/sops/age && chmod 700 ~/.config/sops/age
age-keygen -o ~/.config/sops/age/homeserver.agekey && chmod 600 ~/.config/sops/age/homeserver.agekey
age-keygen -y ~/.config/sops/age/homeserver.agekey     # the public key: put it in .sops.yaml as `age:`
export SOPS_AGE_KEY_FILE=~/.config/sops/age/homeserver.agekey   # in the shell profile: sops on macOS reads no ~/.config
```

**A fork encrypts for its own key**: replace `.sops.yaml`'s recipient with yours, and make every Secret anew; this
repository's files decrypt only with its owner's key.

## Making a Secret

Generate each password on the spot and never print it: the command below renders the Secret with kubectl, labels it
with its app's name (`tests/policy/test_k8s.py` wants every object under `k8s/apps/` labelled), and hands it to sops
on stdin, so the plaintext never touches the disk or the terminal. `--filename-override` makes sops apply
`.sops.yaml`'s rule for that path.

```bash
file=k8s/apps/nextcloud/nextcloud-db.sops.yaml
kubectl create secret generic nextcloud-db -n nextcloud --dry-run=client -o yaml \
  --from-literal=MYSQL_ROOT_PASSWORD="$(openssl rand -base64 24)" \
  --from-literal=MYSQL_DATABASE=nextcloud \
  --from-literal=MYSQL_USER=nextcloud \
  --from-literal=MYSQL_PASSWORD="$(openssl rand -base64 24)" \
  | kubectl label --local -f - app.kubernetes.io/name=nextcloud-db -o yaml \
  | sops --encrypt --input-type yaml --output-type yaml --filename-override "$file" /dev/stdin > "$file"
```

Then list the file in its directory's `kustomization.yaml` `resources`, and commit it. Edit one later with
`sops <file>`, which decrypts it into your editor and encrypts it again on save. Read one with `sops -d <file>`.

**One password in two Secrets**, as PhotoPrism's database password is (in `photoprism-db` and `photoprism`): generate
it once into a shell variable, use it in both commands, and unset it. It never reaches the disk or the terminal.

```bash
db_password="$(openssl rand -base64 24)"
file=k8s/apps/photoprism/photoprism-db.sops.yaml
kubectl create secret generic photoprism-db -n photoprism --dry-run=client -o yaml \
  --from-literal=MYSQL_ROOT_PASSWORD="$(openssl rand -base64 24)" \
  --from-literal=MYSQL_DATABASE=photoprism \
  --from-literal=MYSQL_USER=photoprism \
  --from-literal=MYSQL_PASSWORD="$db_password" \
  | kubectl label --local -f - app.kubernetes.io/name=photoprism-db -o yaml \
  | sops --encrypt --input-type yaml --output-type yaml --filename-override "$file" /dev/stdin > "$file"
file=k8s/apps/photoprism/photoprism.sops.yaml
kubectl create secret generic photoprism -n photoprism --dry-run=client -o yaml \
  --from-literal=PHOTOPRISM_ADMIN_PASSWORD="$(openssl rand -base64 24)" \
  --from-literal=PHOTOPRISM_DATABASE_PASSWORD="$db_password" \
  | kubectl label --local -f - app.kubernetes.io/name=photoprism -o yaml \
  | sops --encrypt --input-type yaml --output-type yaml --filename-override "$file" /dev/stdin > "$file"
unset db_password
```

Read a value afterwards, PhotoPrism's admin password say, with `sops -d --extract
'["data"]["PHOTOPRISM_ADMIN_PASSWORD"]' k8s/apps/photoprism/photoprism.sops.yaml | base64 -d` (kubectl writes the
values base64-encoded, under `data`), or set your own in its place.
