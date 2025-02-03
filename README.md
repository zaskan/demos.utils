# Ansible Collection - demos.utils

Ansible roles for demos environment setup and configuration.

> [!NOTE]  
> Roles configurations are stored in each role README file

> [!IMPORTANT]  
> Tags are used to freeze roles versions and avoid conflicts

In order to use this collection:

- Create a directory `collections`
- Create a file named `requirements.yaml`:
  ```yaml
  collections:
  - name: https://github.com/zascan/demos.utils.git
      type: git
      version: main
  ```
- Run: 
  ```sh
  ansible-galaxy collection install -r collections/requirements.yaml
  ```

## Roles

Available Roles:

- argocd: configure OpenShift default argo CD and create applications for demos environments
- git: tasks for cloning and pushing code into a Git repository
- operator: install oeprators in OpenShift
- gitea: deploy and configure a gitea instance and clone initial repositories
- kubeconfig: create a kubeconfig file in ansible navigator containers
- aap_ocp: create and configure aap in ocp
- bitwarden: install and configure Bitwarden in OpenShift for secret management

TODO: review and describe servicenow and velero_backup roles
