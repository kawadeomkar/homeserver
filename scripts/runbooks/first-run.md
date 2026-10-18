# First run: setting the apps up before the LAN can reach them

Several apps make their first visitor the admin: Nextcloud's, Jellyfin's, Seerr's and SABnzbd's setup wizards, and
Sonarr, Radarr and Prowlarr, which until their first login exists serve anyone, their API key included. So a new
cluster serves nothing to the LAN: until `group_vars/all/local.yml` sets `kubernetes_ingress_allowed_range`, Traefik
answers every request with 403 (`k8s/infrastructure/configs/traefik.yaml`). Set each app up through a port forward,
which goes through the Kubernetes API and needs your kubeconfig, and open the LAN last.

Also do this after any rebuild that starts the apps without their data. A cluster restored from its VM backups
keeps its apps' settings, so only the last step applies.

## Port forwards

```bash
export KUBECONFIG=~/.talos/homeserver/kubeconfig
kubectl -n media port-forward svc/sonarr 8989      # then http://localhost:8989; Ctrl-C when done
```

| App | Namespace | Forward | What the first visit asks |
|---|---|---|---|
| Sonarr | `media` | `svc/sonarr 8989` | Authentication: method **Forms**, a username and a long password from your password manager |
| Radarr | `media` | `svc/radarr 7878` | The same |
| Prowlarr | `media` | `svc/prowlarr 9696` | The same |
| SABnzbd | `media` | `svc/sabnzbd 8080` | Its wizard (the news server). Then Config → General → Security: a username and password; Config → Folders: incomplete `/data/usenet/incomplete`, complete `/data/usenet/complete`; Config → Special → `host_whitelist`: add `sabnzbd.<domain>` |
| Jellyfin | `media` | `svc/jellyfin 8096` | Its wizard: the admin account. Then Dashboard → Playback → Transcoding: transcode path `/transcode` |
| Seerr | `media` | `svc/seerr 5055` | Sign in with Jellyfin, at `http://jellyfin.media.svc.cluster.local:8096`, as Jellyfin's admin; then Sonarr and Radarr by their Service names and API keys |
| Nextcloud | `nextcloud` | `svc/nextcloud 8080:80` | Its wizard: the admin account and the database (`k8s/apps/nextcloud/README.md`), then its `occ` steps there |
| FileBrowser | `media` | `svc/filebrowser 8081:8080` | Nothing: user `admin`, password `FILEBROWSER_ADMIN_PASSWORD` from its SOPS file |
| PhotoPrism | `photoprism` | `svc/photoprism 2342` | Nothing: user `admin`, password `PHOTOPRISM_ADMIN_PASSWORD` from its SOPS file |
| Frigate | `frigate` | `svc/frigate 8971` | Nothing: user `admin`, with the password Frigate printed in its log at its first start (`kubectl -n frigate logs deploy/frigate \| grep -i password`); change it in Frigate's settings |
| Grafana | `monitoring` | `svc/kube-prometheus-stack-grafana 3001:80` | Nothing: user `admin`, password as CLAUDE.md's Monitoring says |
| Homepage | `homepage` | `svc/homepage 3000` | Nothing: it has no login, and shows only links |

`k8s/README-secrets.md` says how to read a value from a SOPS file without printing the others.

Sonarr, Radarr and Prowlarr ask for authentication from every address whatever their settings say: their
Deployments set `<APP>__AUTH__REQUIRED=Enabled`, since "disabled for local addresses" would let the whole LAN in
(Traefik's requests come from a pod's address, which counts as local). Their API key is generated at their first
start; regenerate it (Settings → General → Security) if it was ever seen outside the app.

## Opening the LAN

Once every app above is set up, set the range of addresses Traefik serves, usually the LAN's, in
`group_vars/all/local.yml`:

```yaml
kubernetes_ingress_allowed_range: 192.0.2.0/24
```

Then `make kubernetes`, which writes it into the cluster's settings, and
`flux reconcile kustomization infra-configs -n flux-system` to apply it without waiting. Each app answers at
`https://<app>.<domain>` from then on, once the device trusts the cluster's CA (`scripts/runbooks/tls.md`). Check
that one app answers from a device in the range and that a request from outside it gets 403. An app added later has
its own first visit: set it up through a port forward before its Ingress is merged, or narrow the range to your own
address meanwhile.
