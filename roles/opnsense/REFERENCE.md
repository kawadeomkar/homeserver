<!-- DOCSIBLE START -->

# 📃 Role overview

## opnsense



Description: Configure the OPNsense router through its API - read-only for now






<details>
<summary><b>🧩 Argument Specifications in meta/argument_specs</b></summary>

#### Key: main

**Description**: 
- Runs on the controller and calls the router's HTTPS API with an API key and secret; nothing goes over SSH.
- Today it only reads - it connects, checks the router runs a release it supports, and reports that release and the plugins installed.
- The key and secret go only to a server presenting the pinned certificate.


**Options**:


  - **opnsense_api_host**
    - **Required**: false
    - **Type**: str
    - **Default**: 
  
    - **Description**: The router's address.
  
  
  

  - **opnsense_api_port**
    - **Required**: false
    - **Type**: int
    - **Default**: 443
  
    - **Description**: HTTPS port of the router's web server.
  
  
  

  - **opnsense_api_key**
    - **Required**: false
    - **Type**: str
    - **Default**: 
  
    - **Description**: The key of the API key pair. Keep it in an ansible-vault file.
  
  
  

  - **opnsense_api_secret**
    - **Required**: false
    - **Type**: str
    - **Default**: 
  
    - **Description**: The secret of the API key pair. Keep it in an ansible-vault file.
  
  
  

  - **opnsense_validate_certs**
    - **Required**: false
    - **Type**: bool
    - **Default**: False
  
    - **Description**: Verify the router's certificate against the controller's trusted authorities.
  
  
  

  - **opnsense_api_cert_sha256**
    - **Required**: false
    - **Type**: str
    - **Default**: 
  
    - **Description**: SHA-256 fingerprint of the router's certificate; the key and secret go only to that server. Required while opnsense_validate_certs is off.
  
  
  

  - **opnsense_api_timeout**
    - **Required**: false
    - **Type**: int
    - **Default**: 60
  
    - **Description**: Seconds to wait for the connection and for each answer.
  
  
  

  - **opnsense_min_version**
    - **Required**: false
    - **Type**: str
    - **Default**: 26.7.2
  
    - **Description**: The oldest release the role runs against; an older router fails the run.
  
  
  



</details>




### Defaults

**These are static variables with lower priority**

#### File: defaults/main.yml

| Var          | Type         | Value       |
|--------------|--------------|-------------|
| [opnsense_api_host](defaults/main.yml#L7)   | str |  |    
| [opnsense_api_port](defaults/main.yml#L9)   | int | `443` |    
| [opnsense_api_key](defaults/main.yml#L11)   | str |  |    
| [opnsense_api_secret](defaults/main.yml#L12)   | str |  |    
| [opnsense_validate_certs](defaults/main.yml#L13)   | bool | `False` |    
| [opnsense_api_cert_sha256](defaults/main.yml#L17)   | str |  |    
| [opnsense_api_timeout](defaults/main.yml#L19)   | int | `60` |    
| [opnsense_min_version](defaults/main.yml#L22)   | str | `26.7.2` |    





### Tasks


#### File: tasks/connect.yml

| Name | Module | Has Conditions |
| ---- | ------ | -------------- |
| connect ¦ Connect to the router and read its release | opnsense_info | False |
| connect ¦ Check the router runs a release the role supports | ansible.builtin.assert | False |
| connect ¦ Report the router's state | ansible.builtin.debug | False |

#### File: tasks/main.yml

| Name | Module | Has Conditions | Tags |
| ---- | ------ | -------------- | -----|
| Check the role's inputs | ansible.builtin.import_tasks | False | always |
| Configure the router through its API | block | False |  |
| Connect and report the router's state | ansible.builtin.import_tasks | False |  |

#### File: tasks/validate.yml

| Name | Module | Has Conditions |
| ---- | ------ | -------------- |
| validate ¦ Check the connection settings | ansible.builtin.assert | False |
| validate ¦ Refuse to send the API key to an unproven server | ansible.builtin.assert | False |


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

  Start-->|Task| connect___Connect_to_the_router_and_read_its_release0[connect   connect to the router and read its<br>release]:::task
  connect___Connect_to_the_router_and_read_its_release0-->|Task| connect___Check_the_router_runs_a_release_the_role_supports1[connect   check the router runs a release the role<br>supports]:::task
  connect___Check_the_router_runs_a_release_the_role_supports1-->|Task| connect___Report_the_router_s_state2[connect   report the router s state]:::task
  connect___Report_the_router_s_state2-->End
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
  Check_the_role_s_inputs_validate_yml_0-->|Block Start| Configure_the_router_through_its_API1_block_start_0[[configure the router through its api]]:::block
  Configure_the_router_through_its_API1_block_start_0-->|Import task| Connect_and_report_the_router_s_state_connect_yml_0[/connect and report the router s state<br>import_task: connect yml/]:::importTasks
  Connect_and_report_the_router_s_state_connect_yml_0-.->|End of Block| Configure_the_router_through_its_API1_block_start_0
  Connect_and_report_the_router_s_state_connect_yml_0-->End
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
  validate___Check_the_connection_settings0-->|Task| validate___Refuse_to_send_the_API_key_to_an_unproven_server1[validate   refuse to send the api key to an<br>unproven server]:::task
  validate___Refuse_to_send_the_API_key_to_an_unproven_server1-->End
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
