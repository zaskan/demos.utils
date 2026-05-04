# ITSM API seed (Ansible role)

An Ansible role that creates ITSM objects via the REST API (`/api/v1`) using HTTP Basic auth and variables. Intended for demos and empty-database bootstrap — **mostly non-idempotent** (each run POSTs new rows). **Users** are an exception: existing usernames are skipped after `GET /api/v1/users`, so re-running the playbook does not fail on duplicate users.

## Requirements

- Ansible 2.14+ (uses `ansible.builtin.uri`)
- A running ITSM app with at least one admin user (see main project README / bootstrap env vars)

## Layout

- Role: `roles/itsm_api_seed/`
- Example playbook: `playbooks/test_seed.yml`
- Example vars: `playbooks/vars/test_seed.yml`

## Run

From this directory:

```bash
cd itsm-ansible-role
ansible-playbook playbooks/test_seed.yml
```

The playbook loads `playbooks/vars/test_seed.yml`. Override connection or secrets:

```bash
ansible-playbook playbooks/test_seed.yml \
  -e itsm_api_base_url=http://127.0.0.1:8000 \
  -e itsm_api_user=admin \
  -e itsm_api_password=admin
```

Use **Ansible Vault** for `itsm_api_password` and user passwords in production.

## Role variables (reference)

| Variable | Description |
|----------|-------------|
| `itsm_api_base_url` | Base URL without trailing slash (e.g. `http://127.0.0.1:8000`) |
| `itsm_api_user` | Basic auth username |
| `itsm_api_password` | Basic auth password |
| `itsm_validate_certs` | TLS verify (default `true`) |
| `itsm_http_timeout` | Seconds for HTTP calls |
| `itsm_seed_check_connection` | Run GET smoke check (default `true`) |
| `itsm_seed_admin_settings` | Apply app title, branding, webhook (default `true`) |
| `itsm_seed_users` | Create users from list (default `true`) |
| `itsm_seed_asset_types` | POST asset types (default `true`) |
| `itsm_seed_inventory` | POST inventory (default `true`) |
| `itsm_seed_kb` | POST KB articles (default `true`) |
| `itsm_seed_incidents` | POST incidents (default `true`) |
| `itsm_seed_followups` | Comments + close (default `true`) |
| `itsm_app_title` | If set, PUT `/api/v1/settings/app` |
| `itsm_branding` | Dict for PATCH `/api/v1/settings/branding` (only keys present are sent) |
| `itsm_webhook_url` | If defined, PUT `/api/v1/settings/webhook` (can be `""`) |
| `itsm_users` | List of `{ username, password, role }` — **admin only**; users whose username already exists are skipped (no password update) |
| `itsm_asset_types` | List of `{ name, description }`; names already in the API are skipped (map filled from `GET /asset-types` first) |
| `itsm_inventory` | List of `{ asset_type_name, hostname, ip_address, group_name }`; **hostname** dedupes — existing hostnames from `GET /inventory` are not created again |
| `itsm_kb_articles` | List of `{ title, description }` |
| `itsm_incidents` | List of `{ seed_key, title, description, severity, created_at, inventory_hostname, inventory_asset_id }` — use `seed_key` for follow-ups; link asset via `inventory_hostname` or numeric `inventory_asset_id` |
| `itsm_incident_followups` | List of `{ seed_key, comments: [{ body }], close: { kb_article_id \| kb_article_title } }` |

### Ordering

1. Connection check  
2. Admin settings (app, branding, webhook)  
3. Users  
4. Asset types → builds name→id map  
5. Inventory → builds hostname→id map  
6. KB articles → builds title→id map  
7. Incidents (optional asset link) → builds seed_key→`public_id`  
8. Follow-ups (comments, close with optional KB resolution)

**Logo upload** (`POST /settings/branding/logo`) is not implemented in vars; use the UI or a separate task with `uri` + file.
