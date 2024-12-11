Example:

```yaml
argo:
  ocp_domain: http://cluster.domain.com
  target_namespaces: # Namespaces where ArgoCD will create resources
    - name: test
  environment_repo_url: http://test.github.com
  environment_repo_revision: master
  environment_path: installation/environment/* # Will create an app for each directory in this path
```
