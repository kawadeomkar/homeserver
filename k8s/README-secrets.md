# Kubernetes secrets

Every Kubernetes Secret in this repository is a `*.sops.yaml` file encrypted with [SOPS](https://getsops.io) for the
cluster's age key. `roles/flux_bootstrap` keeps the key in the cluster as the Secret `flux-system/sops-age`, and
Flux decrypts a Secret as it applies it only in a Kustomization that says so: each one listed under
`k8s/clusters/<setup>/` must carry `decryption: {provider: sops, secretRef: {name: sops-age}}`. The Flux Operator's
own root Kustomization, which syncs that directory, decrypts nothing and substitutes nothing, so a Secret never goes
in the directory itself. Ansible's secrets stay in Ansible Vault (`group_vars/all/local.vault.yml`, git-ignored); never put a
Kubernetes Secret there.

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

## The Secrets the apps need

| File | Secret | Keys |
|---|---|---|
| `k8s/apps/nextcloud/nextcloud-db.sops.yaml` | `nextcloud/nextcloud-db` | `MYSQL_ROOT_PASSWORD`, `MYSQL_DATABASE` (`nextcloud`), `MYSQL_USER` (`nextcloud`), `MYSQL_PASSWORD` |
| `k8s/apps/photoprism/photoprism-db.sops.yaml` | `photoprism/photoprism-db` | `MYSQL_ROOT_PASSWORD`, `MYSQL_DATABASE` and `MYSQL_USER` both exactly `photoprism` (its Deployment names them), `MYSQL_PASSWORD` |
| `k8s/apps/photoprism/photoprism.sops.yaml` | `photoprism/photoprism` | `PHOTOPRISM_ADMIN_PASSWORD`, `PHOTOPRISM_DATABASE_PASSWORD` (equal to `photoprism-db`'s `MYSQL_PASSWORD`: above) |
| `k8s/apps/frigate/frigate.sops.yaml` | `frigate/frigate` | `FRIGATE_RTSP_PASSWORD` (the camera restreams' password, user `admin`), and the cameras' and MQTT's hosts and credentials as `FRIGATE_*` variables |
| `k8s/apps/media/filebrowser.sops.yaml` | `media/filebrowser` | `FILEBROWSER_ADMIN_PASSWORD`, `FILEBROWSER_JWT_TOKEN_SECRET` |

Every app reads its Secret with `envFrom`, so every key becomes an environment variable: the names matter to the
container. Until an app's Secret exists, its pods wait for it, and its Flux Kustomization does not become ready;
the other groups are not held up. A SOPS file the cluster cannot decrypt, encrypted for another age key, fails its
whole Flux Kustomization, every app of the group, and nothing else.

- **nextcloud-db**: `linuxserver/mariadb` treats `MYSQL_DATABASE`, `MYSQL_USER` and `MYSQL_PASSWORD` as all or
  none. The Nextcloud admin account is made in the web setup wizard: MySQL/MariaDB, database host `nextcloud-db`,
  database name and user as `MYSQL_DATABASE` and `MYSQL_USER` (`nextcloud`), and `MYSQL_PASSWORD` (`sops -d` reads
  it); the LinuxServer.io image reads no admin variables.
- **photoprism-db** and **photoprism**: PhotoPrism builds its database connection from `PHOTOPRISM_DATABASE_*`, the
  server, name and user set in its Deployment and the password from this Secret, so keep it equal to
  `photoprism-db`'s `MYSQL_PASSWORD`.
- **frigate**: its configuration names no camera and no address (this repository is public); Frigate expands
  `{FRIGATE_<NAME>}` in camera inputs and go2rtc streams from these variables. `FRIGATE_RTSP_PASSWORD` guards the
  restreams `frigate-rtsp` publishes on the LAN (`rtsp://admin:<password>@<LB_FRIGATE_IP>:8554/<camera>`): without
  it, any device on the network could watch every camera, so Frigate does not start until it is set.
