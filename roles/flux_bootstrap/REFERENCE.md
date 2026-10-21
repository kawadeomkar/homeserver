<!-- DOCSIBLE START -->

# 📃 Role overview

## flux_bootstrap



Description: Hand a Kubernetes cluster to Flux, through the Flux Operator






<details>
<summary><b>🧩 Argument Specifications in meta/argument_specs</b></summary>

#### Key: main

**Description**: 
- Runs on the controller with kubectl. Installs or upgrades the Flux Operator, and keeps the Secret flux-system/sops-age (the age key), the ConfigMap flux-system/cluster-settings and the FluxInstance flux as configured, each compared with a server-side diff and applied server-side under one field manager, so a key removed from the settings disappears.
- Flux 2.9 and later fail a manifest naming a setting that does not exist (strict substitution, on by default), instead of giving it an empty string.
- Refuses an age key file that holds anything but one key, or that others can read.
- Installs only an install.yaml of the pinned SHA-256, with the operator's image pinned by digest, and gives the FluxInstance an exact Flux release and a manifests artifact pinned by digest, so nothing it installs can change unreviewed.
- The defaults do nothing.


**Options**:


  - **flux_bootstrap_kubeconfig**
    - **Required**: false
    - **Type**: str
    - **Default**: 
  
    - **Description**: The cluster's kubeconfig.
  
  
  

  - **flux_bootstrap_operator_version**
    - **Required**: false
    - **Type**: str
    - **Default**: 
  
    - **Description**: The Flux Operator's release, such as v0.61.0.
  
  
  

  - **flux_bootstrap_operator_url**
    - **Required**: false
    - **Type**: str
    - **Default**: https://github.com/controlplaneio-fluxcd/flux-operator/releases/download
  
    - **Description**: Where the operator's releases are; <url>/<version>/install.yaml is applied.
  
  
  

  - **flux_bootstrap_operator_sha256**
    - **Required**: false
    - **Type**: str
    - **Default**: 
  
    - **Description**: The SHA-256 (hex) install.yaml must have, checked before anything in it is applied. Required with a version.
  
  
  

  - **flux_bootstrap_operator_digest**
    - **Required**: false
    - **Type**: str
    - **Default**: 
  
    - **Description**: The operator image's digest (sha256:...), which its Deployment is given beside the tag. Required with a version.
  
  
  

  - **flux_bootstrap_instance**
    - **Required**: false
    - **Type**: dict
    - **Default**: {}
  
    - **Description**: The FluxInstance's exact Flux release (such as 2.9.6), the operator's manifests artifact pinned by digest (oci://...@sha256:...), and the Git url, ref and path.
  
  
  

  - **flux_bootstrap_settings**
    - **Required**: false
    - **Type**: dict
    - **Default**: {}
  
    - **Description**: The cluster-settings ConfigMap's keys and values, all strings.
  
  
  

  - **flux_bootstrap_age_key_file**
    - **Required**: false
    - **Type**: str
    - **Default**: 
  
    - **Description**: The age private key file that becomes the Secret flux-system/sops-age. One key, mode 0600.
  
  
  

  - **flux_bootstrap_wait_timeout**
    - **Required**: false
    - **Type**: str
    - **Default**: 10m
  
    - **Description**: How long a real run waits for the operator, the FluxInstance and Flux's Kustomizations.
  
  
  

  - **flux_bootstrap_sync_exclude**
    - **Required**: false
    - **Type**: list
    - **Default**: []
  
    - **Description**: What the FluxInstance's sync leaves out. An entry with a kustomization alone drops that Flux Kustomization (in flux-system) from the sync, which prunes it and what it applied, bar objects marked kustomize.toolkit.fluxcd.io/prune disabled. An entry with a selector too keeps it, and its own build drops the objects the label selector matches. Empty leaves the sync as the repository has it.
  
  
  
    
  

  - **flux_bootstrap_allow_path_change**
    - **Required**: false
    - **Type**: bool
    - **Default**: False
  
    - **Description**: Let a run move the FluxInstance to another path, which deletes everything the old path applied (Flux prunes it). Without it, a path other than the live one is refused.
  
  
  



</details>




### Defaults

**These are static variables with lower priority**

#### File: defaults/main.yml

| Var          | Type         | Value       |
|--------------|--------------|-------------|
| [flux_bootstrap_kubeconfig](defaults/main.yml#L6)   | str |  |    
| [flux_bootstrap_operator_version](defaults/main.yml#L8)   | str |  |    
| [flux_bootstrap_operator_url](defaults/main.yml#L9)   | str | `https://github.com/controlplaneio-fluxcd/flux-operator/releases/download` |    
| [flux_bootstrap_operator_sha256](defaults/main.yml#L12)   | str |  |    
| [flux_bootstrap_operator_digest](defaults/main.yml#L13)   | str |  |    
| [flux_bootstrap_instance](defaults/main.yml#L16)   | dict | `{}` |    
| [flux_bootstrap_settings](defaults/main.yml#L19)   | dict | `{}` |    
| [flux_bootstrap_age_key_file](defaults/main.yml#L22)   | str |  |    
| [flux_bootstrap_wait_timeout](defaults/main.yml#L24)   | str | `10m` |    
| [flux_bootstrap_sync_exclude](defaults/main.yml#L29)   | list | `[]` |    
| [flux_bootstrap_allow_path_change](defaults/main.yml#L32)   | bool | `False` |    





### Tasks


#### File: tasks/main.yml

| Name | Module | Has Conditions | Tags |
| ---- | ------ | -------------- | -----|
| Check the role's inputs | ansible.builtin.assert | True | always |
| Hand the cluster to Flux | block | True |  |
| Look at the age key file | ansible.builtin.stat | False |  |
| Count the keys and other lines in it | ansible.builtin.command | True |  |
| Refuse an age key file that is not one key, or that others can read | ansible.builtin.assert | False |  |
| Look for the kubeconfig | ansible.builtin.stat | False |  |
| Refuse to run without a kubeconfig | ansible.builtin.assert | False |  |
| Read the Flux Operator | ansible.builtin.command | True |  |
| Say what would be installed | ansible.builtin.debug | True |  |
| Keep the operator, the age key, the settings and the FluxInstance as configured | ansible.builtin.import_tasks | True |  |

#### File: tasks/objects.yml

| Name | Module | Has Conditions |
| ---- | ------ | -------------- |
| objects ¦ Work out whether the operator is the pinned release | ansible.builtin.set_fact | False |
| objects ¦ Install or upgrade the Flux Operator | block | True |
| objects ¦ Make a scratch directory for the release | ansible.builtin.tempfile | False |
| objects ¦ Download the release's install.yaml, checking its SHA-256 | ansible.builtin.get_url | False |
| objects ¦ Pin the operator's image by digest | ansible.builtin.replace | False |
| objects ¦ Refuse a release whose operator image was not pinned | ansible.builtin.assert | False |
| objects ¦ Apply the release | ansible.builtin.command | False |
| objects ¦ Say the operator would be upgraded | ansible.builtin.debug | True |
| objects ¦ Wait for the operator | ansible.builtin.command | True |
| objects ¦ Render the age key as the Secret flux-system/sops-age | ansible.builtin.command | False |
| objects ¦ Work out what the sync leaves out | ansible.builtin.set_fact | False |
| objects ¦ Render the settings and the FluxInstance | ansible.builtin.set_fact | False |
| objects ¦ Read the FluxInstance's path | ansible.builtin.command | False |
| objects ¦ Refuse to move Flux to another path | ansible.builtin.assert | False |
| objects ¦ Compare the age key's Secret with the cluster's | ansible.builtin.command | False |
| objects ¦ Compare the settings and the FluxInstance with the cluster's | ansible.builtin.command | False |
| objects ¦ Show what differs | ansible.builtin.debug | True |
| objects ¦ Apply the age key's Secret | ansible.builtin.command | True |
| objects ¦ Apply the settings and the FluxInstance | ansible.builtin.command | False |
| objects ¦ Wait for the FluxInstance to be ready | ansible.builtin.command | True |
| objects ¦ Wait for the sync to carry what it leaves out | ansible.builtin.command | True |
| objects ¦ Refuse a sync that does not carry what it leaves out | ansible.builtin.assert | True |
| objects ¦ Read Flux's Kustomizations | ansible.builtin.command | True |
| objects ¦ Wait for the Kustomizations left out to be deleted | ansible.builtin.command | True |
| objects ¦ Wait for the Kustomizations kept to leave out what their selector matches | ansible.builtin.command | True |
| objects ¦ Refuse Kustomizations that have not left out what their selector matches | ansible.builtin.assert | True |
| objects ¦ Find the Deployments make down held among those left out | ansible.builtin.command | False |
| objects ¦ Say which held Deployments would be deleted | ansible.builtin.debug | True |
| objects ¦ Delete the Deployments make down held among those left out | ansible.builtin.command | True |
| objects ¦ Wait for Flux's Kustomizations to be ready | ansible.builtin.command | True |
| objects ¦ Read Flux's Kustomizations | ansible.builtin.command | True |
| objects ¦ Show Flux's Kustomizations | ansible.builtin.debug | True |
| objects ¦ Refuse Kustomizations that are not ready | ansible.builtin.assert | True |


## Task Flow Graphs



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

  Start-->|Task| Check_the_role_s_inputs0[check the role s inputs<br>When: **flux bootstrap operator version   length   0**]:::task
  Check_the_role_s_inputs0-->|Block Start| Hand_the_cluster_to_Flux1_block_start_0[[hand the cluster to flux<br>When: **flux bootstrap operator version   length   0**]]:::block
  Hand_the_cluster_to_Flux1_block_start_0-->|Task| Look_at_the_age_key_file0[look at the age key file]:::task
  Look_at_the_age_key_file0-->|Task| Count_the_keys_and_other_lines_in_it1[count the keys and other lines in it<br>When: **flux bootstrap age key stat exists**]:::task
  Count_the_keys_and_other_lines_in_it1-->|Task| Refuse_an_age_key_file_that_is_not_one_key__or_that_others_can_read2[refuse an age key file that is not one key  or<br>that others can read]:::task
  Refuse_an_age_key_file_that_is_not_one_key__or_that_others_can_read2-->|Task| Look_for_the_kubeconfig3[look for the kubeconfig]:::task
  Look_for_the_kubeconfig3-->|Task| Refuse_to_run_without_a_kubeconfig4[refuse to run without a kubeconfig]:::task
  Refuse_to_run_without_a_kubeconfig4-->|Task| Read_the_Flux_Operator5[read the flux operator<br>When: **flux bootstrap kubeconfig file stat exists**]:::task
  Read_the_Flux_Operator5-->|Task| Say_what_would_be_installed6[say what would be installed<br>When: **ansible check mode and  flux bootstrap operator is<br>skipped or flux bootstrap operator rc    0**]:::task
  Say_what_would_be_installed6-->|Import task| Keep_the_operator__the_age_key__the_settings_and_the_FluxInstance_as_configured_objects_yml_7[/keep the operator  the age key  the settings and<br>the fluxinstance as configured<br>When: **flux bootstrap operator is not skipped and  flux<br>bootstrap operator rc    0 or not ansible check<br>mode**<br>import_task: objects yml/]:::importTasks
  Keep_the_operator__the_age_key__the_settings_and_the_FluxInstance_as_configured_objects_yml_7-.->|End of Block| Hand_the_cluster_to_Flux1_block_start_0
  Keep_the_operator__the_age_key__the_settings_and_the_FluxInstance_as_configured_objects_yml_7-->End
```


### Graph for objects.yml

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

  Start-->|Task| objects___Work_out_whether_the_operator_is_the_pinned_release0[objects   work out whether the operator is the<br>pinned release]:::task
  objects___Work_out_whether_the_operator_is_the_pinned_release0-->|Block Start| objects___Install_or_upgrade_the_Flux_Operator1_block_start_0[[objects   install or upgrade the flux operator<br>When: **flux bootstrap operator current    flux bootstrap<br>operator pinned and not ansible check mode**]]:::block
  objects___Install_or_upgrade_the_Flux_Operator1_block_start_0-->|Task| objects___Make_a_scratch_directory_for_the_release0[objects   make a scratch directory for the release]:::task
  objects___Make_a_scratch_directory_for_the_release0-->|Task| objects___Download_the_release_s_install_yaml__checking_its_SHA_2561[objects   download the release s install yaml <br>checking its sha 256]:::task
  objects___Download_the_release_s_install_yaml__checking_its_SHA_2561-->|Task| objects___Pin_the_operator_s_image_by_digest2[objects   pin the operator s image by digest]:::task
  objects___Pin_the_operator_s_image_by_digest2-->|Task| objects___Refuse_a_release_whose_operator_image_was_not_pinned3[objects   refuse a release whose operator image<br>was not pinned]:::task
  objects___Refuse_a_release_whose_operator_image_was_not_pinned3-->|Task| objects___Apply_the_release4[objects   apply the release]:::task
  objects___Apply_the_release4-.->|End of Block| objects___Install_or_upgrade_the_Flux_Operator1_block_start_0
  objects___Apply_the_release4-->|Task| objects___Say_the_operator_would_be_upgraded2[objects   say the operator would be upgraded<br>When: **ansible check mode and flux bootstrap operator<br>current    flux bootstrap operator pinned**]:::task
  objects___Say_the_operator_would_be_upgraded2-->|Task| objects___Wait_for_the_operator3[objects   wait for the operator<br>When: **not ansible check mode**]:::task
  objects___Wait_for_the_operator3-->|Task| objects___Render_the_age_key_as_the_Secret_flux_system_sops_age4[objects   render the age key as the secret flux<br>system sops age]:::task
  objects___Render_the_age_key_as_the_Secret_flux_system_sops_age4-->|Task| objects___Work_out_what_the_sync_leaves_out5[objects   work out what the sync leaves out]:::task
  objects___Work_out_what_the_sync_leaves_out5-->|Task| objects___Render_the_settings_and_the_FluxInstance6[objects   render the settings and the fluxinstance]:::task
  objects___Render_the_settings_and_the_FluxInstance6-->|Task| objects___Read_the_FluxInstance_s_path7[objects   read the fluxinstance s path]:::task
  objects___Read_the_FluxInstance_s_path7-->|Task| objects___Refuse_to_move_Flux_to_another_path8[objects   refuse to move flux to another path]:::task
  objects___Refuse_to_move_Flux_to_another_path8-->|Task| objects___Compare_the_age_key_s_Secret_with_the_cluster_s9[objects   compare the age key s secret with the<br>cluster s]:::task
  objects___Compare_the_age_key_s_Secret_with_the_cluster_s9-->|Task| objects___Compare_the_settings_and_the_FluxInstance_with_the_cluster_s10[objects   compare the settings and the<br>fluxinstance with the cluster s]:::task
  objects___Compare_the_settings_and_the_FluxInstance_with_the_cluster_s10-->|Task| objects___Show_what_differs11[objects   show what differs<br>When: **flux bootstrap secret diff rc    1 or flux<br>bootstrap diffs results   selectattr  rc    eq   1<br>   list   length   0**]:::task
  objects___Show_what_differs11-->|Task| objects___Apply_the_age_key_s_Secret12[objects   apply the age key s secret<br>When: **flux bootstrap secret diff rc    1**]:::task
  objects___Apply_the_age_key_s_Secret12-->|Task| objects___Apply_the_settings_and_the_FluxInstance13[objects   apply the settings and the fluxinstance]:::task
  objects___Apply_the_settings_and_the_FluxInstance13-->|Task| objects___Wait_for_the_FluxInstance_to_be_ready14[objects   wait for the fluxinstance to be ready<br>When: **not ansible check mode**]:::task
  objects___Wait_for_the_FluxInstance_to_be_ready14-->|Task| objects___Wait_for_the_sync_to_carry_what_it_leaves_out15[objects   wait for the sync to carry what it<br>leaves out<br>When: **not ansible check mode**]:::task
  objects___Wait_for_the_sync_to_carry_what_it_leaves_out15-->|Task| objects___Refuse_a_sync_that_does_not_carry_what_it_leaves_out16[objects   refuse a sync that does not carry what<br>it leaves out<br>When: **flux bootstrap sync is not skipped**]:::task
  objects___Refuse_a_sync_that_does_not_carry_what_it_leaves_out16-->|Task| objects___Read_Flux_s_Kustomizations17[objects   read flux s kustomizations<br>When: **not ansible check mode and flux bootstrap<br>exclusions   rejectattr  selector     list  <br>length   0**]:::task
  objects___Read_Flux_s_Kustomizations17-->|Task| objects___Wait_for_the_Kustomizations_left_out_to_be_deleted18[objects   wait for the kustomizations left out to<br>be deleted<br>When: **not ansible check mode and  gone   length   0**]:::task
  objects___Wait_for_the_Kustomizations_left_out_to_be_deleted18-->|Task| objects___Wait_for_the_Kustomizations_kept_to_leave_out_what_their_selector_matches19[objects   wait for the kustomizations kept to<br>leave out what their selector matches<br>When: **not ansible check mode and flux bootstrap<br>exclusions   selectattr  selector     list  <br>length   0**]:::task
  objects___Wait_for_the_Kustomizations_kept_to_leave_out_what_their_selector_matches19-->|Task| objects___Refuse_Kustomizations_that_have_not_left_out_what_their_selector_matches20[objects   refuse kustomizations that have not left<br>out what their selector matches<br>When: **flux bootstrap kept is not skipped**]:::task
  objects___Refuse_Kustomizations_that_have_not_left_out_what_their_selector_matches20-->|Task| objects___Find_the_Deployments_make_down_held_among_those_left_out21[objects   find the deployments make down held<br>among those left out]:::task
  objects___Find_the_Deployments_make_down_held_among_those_left_out21-->|Task| objects___Say_which_held_Deployments_would_be_deleted22[objects   say which held deployments would be<br>deleted<br>When: **ansible check mode and  held   length   0**]:::task
  objects___Say_which_held_Deployments_would_be_deleted22-->|Task| objects___Delete_the_Deployments_make_down_held_among_those_left_out23[objects   delete the deployments make down held<br>among those left out<br>When: **not ansible check mode**]:::task
  objects___Delete_the_Deployments_make_down_held_among_those_left_out23-->|Task| objects___Wait_for_Flux_s_Kustomizations_to_be_ready24[objects   wait for flux s kustomizations to be<br>ready<br>When: **not ansible check mode**]:::task
  objects___Wait_for_Flux_s_Kustomizations_to_be_ready24-->|Task| objects___Read_Flux_s_Kustomizations25[objects   read flux s kustomizations<br>When: **not ansible check mode**]:::task
  objects___Read_Flux_s_Kustomizations25-->|Task| objects___Show_Flux_s_Kustomizations26[objects   show flux s kustomizations<br>When: **flux bootstrap kustomizations is not skipped**]:::task
  objects___Show_Flux_s_Kustomizations26-->|Task| objects___Refuse_Kustomizations_that_are_not_ready27[objects   refuse kustomizations that are not ready<br>When: **flux bootstrap kustomizations ready is not skipped**]:::task
  objects___Refuse_Kustomizations_that_are_not_ready27-->End
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
