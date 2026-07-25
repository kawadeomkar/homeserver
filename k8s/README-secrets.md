# Secrets required by these manifests

Plaintext `Secret` manifests are **not** committed to this repo, and `.gitignore` blocks
`k8s/**/*secret*.yaml` so they cannot be added by accident. Four such files were removed in the commit
that added this document — they held base64 values (base64 is encoding, not encryption) and, in
photoprism's case, invalid placeholders that made `kubectl apply` fail outright.

Nothing here has ever been deployed, so no credential needed rotating. What the deletion does mean:
**nextcloud and photoprism cannot be deployed until their Secrets are supplied by a real mechanism.**

## One deliberate exception

`k8s/homepage/secret.yaml` stays tracked. It is a `kubernetes.io/service-account-token` Secret — an
annotation-only request whose token is populated by the cluster, so it carries no credential material.
Note `.gitignore` does not untrack files that are already tracked, so editing it continues to work; the
new rule only stops *new* plaintext Secret manifests from being added.

## Intended mechanism

**SOPS + age**, decided as part of the Proxmox/Talos migration and deliberately not implemented yet —
the layout depends on the GitOps tool (Flux/Argo), and an `.sops.yaml` written now would be rewritten.
Rationale: no in-cluster controller to bootstrap, and `talhelper` already wants an age key for Talos
machine secrets, so one key type covers both concerns.

Whichever mechanism is chosen, the age/private key must **not** live in the repo it decrypts, and needs
a documented backup location — the cluster cannot reconcile encrypted manifests without it.

Until then, create them by hand with the commands below, or skip the two services.

### Creating them by hand

The namespace must exist first (`kubectl apply -f k8s/<service>/0-namespace.yaml`). Single-quote every
value: the photoprism DSN contains `&`, `(`, and `)`, which the shell would otherwise interpret.

```sh
kubectl -n nextcloud create secret generic nextcloud-db-secret \
  --from-literal=MYSQL_ROOT_PASSWORD='...' \
  --from-literal=MYSQL_DATABASE='nextcloud' \
  --from-literal=MYSQL_USER='nextcloud' \
  --from-literal=MYSQL_PASSWORD='...'

kubectl -n photoprism create secret generic photoprism-db-secret \
  --from-literal=MYSQL_ROOT_PASSWORD='...' \
  --from-literal=MYSQL_DATABASE='photoprism' \
  --from-literal=MYSQL_USER='photoprism' \
  --from-literal=MYSQL_PASSWORD='...'

kubectl -n photoprism create secret generic photoprism-secrets \
  --from-literal=PHOTOPRISM_ADMIN_PASSWORD='...' \
  --from-literal=PHOTOPRISM_DATABASE_DSN='photoprism:...@tcp(photoprism-mariadb-service.photoprism.svc.cluster.local:3306)/photoprism?charset=utf8mb4,utf8&parseTime=true'
```

Committing a filled-in Secret manifest is blocked by `.gitignore` (`k8s/**/*secret*.yaml`, both cases,
since git globs are case-sensitive). Exempted as safe-by-construction: `*.sops.yaml`, `*sealed*.yaml`,
`*.example.yaml`, `*.template.yaml`, `externalsecret*.yaml`, `secretstore*.yaml`. Note filename rules
cannot catch an extensionless private key (an SSH key named `rpi`, say) — a secret scanner is the real
control, filed as `REPO-3` in `docs/ISSUES.md`.

## What each Secret must contain

All four references use `envFrom.secretRef`, i.e. every key in the Secret is injected as an environment
variable. Key names therefore matter to the container, not to the manifest.

### `nextcloud-db-secret` — namespace `nextcloud`

Consumed by both `deployment.yaml` (the `lscr.io/linuxserver/nextcloud` app) and `statefulset.yaml`
(the `linuxserver/mariadb` database).

| Key | Consumed by |
|---|---|
| `MYSQL_ROOT_PASSWORD` | mariadb |
| `MYSQL_DATABASE` | mariadb |
| `MYSQL_USER` | mariadb |
| `MYSQL_PASSWORD` | mariadb |

The deleted file also carried `NEXTCLOUD_ADMIN` and `NEXTCLOUD_ADMIN_PASSWORD`. **Do not recreate them:**
`lscr.io/linuxserver/nextcloud` ignores both — it documents only `PUID`/`PGID`/`TZ`/`UMASK`, and the admin
account is created in the web setup wizard. (Even the official image spells it `NEXTCLOUD_ADMIN_USER`.)
`linuxserver/mariadb` additionally treats `MYSQL_DATABASE`/`MYSQL_USER`/`MYSQL_PASSWORD` as all-or-none:
supply all three or none of them.

### `photoprism-db-secret` — namespace `photoprism`

Consumed by `mariadb-deployment.yaml` (image `mariadb:latest`). The deleted file carried all four of these
plus two `NEXTCLOUD_*` leftovers copied from nextcloud — a real copy-paste, but a harmless one, since the
MariaDB entrypoint simply ignores the extras. (An earlier version of this document and of PR #3 claimed
the database "would never have initialised"; that was wrong.) It needs the standard four:

`MYSQL_ROOT_PASSWORD`, `MYSQL_DATABASE`, `MYSQL_USER`, `MYSQL_PASSWORD`

### `photoprism-secrets` — namespace `photoprism`

Consumed by `statefulset.yaml` (image `photoprism/photoprism`). Two conflicting definitions of this
Secret existed (`1-secret-photoprism.yaml` with `data:`, `secret.yaml` with `stringData:`), so which one
won depended on apply order.

| Key | Notes |
|---|---|
| `PHOTOPRISM_ADMIN_PASSWORD` | initial admin login |
| `PHOTOPRISM_DATABASE_DSN` | must point at the in-cluster DB, e.g. `user:pass@tcp(photoprism-mariadb-service.photoprism.svc.cluster.local:3306)/photoprism?charset=utf8mb4,utf8&parseTime=true` — note the Service is `photoprism-mariadb-service` (`service-db.yaml:4`); `photoprism-mariadb` is the *Deployment* name and will not resolve |

Keep the DSN credentials consistent with `MYSQL_USER`/`MYSQL_PASSWORD`/`MYSQL_DATABASE` above.

## Supplying the Secret is not the only thing blocking photoprism

`k8s/photoprism/mariadb-deployment.yaml` runs `runAsUser: 1000` against upstream `mariadb`, whose
entrypoint expects uid 999 (or root) to initialise the data directory — so the pod will still fail after
the Secret exists. Tracked as part of `K8S-3` in `docs/ISSUES.md`. Nothing here has ever been deployed, so
photoprism should be treated as an unfinished service rather than a regression.
