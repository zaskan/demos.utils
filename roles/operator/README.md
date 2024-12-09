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
~~~
