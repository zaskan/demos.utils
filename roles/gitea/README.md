Example:

```yaml
gitea:
  namespace: gitea # Gitea will be the default namespace
  ocp_domain: "{{ ocp_host }}"
  repositories: # list of repos to replicate in Gitea
    - name: test
      url: https://github.com/clbartolome/gitops-eda.git
      branch: master
      path: "/installation" # include '/' before the desired directory 
    - name: test-2
      url: https://github.com/clbartolome/gitops-eda.git
      branch: master
      path: ""
  users:
    - id: dev
      pass: pass
      admin: false
      colaborator_in: # projects where user is colaborator (write permisions)
        - test
    - id: dev-3
      pass: pass
      admin: true
      colaborator_in: []
```