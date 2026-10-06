# proxmox_storage

Adds NFS shares to a Proxmox VE node as storage, with `pvesm` over SSH. It also corrects the content,
format, mount options and enabled state of a storage that already exists. With only its defaults it
changes nothing.

It refuses a storage id that already exists with another type, server or export, rather than replacing
a storage that may hold disks. `community.proxmox.proxmox_storage` is not used: it only creates
storage, never corrects it, and has no format option.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `proxmox_storage_nfs` | `[]` | Storages: `id`, `export`; optional `server`, `content`, `format`, `nfs_options`, `enabled` |
| `proxmox_storage_nfs_server` | `""` | NFS server for entries without their own |
| `proxmox_storage_nfs_options` | `""` | Mount options, e.g. `vers=4.2`; empty leaves Proxmox's defaults |
| `proxmox_storage_format` | `qcow2` | Default image format |
| `proxmox_storage_content` | `[images]` | Content types |
| `proxmox_storage_enabled` | `true` | Enabled state |
| `proxmox_storage_node` | `""` | Node name; empty means the host's short hostname |
| `proxmox_storage_startall_delay` | `null` | Seconds to wait at boot before starting VMs (0-300); null leaves it alone |

A disk imported with `import-from` keeps the source image's format whatever the storage default says.
Whatever imports images must ask for `format=qcow2` itself.

## Example

```yaml
# group_vars/proxmox.yml
proxmox_storage_nfs_server: 192.0.2.10
proxmox_storage_nfs_options: vers=4.2
proxmox_storage_nfs:
  - id: nas-vm
    export: /mnt/tank/vm
proxmox_storage_startall_delay: 120
```

In check mode the role reads the node's configuration and prints its plan (`add`, `correct` or `ok` per
storage). The `pvesm` commands themselves run only for real.
