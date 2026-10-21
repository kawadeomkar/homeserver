# Nextcloud

Nextcloud (LinuxServer.io image) and its MariaDB, in the namespace `nextcloud`. The users' files are on the static
volume `nextcloud-data` (the NAS's `persistent/nextcloud` with a NAS, the node's data volume without one); the
configuration and the database are on local volumes.

## First start

1. The database's Secret, `nextcloud/nextcloud-db`, is committed (`nextcloud-db.sops.yaml`). A fork makes its own
   before the first start (`k8s/README-secrets.md`). Never make it anew once MariaDB has started: MariaDB keeps
   the passwords it first started with, and Nextcloud would then hold the other.
2. Through a port forward, before the LAN can reach it (`scripts/runbooks/first-run.md`:
   `kubectl -n nextcloud port-forward svc/nextcloud 8080:80`, then http://localhost:8080), run the setup wizard: an
   admin account of your choosing, and the database **MySQL/MariaDB** with the user, password and database name
   from the Secret, on host `nextcloud-db`.
3. Tell Nextcloud the name and the proxy it is reached through (the namespace's pod, `deploy/nextcloud`):

   ```bash
   kubectl exec -n nextcloud deploy/nextcloud -- occ config:system:set trusted_domains 1 --value=nextcloud.<domain>
   kubectl exec -n nextcloud deploy/nextcloud -- occ config:system:set trusted_proxies 0 --value=<pod network>
   kubectl exec -n nextcloud deploy/nextcloud -- occ config:system:set overwriteprotocol --value=https
   ```

   `<pod network>` is the cluster's pod subnet, where Traefik runs: Talos's default, which
   `talosctl get machineconfig -o yaml | grep -A1 podSubnets` shows. `overwriteprotocol` is `https` because Traefik
   serves Nextcloud over HTTPS (`scripts/runbooks/tls.md`) and talks to it in plain HTTP.
