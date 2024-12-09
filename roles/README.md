Example:

~~~
namespaces:
  - name: aap
operators:
  - name: openshift-gitops-operator
    namespace: openshift-operators
    channel: gitops-1.12
    source: redhat-operators
    sourcenamespace: openshift-marketplace
    clusterwide: true
  - name: ansible-automation-platform-operator
    namespace: aap
    channel: stable-2.4-cluster-scoped
    source: redhat-operators
    sourcenamespace: openshift-marketplace
    clusterwide: false
resources_url: https://github.com/zaskan/ansible_automation_platform.casc.git
target_revision: main
resources_path: "roles/deploy_gitops/files/environment/resources/"
~~~
