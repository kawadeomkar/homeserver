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
  
    - **Description**: The annotation in which make suspend records an app's replicas, which marks it suspended.
  
  
  

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
  
  
  

  - **status_probe_apps**
    - **Required**: false
    - **Type**: bool
    - **Default**: True
  
    - **Description**: Whether the report asks each app's addresses from this machine, a DNS lookup and one GET per URL.
  
  
  

  - **status_probe_timeout**
    - **Required**: false
    - **Type**: int
    - **Default**: 5
  
    - **Description**: Seconds each lookup and each request may take.
  
  
  

  - **status_admin**
    - **Required**: false
    - **Type**: list
    - **Default**: []
  
    - **Description**: The admin pages the report lists, each {name, url}.
  
  
  
    
  

  - **status_commands**
    - **Required**: false
    - **Type**: dict
    - **Default**: {}
  
    - **Description**: The commands the report suggests, by purpose: upgrade, resume (with {names}), talos_upgrade, credentials, allowlist, secrets, nas, nas_release, nas_network. One left out is suggested by text alone.
  
  
  



#### Key: report

**Description**: 
- Puts together what each host of status_hosts found (its status_found), ranks what needs attention, writes the report to status_report_file as JSON, mode 0600, and shows it as text, unless status_quiet.
- Writes the report in a dry run too, since make status runs as one and takes its exit code from the file.
- Unless status_probe_apps is off, first asks each app's addresses from this machine with the role's module status_probe, checking no certificate and sending no credentials.


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
  
  
  

  - **status_probe_apps**
    - **Required**: false
    - **Type**: bool
    - **Default**: True
  
    - **Description**: Whether the report asks each app's addresses from this machine, a DNS lookup and one GET per URL.
  
  
  

  - **status_probe_timeout**
    - **Required**: false
    - **Type**: int
    - **Default**: 5
  
    - **Description**: Seconds each lookup and each request may take.
  
  
  

  - **status_admin**
    - **Required**: false
    - **Type**: list
    - **Default**: []
  
    - **Description**: The admin pages the report lists, each {name, url}.
  
  
  
    
  

  - **status_commands**
    - **Required**: false
    - **Type**: dict
    - **Default**: {}
  
    - **Description**: The commands the report suggests, by purpose: upgrade, resume (with {names}), talos_upgrade, credentials, allowlist, secrets, nas, nas_release, nas_network. One left out is suggested by text alone.
  
  
  



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
| [status_cluster_kubeconfig](defaults/main.yml#L27)   | str |  |    
| [status_cluster_talosconfig](defaults/main.yml#L28)   | str |  |    
| [status_cluster_server](defaults/main.yml#L29)   | str |  |    
| [status_cluster_nodes](defaults/main.yml#L31)   | list | `[]` |    
| [status_cluster_apps_path](defaults/main.yml#L34)   | str |  |    
| [status_cluster_annotation](defaults/main.yml#L35)   | str |  |    
| [status_cluster_database_label](defaults/main.yml#L36)   | str |  |    
| [status_cluster_volumes](defaults/main.yml#L38)   | dict | `{}` |    
| [status_cluster_settings](defaults/main.yml#L41)   | dict | `{}` |    
| [status_cluster_alertmanager](defaults/main.yml#L43)   | dict | `{}` |    
| [status_cluster_flux_url](defaults/main.yml#L45)   | str |  |    
| [status_cluster_flux_ref](defaults/main.yml#L46)   | str |  |    
| [status_cluster_pins](defaults/main.yml#L48)   | dict | `{}` |    
| [status_cluster_request_timeout](defaults/main.yml#L50)   | int | `10` |    
| [status_nas_state](defaults/main.yml#L53)   | dict | `{}` |    
| [status_nas_release](defaults/main.yml#L54)   | str |  |    
| [status_parallel](defaults/main.yml#L56)   | int | `8` |    
| [status_timeout](defaults/main.yml#L57)   | int | `30` |    
| [status_hosts](defaults/main.yml#L60)   | list | `[]` |    
| [status_report_file](defaults/main.yml#L61)   | str |  |    
| [status_color](defaults/main.yml#L62)   | bool | `False` |    
| [status_quiet](defaults/main.yml#L63)   | bool | `False` |    
| [status_probe_apps](defaults/main.yml#L66)   | bool | `True` |    
| [status_probe_timeout](defaults/main.yml#L67)   | int | `5` |    
| [status_admin](defaults/main.yml#L69)   | list | `[]` |    
| [status_commands](defaults/main.yml#L74)   | dict | `{}` |    





### Tasks


#### File: tasks/cluster.yml

| Name | Module | Has Conditions |
| ---- | ------ | -------------- |
| cluster ¦ Read the credentials' server and expiry | ansible.builtin.set_fact | False |
| cluster ¦ Check the credentials are this cluster's | ansible.builtin.set_fact | False |
| cluster ¦ Read the cluster, at once | status_read | True |
| cluster ¦ Work out what the cluster says | ansible.builtin.set_fact | False |

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
| proxmox ¦ Read the node's name | status_read | True |
| proxmox ¦ Use the node's name | ansible.builtin.set_fact | False |
| proxmox ¦ Read the host, its storages and its VMs, at once | status_read | True |
| proxmox ¦ Work out what the host says | ansible.builtin.set_fact | False |

#### File: tasks/report.yml

| Name | Module | Has Conditions |
| ---- | ------ | -------------- |
| report ¦ Put the findings together | ansible.builtin.set_fact | False |
| report ¦ Ask the apps' addresses | status_probe | True |
| report ¦ Rank what needs attention | ansible.builtin.set_fact | False |
| report ¦ Write the report | block | True |
| report ¦ Make the report's directory | ansible.builtin.file | False |
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

  Start-->|Task| cluster___Read_the_credentials__server_and_expiry0[cluster   read the credentials  server and expiry]:::task
  cluster___Read_the_credentials__server_and_expiry0-->|Task| cluster___Check_the_credentials_are_this_cluster_s1[cluster   check the credentials are this cluster s]:::task
  cluster___Check_the_credentials_are_this_cluster_s1-->|Task| cluster___Read_the_cluster__at_once2[cluster   read the cluster  at once<br>When: **status cluster unread   length    0**]:::task
  cluster___Read_the_cluster__at_once2-->|Task| cluster___Work_out_what_the_cluster_says3[cluster   work out what the cluster says]:::task
  cluster___Work_out_what_the_cluster_says3-->End
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
  Check_the_role_s_inputs0-->|Task| Read_nothing_here1[read nothing here<br>When: **status source   length    0**]:::task
  Read_nothing_here1-->|Include task| Read_the_Proxmox_host_proxmox_yml_2[read the proxmox host<br>When: **status source     proxmox**<br>include_task: proxmox yml]:::includeTasks
  Read_the_Proxmox_host_proxmox_yml_2-->|Include task| Read_the_cluster_cluster_yml_3[read the cluster<br>When: **status source     cluster**<br>include_task: cluster yml]:::includeTasks
  Read_the_cluster_cluster_yml_3-->|Task| Work_out_what_the_NAS_says4[work out what the nas says<br>When: **status source     nas**]:::task
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

  Start-->|Task| proxmox___Read_the_node_s_name0[proxmox   read the node s name<br>When: **status proxmox node   length    0**]:::task
  proxmox___Read_the_node_s_name0-->|Task| proxmox___Use_the_node_s_name1[proxmox   use the node s name]:::task
  proxmox___Use_the_node_s_name1-->|Task| proxmox___Read_the_host__its_storages_and_its_VMs__at_once2[proxmox   read the host  its storages and its vms <br>at once<br>When: **not  status proxmox name unreachable   default<br>false**]:::task
  proxmox___Read_the_host__its_storages_and_its_VMs__at_once2-->|Task| proxmox___Work_out_what_the_host_says3[proxmox   work out what the host says]:::task
  proxmox___Work_out_what_the_host_says3-->End
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
  report___Put_the_findings_together0-->|Task| report___Ask_the_apps__addresses1[report   ask the apps  addresses<br>When: **status probe apps and  targets   length   0**]:::task
  report___Ask_the_apps__addresses1-->|Task| report___Rank_what_needs_attention2[report   rank what needs attention]:::task
  report___Rank_what_needs_attention2-->|Block Start| report___Write_the_report3_block_start_0[[report   write the report<br>When: **status report file   length   0**]]:::block
  report___Write_the_report3_block_start_0-->|Task| report___Make_the_report_s_directory0[report   make the report s directory]:::task
  report___Make_the_report_s_directory0-->|Task| report___Write_the_report_as_JSON1[report   write the report as json]:::task
  report___Write_the_report_as_JSON1-.->|End of Block| report___Write_the_report3_block_start_0
  report___Write_the_report_as_JSON1-->|Task| report___Show_the_report4[report   show the report<br>When: **not status quiet**]:::task
  report___Show_the_report4-->End
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
