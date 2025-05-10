Example:

```yaml
htpasswd_provider_operation: # install or uninstall
htpasswd_provider:
  name: provider-name
  users_key: user- # Starting user key - number will be concatenated afterwards (ex: user-2)
  pass_key: pass- # Starting passwd key - number will be concatenated afterwards (ex: pass-2)
  total_users: 3 # Number of users to be created
```