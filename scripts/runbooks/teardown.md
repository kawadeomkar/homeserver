# Taking the cluster down again

How to remove a Kubernetes cluster this repository built, such as a test deployment, and everything it brought
with it, leaving the NAS and the Proxmox host as they were before `make apply TYPE=kubernetes`. Nothing here runs by
itself; do it in this order, top to bottom. Commands on the Proxmox host run as root; addresses and MACs are in
`group_vars/all/local.yml`.

What stays, since it is not the cluster's: the storage ids under their class names (`homeserver-persistent`,
`homeserver-ephemeral`, the project pools) and `homeserver-backup`, the NAS's `proxmox/backup` and project datasets,
the package upgrades `make apply TYPE=proxmox` installed, and the age key's public half in `.sops.yaml` (harmless
without the key).

## 1. Keep what you want from the apps

The apps' own data (databases, settings) lives on the worker's data disk, which step 2 destroys. With a NAS, the
shared data (Nextcloud's files, PhotoPrism's photos, the recordings) is on the NAS datasets, which step 4 destroys
only if you choose to, and the media library (`ephemeral/jellyfin`) predates the cluster and is never removed here.
Without a NAS all of it is on the worker's disks, and step 2 destroys it with the rest: the files and photos on the
data volume, the library and the recordings on the media disk, never backed up (or on the data volume, with
`kubernetes_media_on_data_volume`). Export anything worth keeping first: Nextcloud's and PhotoPrism's files through
their apps or from the NAS datasets, Grafana's dashboards, the library, and so on.

## 2. The VMs

Stop Flux first, so nothing re-creates what you remove, then the VMs:

```bash
export KUBECONFIG=~/.talos/homeserver/kubeconfig     # or local.yml's kubernetes_talos_config_dir
flux suspend kustomization --all -n flux-system     # optional: the VMs go anyway
```

On the Proxmox host:

```bash
qm stop 301; qm stop 300
qm destroy 301 --purge 1 --destroy-unreferenced-disks 1
qm destroy 300 --purge 1 --destroy-unreferenced-disks 1
```

`--purge` also takes the VMs out of the `vm-nightly` backup job and of HA. Then remove their backups, which would
otherwise make `make apply TYPE=kubernetes` refuse to build the VMs anew (`proxmox_vm` reads leftovers as a rebuild):

```bash
pvesm list homeserver-backup --vmid 300; pvesm list homeserver-backup --vmid 301    # without a NAS: vm_backup_storage
pvesm free <volid>                           # for each backup listed
pvesm list local --content iso | grep talos  # the Talos ISOs proxmox_vm downloaded, on kubernetes_iso_storage
pvesm free local:iso/talos-<version>-<schematic>-metal-amd64.iso    # local, unless local.yml names another
```

The `vm-nightly` job stays, covering `vm_backup_extra_vmids` if any; with none, remove it by hand
(`pvesh delete /cluster/backup/vm-nightly`), since the role never deletes a job.

## 3. The workstation

```bash
rm -rf ~/.talos/homeserver                   # or kubernetes_talos_config_dir: the secrets bundle, talosconfig, kubeconfig, the bootstrap record, etcd snapshots
```

In `group_vars/all/local.yml`, delete the Kubernetes section (every `kubernetes_*`, `flux_*`, `sops_*` and
`haos_url` setting): without node addresses the NAS and Proxmox plays leave the cluster's parts alone, and
`make apply TYPE=kubernetes` ends by itself. The secrets bundle went with the directory: a new cluster gets a new one,
and so does the password manager's copy. Keep the age key (`~/.config/sops/age/homeserver.agekey`) only if the SOPS
files under `k8s/` stay; they decrypt with it alone.

## 4. The NAS (with a NAS)

The NAS role creates and never deletes, so this part is by hand, the one exception to "every change goes through
the role", and needs the owner. With the Kubernetes section gone from `local.yml`, `make check TYPE=truenas` shows
nothing to add back; it then lists what remains for you to remove in the TrueNAS UI (below with the default pool
names, `persistent` and `ephemeral`: yours if `local.yml` renames them):

- **The NFS shares** of `persistent/nextcloud`, `persistent/photoprism`, `ephemeral/frigate` and
  `ephemeral/jellyfin` (Shares → NFS): the last one exports the media library to the worker; removing the share
  leaves the library alone.
- **The snapshot tasks** of `persistent/nextcloud` and `persistent/photoprism` (Data Protection → Periodic Snapshot
  Tasks), and their snapshots once you no longer want them.
- **The datasets** `persistent/nextcloud`, `persistent/photoprism` and `ephemeral/frigate`, with what is in them,
  after step 1 (Datasets → the dataset → Delete). Not `ephemeral/jellyfin`.

The two snapshot tasks of `persistent/proxmox/vm` are the VM storage's, not the cluster's: keep them.

## 5. The router

Remove the nodes' DHCP reservations and the wildcard DNS record for `*.<domain>` from OPNsense, and free MetalLB's
block.

## 6. Check

`make check TYPE=truenas` and `make check TYPE=proxmox` report `changed=0`, `make check TYPE=kubernetes` says this setup
has no cluster, and `qm list` shows neither VM.
