# Nextcloud

Nextcloud (LinuxServer.io image) and its MariaDB, in the namespace `nextcloud`. The users' files are on the static
volume `nextcloud-data` (the NAS's `persistent/nextcloud` with a NAS, the node's data volume without one); the
configuration and the database are on local volumes.

## First start

1. Make the Secret `nextcloud/nextcloud-db` (`k8s/README-secrets.md`) and list it in `kustomization.yaml`.
2. Open `http://nextcloud.<domain>/` and run the setup wizard: an admin account of your choosing, and the database
   **MySQL/MariaDB** with the user, password and database name from the Secret, on host `nextcloud-db`.
3. Tell Nextcloud the name and the proxy it is reached through (the namespace's pod, `deploy/nextcloud`):

   ```bash
   kubectl exec -n nextcloud deploy/nextcloud -- occ config:system:set trusted_domains 1 --value=nextcloud.<domain>
   kubectl exec -n nextcloud deploy/nextcloud -- occ config:system:set trusted_proxies 0 --value=<pod network>
   kubectl exec -n nextcloud deploy/nextcloud -- occ config:system:set overwriteprotocol --value=http
   ```

   `<pod network>` is the cluster's pod subnet, where Traefik runs: Talos's default, which
   `talosctl get machineconfig -o yaml | grep -A1 podSubnets` shows. Once TLS is in place, set `overwriteprotocol`
   to `https`.
