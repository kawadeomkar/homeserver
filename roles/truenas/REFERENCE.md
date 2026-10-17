<!-- DOCSIBLE START -->

# 📃 Role overview

## truenas



Description: Configure TrueNAS Community Edition through its API - address, pools, datasets, NFS






<details>
<summary><b>🧩 Argument Specifications in meta/argument_specs</b></summary>

#### Key: main

**Description**: 
- Runs on the controller and talks to the NAS's JSON-RPC WebSocket API with an API key; nothing goes over SSH.
- Stages, each under its own tag - truenas_network (network settings, static address), truenas_pools (import or create, auto-trim, scrub), truenas_system (general settings, NTP), truenas_datasets, truenas_nfs (service settings, shares), truenas_services.
- The defaults change nothing. Every stage acts only on what its variables declare, and an empty list or dict (or a null value) leaves that part of the NAS alone.
- Never destroys, wipes, exports or upgrades a pool, never deletes a dataset, and refuses an NFS share without a host or network restriction.


**Options**:


  - **truenas_api_hosts**
    - **Required**: false
    - **Type**: list
    - **Default**: []
  
    - **Description**: Addresses to try in order; the first that answers is used.
  
  
  

  - **truenas_api_port**
    - **Required**: false
    - **Type**: int
    - **Default**: 443
  
    - **Description**: HTTPS port of the TrueNAS web server.
  
  
  

  - **truenas_api_key**
    - **Required**: false
    - **Type**: str
    - **Default**: 
  
    - **Description**: API key. Keep it in an ansible-vault file.
  
  
  

  - **truenas_validate_certs**
    - **Required**: false
    - **Type**: bool
    - **Default**: False
    - **Description**: 
  
  
  
  

  - **truenas_api_cert_sha256**
    - **Required**: false
    - **Type**: str
    - **Default**: 
  
    - **Description**: SHA-256 fingerprint of the NAS's certificate; when set, the API key goes only to that server.
  
  
  

  - **truenas_api_timeout**
    - **Required**: false
    - **Type**: int
    - **Default**: 120
  
    - **Description**: Seconds to wait for one API answer.
  
  
  

  - **truenas_api_connect_wait**
    - **Required**: false
    - **Type**: int
    - **Default**: 60
  
    - **Description**: Seconds to keep retrying a refused connection.
  
  
  

  - **truenas_api_job_timeout**
    - **Required**: false
    - **Type**: int
    - **Default**: 1800
  
    - **Description**: Seconds to wait for a job such as a pool import.
  
  
  

  - **truenas_api_login_wait**
    - **Required**: false
    - **Type**: int
    - **Default**: 75
  
    - **Description**: Seconds to wait out TrueNAS's login rate limit (20 logins per 60 s per client).
  
  
  

  - **truenas_supported_version**
    - **Required**: false
    - **Type**: str
    - **Default**: 25.10
  
    - **Description**: Major.minor release the role was written against.
  
  
  

  - **truenas_network_mode**
    - **Required**: false
    - **Type**: str
    - **Default**: dhcp
  
    - **Description**: static gives the interface truenas_static_address; dhcp leaves it alone.
  
      - **Choices**:
    
          - static
    
          - dhcp
    
  
  
  

  - **truenas_static_address**
    - **Required**: false
    - **Type**: str
    - **Default**: 
  
    - **Description**: Static IPv4 address in CIDR form.
  
  
  

  - **truenas_gateway**
    - **Required**: false
    - **Type**: str
    - **Default**: 
    - **Description**: 
  
  
  
  

  - **truenas_nameservers**
    - **Required**: false
    - **Type**: list
    - **Default**: []
  
    - **Description**: Up to three.
  
  
  

  - **truenas_interface**
    - **Required**: false
    - **Type**: str
    - **Default**: 
  
    - **Description**: Interface to configure. Empty means the one carrying the API connection.
  
  
  

  - **truenas_ipv6_auto**
    - **Required**: false
    - **Type**: bool
    - **Default**: none
  
    - **Description**: IPv6 autoconfiguration on the interface. Unset or null leaves it alone.
  
  
  

  - **truenas_checkin_timeout**
    - **Required**: false
    - **Type**: int
    - **Default**: 90
  
    - **Description**: Seconds TrueNAS waits for the new address to be confirmed before rolling back.
  
  
  

  - **truenas_static_address_conflict_check**
    - **Required**: false
    - **Type**: bool
    - **Default**: True
  
    - **Description**: Refuse a static address that another device already answers on.
  
  
  

  - **truenas_network_settings**
    - **Required**: false
    - **Type**: dict
    - **Default**: {}
  
    - **Description**: Fields for network.configuration.update, e.g. hostname and domain.
  
  
  

  - **truenas_pools**
    - **Required**: false
    - **Type**: list
    - **Default**: []
  
    - **Description**: Pools, each with name and (for creation) layout; optional autotrim, scrub, guid.
  
  
  
    
  

  - **truenas_pool_allow_create**
    - **Required**: false
    - **Type**: bool
    - **Default**: False
    - **Description**: 
  
  
  
  

  - **truenas_pool_disks**
    - **Required**: false
    - **Type**: dict
    - **Default**: {}
  
    - **Description**: Pool name to list of disk serials, used only to create a pool.
  
  
  

  - **truenas_pool_guids**
    - **Required**: false
    - **Type**: dict
    - **Default**: {}
  
    - **Description**: Pool name to GUID; a pool with another GUID is refused.
  
  
  

  - **truenas_pool_autotrim**
    - **Required**: false
    - **Type**: str
    - **Default**: 
  
    - **Description**: Auto-trim for pools that do not set their own. Empty leaves it alone.
  
      - **Choices**:
    
          - 
    
          - ON
    
          - OFF
    
  
  
  

  - **truenas_pool_scrub**
    - **Required**: false
    - **Type**: dict
    - **Default**: none
  
    - **Description**: Scrub task for pools that do not set their own. Unset or null leaves it alone.
  
  
  

  - **truenas_system_dataset_pool**
    - **Required**: false
    - **Type**: str
    - **Default**: boot-pool
  
    - **Description**: Pool to pin the system dataset to before importing pools (only when truenas_pools is set). Empty disables the pin.
  
  
  

  - **truenas_general_settings**
    - **Required**: false
    - **Type**: dict
    - **Default**: {}
  
    - **Description**: Fields for system.general.update, e.g. ui_httpsredirect and timezone.
  
  
  

  - **truenas_ui_restart_delay**
    - **Required**: false
    - **Type**: int
    - **Default**: 3
  
    - **Description**: Seconds after a general-settings update before the web server restarts.
  
  
  

  - **truenas_ui_restart_wait**
    - **Required**: false
    - **Type**: int
    - **Default**: 10
  
    - **Description**: Seconds to wait for that restart before the next call.
  
  
  

  - **truenas_ntp_servers**
    - **Required**: false
    - **Type**: list
    - **Default**: []
  
    - **Description**: NTP servers, each with address and optionally iburst, burst, prefer, minpoll, maxpoll.
  
  
  
    
  

  - **truenas_ntp_server_defaults**
    - **Required**: false
    - **Type**: dict
    - **Default**: {}
  
    - **Description**: Fields laid under every NTP server entry.
  
  
  

  - **truenas_datasets**
    - **Required**: false
    - **Type**: list
    - **Default**: []
  
    - **Description**: Datasets, each with name and properties as pool.dataset.create takes them.
  
  
  
    
  

  - **truenas_nfs_settings**
    - **Required**: false
    - **Type**: dict
    - **Default**: {}
  
    - **Description**: Fields for nfs.update, e.g. protocols.
  
  
  

  - **truenas_nfs_shares**
    - **Required**: false
    - **Type**: list
    - **Default**: []
  
    - **Description**: Shares, each with path and the fields of sharing.nfs.create.
  
  
  
    
  

  - **truenas_nfs_share_defaults**
    - **Required**: false
    - **Type**: dict
    - **Default**: {}
  
    - **Description**: Fields laid under every share.
  
  
  

  - **truenas_services**
    - **Required**: false
    - **Type**: dict
    - **Default**: {}
  
    - **Description**: Service name to {enabled, state}; state is started or stopped.
  
  
  



</details>




### Defaults

**These are static variables with lower priority**

#### File: defaults/main.yml

| Var          | Type         | Value       |
|--------------|--------------|-------------|
| [truenas_api_hosts](defaults/main.yml#L9)   | list | `[]` |    
| [truenas_api_port](defaults/main.yml#L11)   | int | `443` |    
| [truenas_api_key](defaults/main.yml#L12)   | str |  |    
| [truenas_validate_certs](defaults/main.yml#L13)   | bool | `False` |    
| [truenas_api_cert_sha256](defaults/main.yml#L17)   | str |  |    
| [truenas_api_timeout](defaults/main.yml#L20)   | int | `120` |    
| [truenas_api_connect_wait](defaults/main.yml#L21)   | int | `60` |    
| [truenas_api_job_timeout](defaults/main.yml#L22)   | int | `1800` |    
| [truenas_api_login_wait](defaults/main.yml#L23)   | int | `75` |    
| [truenas_supported_version](defaults/main.yml#L25)   | str | `25.10` |    
| [truenas_network_mode](defaults/main.yml#L29)   | str | `dhcp` |    
| [truenas_static_address](defaults/main.yml#L30)   | str |  |    
| [truenas_gateway](defaults/main.yml#L31)   | str |  |    
| [truenas_nameservers](defaults/main.yml#L32)   | list | `[]` |    
| [truenas_interface](defaults/main.yml#L34)   | str |  |    
| [truenas_ipv6_auto](defaults/main.yml#L36)   | NoneType | `None` |    
| [truenas_checkin_timeout](defaults/main.yml#L38)   | int | `90` |    
| [truenas_static_address_conflict_check](defaults/main.yml#L41)   | bool | `True` |    
| [truenas_network_settings](defaults/main.yml#L43)   | dict | `{}` |    
| [truenas_pools](defaults/main.yml#L49)   | list | `[]` |    
| [truenas_pool_allow_create](defaults/main.yml#L50)   | bool | `False` |    
| [truenas_pool_disks](defaults/main.yml#L52)   | dict | `{}` |    
| [truenas_pool_guids](defaults/main.yml#L53)   | dict | `{}` |    
| [truenas_pool_autotrim](defaults/main.yml#L55)   | str |  |    
| [truenas_pool_scrub](defaults/main.yml#L56)   | NoneType | `None` |    
| [truenas_system_dataset_pool](defaults/main.yml#L60)   | str | `boot-pool` |    
| [truenas_general_settings](defaults/main.yml#L64)   | dict | `{}` |    
| [truenas_ui_restart_delay](defaults/main.yml#L67)   | int | `3` |    
| [truenas_ui_restart_wait](defaults/main.yml#L68)   | int | `10` |    
| [truenas_ntp_servers](defaults/main.yml#L70)   | list | `[]` |    
| [truenas_ntp_server_defaults](defaults/main.yml#L71)   | dict | `{}` |    
| [truenas_datasets](defaults/main.yml#L76)   | list | `[]` |    
| [truenas_nfs_settings](defaults/main.yml#L80)   | dict | `{}` |    
| [truenas_nfs_shares](defaults/main.yml#L83)   | list | `[]` |    
| [truenas_nfs_share_defaults](defaults/main.yml#L85)   | dict | `{}` |    
| [truenas_services](defaults/main.yml#L89)   | dict | `{}` |    





### Tasks


#### File: tasks/connect.yml

| Name | Module | Has Conditions |
| ---- | ------ | -------------- |
| connect ¦ Connect to the NAS and read its state | truenas_info | False |
| connect ¦ Use the address that answered | ansible.builtin.set_fact | False |
| connect ¦ Check the NAS is ready and on the supported release | ansible.builtin.assert | False |
| connect ¦ Check no interface change is waiting to be committed | ansible.builtin.assert | False |
| connect ¦ Report the NAS's state | ansible.builtin.debug | False |

#### File: tasks/datasets.yml

| Name | Module | Has Conditions |
| ---- | ------ | -------------- |
| datasets ¦ Create the datasets and set their properties | truenas_dataset | True |

#### File: tasks/main.yml

| Name | Module | Has Conditions | Tags |
| ---- | ------ | -------------- | -----|
| Check the role's inputs | ansible.builtin.import_tasks | False | always |
| Configure the NAS through its API | block | False |  |
| Connect and report the NAS's state | ansible.builtin.import_tasks | False |  |
| Network settings and static address | ansible.builtin.import_tasks | False |  |
| Data pools | ansible.builtin.import_tasks | False |  |
| System settings | ansible.builtin.import_tasks | False |  |
| Datasets | ansible.builtin.import_tasks | False |  |
| NFS settings and shares | ansible.builtin.import_tasks | False |  |
| Services | ansible.builtin.import_tasks | False |  |

#### File: tasks/network.yml

| Name | Module | Has Conditions |
| ---- | ------ | -------------- |
| network ¦ Apply the network settings | truenas_config | True |
| network ¦ Give the NAS its static address | truenas_interface | True |
| network ¦ Use the static address from here on | ansible.builtin.set_fact | True |

#### File: tasks/nfs.yml

| Name | Module | Has Conditions |
| ---- | ------ | -------------- |
| nfs ¦ Apply the NFS service settings | truenas_config | True |
| nfs ¦ Create or update the NFS shares | truenas_resource | True |

#### File: tasks/pools.yml

| Name | Module | Has Conditions |
| ---- | ------ | -------------- |
| pools ¦ Pin the system dataset before any pool is imported | truenas_config | True |
| pools ¦ Import or create the pools | truenas_pool | True |
| pools ¦ Check the system dataset is still where it was pinned | truenas_config | True |

#### File: tasks/services.yml

| Name | Module | Has Conditions |
| ---- | ------ | -------------- |
| services ¦ Enable, disable, start or stop services | truenas_service | True |

#### File: tasks/system.yml

| Name | Module | Has Conditions |
| ---- | ------ | -------------- |
| system ¦ Apply the general settings | truenas_config | True |
| system ¦ Wait for the web server to restart | ansible.builtin.pause | True |
| system ¦ Set the NTP servers | truenas_resource | True |

#### File: tasks/validate.yml

| Name | Module | Has Conditions |
| ---- | ------ | -------------- |
| validate ¦ Check the connection settings | ansible.builtin.assert | False |
| validate ¦ Check a static address has what it needs | ansible.builtin.assert | True |
| validate ¦ Refuse an NFS share open to every host | ansible.builtin.assert | False |


## Task Flow Graphs



### Graph for connect.yml

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

  Start-->|Task| connect___Connect_to_the_NAS_and_read_its_state0[connect   connect to the nas and read its state]:::task
  connect___Connect_to_the_NAS_and_read_its_state0-->|Task| connect___Use_the_address_that_answered1[connect   use the address that answered]:::task
  connect___Use_the_address_that_answered1-->|Task| connect___Check_the_NAS_is_ready_and_on_the_supported_release2[connect   check the nas is ready and on the<br>supported release]:::task
  connect___Check_the_NAS_is_ready_and_on_the_supported_release2-->|Task| connect___Check_no_interface_change_is_waiting_to_be_committed3[connect   check no interface change is waiting to<br>be committed]:::task
  connect___Check_no_interface_change_is_waiting_to_be_committed3-->|Task| connect___Report_the_NAS_s_state4[connect   report the nas s state]:::task
  connect___Report_the_NAS_s_state4-->End
```


### Graph for datasets.yml

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

  Start-->|Task| datasets___Create_the_datasets_and_set_their_properties0[datasets   create the datasets and set their<br>properties<br>When: **truenas datasets   length   0**]:::task
  datasets___Create_the_datasets_and_set_their_properties0-->End
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

  Start-->|Import task| Check_the_role_s_inputs_validate_yml_0[/check the role s inputs<br>import_task: validate yml/]:::importTasks
  Check_the_role_s_inputs_validate_yml_0-->|Block Start| Configure_the_NAS_through_its_API1_block_start_0[[configure the nas through its api]]:::block
  Configure_the_NAS_through_its_API1_block_start_0-->|Import task| Connect_and_report_the_NAS_s_state_connect_yml_0[/connect and report the nas s state<br>import_task: connect yml/]:::importTasks
  Connect_and_report_the_NAS_s_state_connect_yml_0-->|Import task| Network_settings_and_static_address_network_yml_1[/network settings and static address<br>import_task: network yml/]:::importTasks
  Network_settings_and_static_address_network_yml_1-->|Import task| Data_pools_pools_yml_2[/data pools<br>import_task: pools yml/]:::importTasks
  Data_pools_pools_yml_2-->|Import task| System_settings_system_yml_3[/system settings<br>import_task: system yml/]:::importTasks
  System_settings_system_yml_3-->|Import task| Datasets_datasets_yml_4[/datasets<br>import_task: datasets yml/]:::importTasks
  Datasets_datasets_yml_4-->|Import task| NFS_settings_and_shares_nfs_yml_5[/nfs settings and shares<br>import_task: nfs yml/]:::importTasks
  NFS_settings_and_shares_nfs_yml_5-->|Import task| Services_services_yml_6[/services<br>import_task: services yml/]:::importTasks
  Services_services_yml_6-.->|End of Block| Configure_the_NAS_through_its_API1_block_start_0
  Services_services_yml_6-->End
```


### Graph for network.yml

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

  Start-->|Task| network___Apply_the_network_settings0[network   apply the network settings<br>When: **truenas network settings   length   0**]:::task
  network___Apply_the_network_settings0-->|Task| network___Give_the_NAS_its_static_address1[network   give the nas its static address<br>When: **truenas network mode     static**]:::task
  network___Give_the_NAS_its_static_address1-->|Task| network___Use_the_static_address_from_here_on2[network   use the static address from here on<br>When: **truenas network is not skipped**]:::task
  network___Use_the_static_address_from_here_on2-->End
```


### Graph for nfs.yml

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

  Start-->|Task| nfs___Apply_the_NFS_service_settings0[nfs   apply the nfs service settings<br>When: **truenas nfs settings   length   0**]:::task
  nfs___Apply_the_NFS_service_settings0-->|Task| nfs___Create_or_update_the_NFS_shares1[nfs   create or update the nfs shares<br>When: **truenas nfs shares   length   0**]:::task
  nfs___Create_or_update_the_NFS_shares1-->End
```


### Graph for pools.yml

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

  Start-->|Task| pools___Pin_the_system_dataset_before_any_pool_is_imported0[pools   pin the system dataset before any pool is<br>imported<br>When: **truenas system dataset pool   default     true   <br>length   0 and truenas pools   length   0**]:::task
  pools___Pin_the_system_dataset_before_any_pool_is_imported0-->|Task| pools___Import_or_create_the_pools1[pools   import or create the pools<br>When: **truenas pools   length   0**]:::task
  pools___Import_or_create_the_pools1-->|Task| pools___Check_the_system_dataset_is_still_where_it_was_pinned2[pools   check the system dataset is still where it<br>was pinned<br>When: **truenas system dataset pool   default     true   <br>length   0 and truenas pools   length   0**]:::task
  pools___Check_the_system_dataset_is_still_where_it_was_pinned2-->End
```


### Graph for services.yml

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

  Start-->|Task| services___Enable__disable__start_or_stop_services0[services   enable  disable  start or stop services<br>When: **truenas services   length   0**]:::task
  services___Enable__disable__start_or_stop_services0-->End
```


### Graph for system.yml

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

  Start-->|Task| system___Apply_the_general_settings0[system   apply the general settings<br>When: **truenas general settings   length   0**]:::task
  system___Apply_the_general_settings0-->|Task| system___Wait_for_the_web_server_to_restart1[system   wait for the web server to restart<br>When: **truenas general is changed and not ansible check<br>mode and truenas ui restart wait   int   0**]:::task
  system___Wait_for_the_web_server_to_restart1-->|Task| system___Set_the_NTP_servers2[system   set the ntp servers<br>When: **truenas ntp servers   length   0**]:::task
  system___Set_the_NTP_servers2-->End
```


### Graph for validate.yml

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

  Start-->|Task| validate___Check_the_connection_settings0[validate   check the connection settings]:::task
  validate___Check_the_connection_settings0-->|Task| validate___Check_a_static_address_has_what_it_needs1[validate   check a static address has what it<br>needs<br>When: **truenas network mode     static**]:::task
  validate___Check_a_static_address_has_what_it_needs1-->|Task| validate___Refuse_an_NFS_share_open_to_every_host2[validate   refuse an nfs share open to every host]:::task
  validate___Refuse_an_NFS_share_open_to_every_host2-->End
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
