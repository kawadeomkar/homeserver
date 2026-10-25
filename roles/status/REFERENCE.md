<!-- DOCSIBLE START -->

# 📃 Role overview

## status



Description: Read the homeserver's machines without changing them, and report what runs and what needs doing






<details>
<summary><b>🧩 Argument Specifications in meta/argument_specs</b></summary>

#### Key: main

**Description**: 
- Reads the host the play runs on, as status_source names it, and leaves what it found in the fact status_found for the report entry point. Every command goes through the role's module status_read, which runs them at once and refuses any that is not on its read list.
- On the Proxmox host (proxmox) it reads the host, its storages and the VMs of status_vms; every other VM is only counted.
- For the cluster (cluster), on this machine, it reads the nodes, the apps (each Ingress's Service, then the Deployment it selects, and every Deployment a Flux Kustomization under status_cluster_apps_path applies), Flux, the certificates, the nodes' kubelet statistics, Alertmanager's alerts, etcd's state and the newest commit of the branch Flux syncs. It reads the credentials under no_log, keeping only the server the kubeconfig names and when each client certificate expires, and reports another cluster's credentials rather than use them. It never reads a Secret.
- For the NAS (nas) it reads nothing itself, and works out what status_nas_state says, the result of truenas_info with its status option, which roles/truenas's status entry point reads in one login - the pools' health, capacity and last scrub, TrueNAS's alerts, the snapshot tasks' last runs, the NFS service.
- Unless status_probe_apps is off, asks from this machine the addresses of the apps it found (the cluster's, and an app on a VM of its own) with the role's module status_probe, checking no certificate and sending no credentials.
- Reads only the sources the sections of status_types need (the filter status_host_reads); on the Proxmox host it says what its Tailscale says first, in the fact status_early.
- A host that does not answer is recorded as such, not a failure, so the report can say so.
- Refuses a section name that is not one of the report's.
- The defaults read nothing.


**Options**:


  - **status_source**
    - **Required**: false
    - **Type**: str
    - **Default**: 
  
    - **Description**: What the host is; empty reads nothing.
  
      - **Choices**:
    
          - 
    
          - proxmox
    
          - cluster
    
          - nas
    
  
  
  

  - **status_types**
    - **Required**: false
    - **Type**: list
    - **Default**: []
  
    - **Description**: The report's sections to read and show; empty means every one.
  
  
  

  - **status_vms**
    - **Required**: false
    - **Type**: list
    - **Default**: []
  
    - **Description**: The VMs this setup makes; every other VM on the host is only counted.
  
  
  
    
  

  - **status_setup**
    - **Required**: false
    - **Type**: dict
    - **Default**: {}
  
    - **Description**: What this setup has, as {nas, cluster, home_assistant}, for the report's first line.
  
  
  

  - **status_proxmox_node**
    - **Required**: false
    - **Type**: str
    - **Default**: 
  
    - **Description**: The Proxmox node's name; empty takes the host's short hostname.
  
  
  

  - **status_budget_storages**
    - **Required**: false
    - **Type**: list
    - **Default**: []
  
    - **Description**: Storages whose disk budget the report shows.
  
  
  

  - **status_storage_headroom_gib**
    - **Required**: false
    - **Type**: int
    - **Default**: 0
  
    - **Description**: GiB of a ZFS, directory or BTRFS storage kept out of its budget for the host's own files.
  
  
  

  - **status_reboot_required_file**
    - **Required**: false
    - **Type**: str
    - **Default**: 
  
    - **Description**: The file that says the host waits for a reboot; empty does not look.
  
  
  

  - **status_backup_jobs**
    - **Required**: false
    - **Type**: list
    - **Default**: []
  
    - **Description**: The backup jobs this setup has, as roles/proxmox_backup takes them ({id, schedule, storage, vmid}), checked against the host's; empty with VMs to back up says nothing is backed up.
  
  
  
    
  

  - **status_cluster_kubeconfig**
    - **Required**: false
    - **Type**: str
    - **Default**: 
  
    - **Description**: The kubeconfig the cluster is read with.
  
  
  

  - **status_cluster_talosconfig**
    - **Required**: false
    - **Type**: str
    - **Default**: 
  
    - **Description**: The talosconfig etcd's state is read with; a missing file reads none.
  
  
  

  - **status_cluster_server**
    - **Required**: false
    - **Type**: str
    - **Default**: 
  
    - **Description**: The API server the kubeconfig must name; another is reported and not read. Empty checks nothing.
  
  
  

  - **status_cluster_nodes**
    - **Required**: false
    - **Type**: list
    - **Default**: []
  
    - **Description**: The cluster's nodes, whose kubelet statistics give their memory and disks.
  
  
  
    
  

  - **status_cluster_apps_path**
    - **Required**: false
    - **Type**: str
    - **Default**: 
  
    - **Description**: The path in the repository under which Flux's Kustomizations apply the apps.
  
  
  

  - **status_cluster_annotation**
    - **Required**: false
    - **Type**: str
    - **Default**: 
  
    - **Description**: The annotation in which just suspend records an app's replicas, which marks it suspended.
  
  
  

  - **status_cluster_database_label**
    - **Required**: false
    - **Type**: str
    - **Default**: 
  
    - **Description**: The label (key=value) of an app's database, listed under the app.
  
  
  

  - **status_cluster_volumes**
    - **Required**: false
    - **Type**: dict
    - **Default**: {}
  
    - **Description**: The nodes' own volumes to show, as {mount path, name}.
  
  
  

  - **status_cluster_settings**
    - **Required**: false
    - **Type**: dict
    - **Default**: {}
  
    - **Description**: The ConfigMap of the cluster's settings, as {namespace, name, allowlist}, allowlist the key of Traefik's allowed range; empty reads none.
  
  
  

  - **status_cluster_alertmanager**
    - **Required**: false
    - **Type**: dict
    - **Default**: {}
  
    - **Description**: Alertmanager's pod, as {namespace, pod}, whose firing alerts amtool lists; empty reads none.
  
  
  

  - **status_cluster_flux_url**
    - **Required**: false
    - **Type**: str
    - **Default**: 
  
    - **Description**: The repository Flux syncs; empty does not compare its branch with what Flux applied.
  
  
  

  - **status_cluster_flux_ref**
    - **Required**: false
    - **Type**: str
    - **Default**: 
  
    - **Description**: The branch Flux syncs, as refs/heads/<name>.
  
  
  

  - **status_cluster_pins**
    - **Required**: false
    - **Type**: dict
    - **Default**: {}
  
    - **Description**: The versions the configuration pins, as {talos, kubernetes}, compared with what the nodes run.
  
  
  

  - **status_cluster_request_timeout**
    - **Required**: false
    - **Type**: int
    - **Default**: 10
  
    - **Description**: Seconds kubectl waits for the API server on each read.
  
  
  

  - **status_cluster_snapshot_dir**
    - **Required**: false
    - **Type**: str
    - **Default**: 
  
    - **Description**: The directory of the cluster's etcd snapshots, each named <anything>-<UTC time>.db (etcd-20261021T093000Z.db), whose newest the backups section shows; empty shows none.
  
  
  

  - **status_nas_state**
    - **Required**: false
    - **Type**: dict
    - **Default**: {}
  
    - **Description**: What truenas_info returned with its status option, or its failure; the NAS's findings.
  
  
  

  - **status_nas_release**
    - **Required**: false
    - **Type**: str
    - **Default**: 
  
    - **Description**: The TrueNAS release (major.minor) the NAS's configuration is written for; empty compares none.
  
  
  

  - **status_tailscale_state**
    - **Required**: false
    - **Type**: dict
    - **Default**: {}
  
    - **Description**: The Proxmox host's Tailscale, as roles/proxmox_tailscale's status entry point records it in proxmox_tailscale_status_state; empty when it is off.
  
  
  

  - **status_parallel**
    - **Required**: false
    - **Type**: int
    - **Default**: 8
  
    - **Description**: How many reads run at once on a host.
  
  
  

  - **status_timeout**
    - **Required**: false
    - **Type**: int
    - **Default**: 30
  
    - **Description**: Seconds each read may take.
  
  
  

  - **status_hosts**
    - **Required**: false
    - **Type**: list
    - **Default**: []
  
    - **Description**: The report entry point's hosts, whose findings it puts together.
  
  
  

  - **status_report_file**
    - **Required**: false
    - **Type**: str
    - **Default**: 
  
    - **Description**: Where the report entry point writes the report, as JSON; empty writes none.
  
  
  

  - **status_color**
    - **Required**: false
    - **Type**: bool
    - **Default**: False
  
    - **Description**: Whether the report entry point shows the report in colour.
  
  
  

  - **status_quiet**
    - **Required**: false
    - **Type**: bool
    - **Default**: False
  
    - **Description**: Whether the report entry point shows no report.
  
  
  

  - **status_read_at**
    - **Required**: false
    - **Type**: str
    - **Default**: 
  
    - **Description**: When the report was read, as its first line says it; empty takes now.
  
  
  

  - **status_now**
    - **Required**: false
    - **Type**: int
    - **Default**: 0
  
    - **Description**: When the report was read, in seconds since the epoch, which its ages count from; 0 takes now.
  
  
  

  - **status_probe_apps**
    - **Required**: false
    - **Type**: bool
    - **Default**: True
  
    - **Description**: Whether the main entry point asks each app's addresses from this machine, a DNS lookup and one GET per URL: the cluster's apps, and an app on a VM of its own.
  
  
  

  - **status_probe_timeout**
    - **Required**: false
    - **Type**: int
    - **Default**: 5
  
    - **Description**: Seconds each lookup and each request may take.
  
  
  

  - **status_admin**
    - **Required**: false
    - **Type**: list
    - **Default**: []
  
    - **Description**: The admin pages the report lists, each {name, url}; tailscale_node marks the page the Proxmox host serves itself, which its tailnet name reaches too.
  
  
  
    
  

  - **status_commands**
    - **Required**: false
    - **Type**: dict
    - **Default**: {}
  
    - **Description**: The commands the report suggests, by purpose: upgrade, resume (with {names}), talos_upgrade, credentials, allowlist, secrets, nas, nas_release, nas_network, backup, backup_target, etcd_snapshot, tailscale, tailscale_login, split_dns. One left out is suggested by text alone.
  
  
  



#### Key: report

**Description**: 
- Puts together what each host of status_hosts found (its status_found), ranks what needs attention, writes the report to status_report_file as JSON, mode 0600, and shows it as text, unless status_quiet.
- Writes the report in a dry run too, since just status runs as one and takes its exit code from the file.
- What each host found includes what its apps' addresses answered, which the main entry point asks.


**Options**:


  - **status_types**
    - **Required**: false
    - **Type**: list
    - **Default**: []
  
    - **Description**: The report's sections to show; empty means every one.
  
  
  

  - **status_setup**
    - **Required**: false
    - **Type**: dict
    - **Default**: {}
  
    - **Description**: What this setup has, as {nas, cluster, home_assistant}, for the report's first line.
  
  
  

  - **status_hosts**
    - **Required**: false
    - **Type**: list
    - **Default**: []
  
    - **Description**: The hosts whose findings the report puts together.
  
  
  

  - **status_report_file**
    - **Required**: false
    - **Type**: str
    - **Default**: 
  
    - **Description**: Where the report is written, as JSON; empty writes none.
  
  
  

  - **status_color**
    - **Required**: false
    - **Type**: bool
    - **Default**: False
  
    - **Description**: Whether the report is shown in colour.
  
  
  

  - **status_quiet**
    - **Required**: false
    - **Type**: bool
    - **Default**: False
  
    - **Description**: Whether no report is shown.
  
  
  

  - **status_read_at**
    - **Required**: false
    - **Type**: str
    - **Default**: 
  
    - **Description**: When the report was read, as its first line says it; empty takes now.
  
  
  

  - **status_now**
    - **Required**: false
    - **Type**: int
    - **Default**: 0
  
    - **Description**: When the report was read, in seconds since the epoch, which its ages count from; 0 takes now.
  
  
  

  - **status_admin**
    - **Required**: false
    - **Type**: list
    - **Default**: []
  
    - **Description**: The admin pages the report lists, each {name, url}; tailscale_node marks the page the Proxmox host serves itself, which its tailnet name reaches too.
  
  
  
    
  

  - **status_commands**
    - **Required**: false
    - **Type**: dict
    - **Default**: {}
  
    - **Description**: The commands the report suggests, by purpose: upgrade, resume (with {names}), talos_upgrade, credentials, allowlist, secrets, nas, nas_release, nas_network, backup, backup_target, etcd_snapshot, tailscale, tailscale_login, split_dns. One left out is suggested by text alone.
  
  
  



</details>




### Defaults

**These are static variables with lower priority**

#### File: defaults/main.yml

| Var          | Type         | Value       |
|--------------|--------------|-------------|
| [status_source](defaults/main.yml#L7)   | str |  |    
| [status_types](defaults/main.yml#L9)   | list | `[]` |    
| [status_vms](defaults/main.yml#L14)   | list | `[]` |    
| [status_setup](defaults/main.yml#L16)   | dict | `{}` |    
| [status_proxmox_node](defaults/main.yml#L18)   | str |  |    
| [status_budget_storages](defaults/main.yml#L20)   | list | `[]` |    
| [status_storage_headroom_gib](defaults/main.yml#L22)   | int | `0` |    
| [status_reboot_required_file](defaults/main.yml#L24)   | str |  |    
| [status_backup_jobs](defaults/main.yml#L28)   | list | `[]` |    
| [status_cluster_kubeconfig](defaults/main.yml#L31)   | str |  |    
| [status_cluster_talosconfig](defaults/main.yml#L32)   | str |  |    
| [status_cluster_server](defaults/main.yml#L33)   | str |  |    
| [status_cluster_nodes](defaults/main.yml#L35)   | list | `[]` |    
| [status_cluster_apps_path](defaults/main.yml#L38)   | str |  |    
| [status_cluster_annotation](defaults/main.yml#L39)   | str |  |    
| [status_cluster_database_label](defaults/main.yml#L40)   | str |  |    
| [status_cluster_volumes](defaults/main.yml#L42)   | dict | `{}` |    
| [status_cluster_settings](defaults/main.yml#L45)   | dict | `{}` |    
| [status_cluster_alertmanager](defaults/main.yml#L47)   | dict | `{}` |    
| [status_cluster_flux_url](defaults/main.yml#L49)   | str |  |    
| [status_cluster_flux_ref](defaults/main.yml#L50)   | str |  |    
| [status_cluster_pins](defaults/main.yml#L52)   | dict | `{}` |    
| [status_cluster_request_timeout](defaults/main.yml#L54)   | int | `10` |    
| [status_cluster_snapshot_dir](defaults/main.yml#L57)   | str |  |    
| [status_nas_state](defaults/main.yml#L60)   | dict | `{}` |    
| [status_nas_release](defaults/main.yml#L61)   | str |  |    
| [status_tailscale_state](defaults/main.yml#L63)   | dict | `{}` |    
| [status_parallel](defaults/main.yml#L65)   | int | `8` |    
| [status_timeout](defaults/main.yml#L66)   | int | `30` |    
| [status_hosts](defaults/main.yml#L69)   | list | `[]` |    
| [status_report_file](defaults/main.yml#L70)   | str |  |    
| [status_color](defaults/main.yml#L71)   | bool | `False` |    
| [status_quiet](defaults/main.yml#L72)   | bool | `False` |    
| [status_read_at](defaults/main.yml#L75)   | str |  |    
| [status_now](defaults/main.yml#L76)   | int | `0` |    
| [status_probe_apps](defaults/main.yml#L79)   | bool | `True` |    
| [status_probe_timeout](defaults/main.yml#L80)   | int | `5` |    
| [status_admin](defaults/main.yml#L83)   | list | `[]` |    
| [status_commands](defaults/main.yml#L91)   | dict | `{}` |    





### Tasks


#### File: tasks/cluster.yml

| Name | Module | Has Conditions |
| ---- | ------ | -------------- |
| cluster ¦ Read the credentials' server and expiry | ansible.builtin.set_fact | True |
| cluster ¦ Check the credentials are this cluster's | ansible.builtin.set_fact | True |
| cluster ¦ Read the cluster, at once | status_read | True |
| cluster ¦ Work out what the cluster says | ansible.builtin.set_fact | False |
| cluster ¦ Ask the apps' addresses | status_probe | True |
| cluster ¦ Say what the cluster says | ansible.builtin.set_fact | False |

#### File: tasks/main.yml

| Name | Module | Has Conditions | Tags |
| ---- | ------ | -------------- | -----|
| Check the role's inputs | ansible.builtin.assert | False | always |
| Read nothing here | ansible.builtin.set_fact | True |  |
| Read the Proxmox host | ansible.builtin.include_tasks | True |  |
| Read the cluster | ansible.builtin.include_tasks | True |  |
| Work out what the NAS says | ansible.builtin.set_fact | True |  |

#### File: tasks/proxmox.yml

| Name | Module | Has Conditions |
| ---- | ------ | -------------- |
| proxmox ¦ Read this machine's Tailscale DNS | status_read | True |
| proxmox ¦ Say what its Tailscale says | ansible.builtin.set_fact | True |
| proxmox ¦ Read the node's name | status_read | True |
| proxmox ¦ Use the node's name | ansible.builtin.set_fact | True |
| proxmox ¦ Read the host, its storages and its VMs, at once | status_read | True |
| proxmox ¦ Read the logs of the backup runs that failed | status_read | True |
| proxmox ¦ Work out what the host says | ansible.builtin.set_fact | False |
| proxmox ¦ Ask Home Assistant's address | status_probe | True |
| proxmox ¦ Say what the host says | ansible.builtin.set_fact | False |

#### File: tasks/report.yml

| Name | Module | Has Conditions |
| ---- | ------ | -------------- |
| report ¦ Put the findings together | ansible.builtin.set_fact | False |
| report ¦ Rank what needs attention | ansible.builtin.set_fact | False |
| report ¦ Write the report | block | True |
| report ¦ Make the report's directory | ansible.builtin.file | True |
| report ¦ Write the report as JSON | ansible.builtin.copy | False |
| report ¦ Show the report | ansible.builtin.debug | True |


## Task Flow Graphs



### Graph for cluster.yml

```mermaid
flowchart TD
Start
classDef block stroke:#3498db,stroke-width:2px;
classDef task stroke:#4b76bb,stroke-width:2px;
classDef includeTasks stroke:#16a085,stroke-width:2px;
classDef importTasks stroke:#34495e,stroke-width:2px;
classDef includeRole stroke:#2980b9,stroke-width:2px;
classDef importRole stroke:#699ba7,stroke-width:2px;
classDef includeVars stroke:#8e44ad,stroke-width:2px;
classDef rescue stroke:#665352,stroke-width:2px;

  Start-->|Task| cluster___Read_the_credentials__server_and_expiry0[cluster   read the credentials  server and expiry<br>When: **cluster  in status reads**]:::task
  cluster___Read_the_credentials__server_and_expiry0-->|Task| cluster___Check_the_credentials_are_this_cluster_s1[cluster   check the credentials are this cluster s<br>When: **cluster  in status reads**]:::task
  cluster___Check_the_credentials_are_this_cluster_s1-->|Task| cluster___Read_the_cluster__at_once2[cluster   read the cluster  at once<br>When: **cluster  in status reads and status cluster<br>unread   length    0**]:::task
  cluster___Read_the_cluster__at_once2-->|Task| cluster___Work_out_what_the_cluster_says3[cluster   work out what the cluster says]:::task
  cluster___Work_out_what_the_cluster_says3-->|Task| cluster___Ask_the_apps__addresses4[cluster   ask the apps  addresses<br>When: **status probe apps and  targets   length   0**]:::task
  cluster___Ask_the_apps__addresses4-->|Task| cluster___Say_what_the_cluster_says5[cluster   say what the cluster says]:::task
  cluster___Say_what_the_cluster_says5-->End
```


### Graph for main.yml

```mermaid
flowchart TD
Start
classDef block stroke:#3498db,stroke-width:2px;
classDef task stroke:#4b76bb,stroke-width:2px;
classDef includeTasks stroke:#16a085,stroke-width:2px;
classDef importTasks stroke:#34495e,stroke-width:2px;
classDef includeRole stroke:#2980b9,stroke-width:2px;
classDef importRole stroke:#699ba7,stroke-width:2px;
classDef includeVars stroke:#8e44ad,stroke-width:2px;
classDef rescue stroke:#665352,stroke-width:2px;

  Start-->|Task| Check_the_role_s_inputs0[check the role s inputs]:::task
  Check_the_role_s_inputs0-->|Task| Read_nothing_here1[read nothing here<br>When: **status source   status host reads status types   <br>length    0**]:::task
  Read_nothing_here1-->|Include task| Read_the_Proxmox_host_proxmox_yml_2[read the proxmox host<br>When: **status source     proxmox  and status reads  <br>length   0**<br>include_task: proxmox yml]:::includeTasks
  Read_the_Proxmox_host_proxmox_yml_2-->|Include task| Read_the_cluster_cluster_yml_3[read the cluster<br>When: **status source     cluster  and status reads  <br>length   0**<br>include_task: cluster yml]:::includeTasks
  Read_the_cluster_cluster_yml_3-->|Task| Work_out_what_the_NAS_says4[work out what the nas says<br>When: **status source     nas  and status source   status<br>host reads status types    length   0**]:::task
  Work_out_what_the_NAS_says4-->End
```


### Graph for proxmox.yml

```mermaid
flowchart TD
Start
classDef block stroke:#3498db,stroke-width:2px;
classDef task stroke:#4b76bb,stroke-width:2px;
classDef includeTasks stroke:#16a085,stroke-width:2px;
classDef importTasks stroke:#34495e,stroke-width:2px;
classDef includeRole stroke:#2980b9,stroke-width:2px;
classDef importRole stroke:#699ba7,stroke-width:2px;
classDef includeVars stroke:#8e44ad,stroke-width:2px;
classDef rescue stroke:#665352,stroke-width:2px;

  Start-->|Task| proxmox___Read_this_machine_s_Tailscale_DNS0[proxmox   read this machine s tailscale dns<br>When: **tailscale  in status reads and status tailscale<br>state routes   default       selectattr <br>advertised     list   length   0**]:::task
  proxmox___Read_this_machine_s_Tailscale_DNS0-->|Task| proxmox___Say_what_its_Tailscale_says1[proxmox   say what its tailscale says<br>When: **tailscale  in status reads**]:::task
  proxmox___Say_what_its_Tailscale_says1-->|Task| proxmox___Read_the_node_s_name2[proxmox   read the node s name<br>When: **proxmox  in status reads and status proxmox node <br> length    0**]:::task
  proxmox___Read_the_node_s_name2-->|Task| proxmox___Use_the_node_s_name3[proxmox   use the node s name<br>When: **proxmox  in status reads**]:::task
  proxmox___Use_the_node_s_name3-->|Task| proxmox___Read_the_host__its_storages_and_its_VMs__at_once4[proxmox   read the host  its storages and its vms <br>at once<br>When: **proxmox  in status reads and not  status proxmox<br>name unreachable   default false**]:::task
  proxmox___Read_the_host__its_storages_and_its_VMs__at_once4-->|Task| proxmox___Read_the_logs_of_the_backup_runs_that_failed5[proxmox   read the logs of the backup runs that<br>failed<br>When: **runs   length   0**]:::task
  proxmox___Read_the_logs_of_the_backup_runs_that_failed5-->|Task| proxmox___Work_out_what_the_host_says6[proxmox   work out what the host says]:::task
  proxmox___Work_out_what_the_host_says6-->|Task| proxmox___Ask_Home_Assistant_s_address7[proxmox   ask home assistant s address<br>When: **status probe apps and  targets   length   0**]:::task
  proxmox___Ask_Home_Assistant_s_address7-->|Task| proxmox___Say_what_the_host_says8[proxmox   say what the host says]:::task
  proxmox___Say_what_the_host_says8-->End
```


### Graph for report.yml

```mermaid
flowchart TD
Start
classDef block stroke:#3498db,stroke-width:2px;
classDef task stroke:#4b76bb,stroke-width:2px;
classDef includeTasks stroke:#16a085,stroke-width:2px;
classDef importTasks stroke:#34495e,stroke-width:2px;
classDef includeRole stroke:#2980b9,stroke-width:2px;
classDef importRole stroke:#699ba7,stroke-width:2px;
classDef includeVars stroke:#8e44ad,stroke-width:2px;
classDef rescue stroke:#665352,stroke-width:2px;

  Start-->|Task| report___Put_the_findings_together0[report   put the findings together]:::task
  report___Put_the_findings_together0-->|Task| report___Rank_what_needs_attention1[report   rank what needs attention]:::task
  report___Rank_what_needs_attention1-->|Block Start| report___Write_the_report2_block_start_0[[report   write the report<br>When: **status report file   length   0**]]:::block
  report___Write_the_report2_block_start_0-->|Task| report___Make_the_report_s_directory0[report   make the report s directory<br>When: **status report file   dirname is not directory**]:::task
  report___Make_the_report_s_directory0-->|Task| report___Write_the_report_as_JSON1[report   write the report as json]:::task
  report___Write_the_report_as_JSON1-.->|End of Block| report___Write_the_report2_block_start_0
  report___Write_the_report_as_JSON1-->|Task| report___Show_the_report3[report   show the report<br>When: **not status quiet**]:::task
  report___Show_the_report3-->End
```





## Author Information
homeserver contributors

#### License

none

#### Minimum Ansible Version

2.21

#### Platforms

- **GenericLinux**: ['all']


#### Dependencies

No dependencies specified.
<!-- DOCSIBLE END -->
