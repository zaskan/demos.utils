# ITSM Ansible role

An Ansible role that creates ITSM objects via the REST API (`/api/v1`) using HTTP Basic auth and variables. Intended for demos and empty-database bootstrap — **mostly non-idempotent** (each run POSTs new rows). **Users** are an exception: existing usernames are skipped after `GET /api/v1/users`, so re-running the playbook does not fail on duplicate users.

## Requirements

- Ansible 2.14+ (uses `ansible.builtin.uri`)
- A running ITSM app with at least one admin user (see main project README / bootstrap env vars)

## Layout

- Role: this directory ([`roles/itsm-ansible-role/`](.) in the monorepo)
- Example playbook: [`test/example.yml`](test/example.yml)
- Example vars: [`test/vars/example.yml`](test/vars/example.yml)

## Run

From this directory:

```bash
cd itsm-ansible-role
ansible-playbook test/example.yml
```

The playbook loads `test/vars/example.yml` and includes this role by path (`{{ playbook_dir }}/..`) so a local clone works without installing under `roles/itsm_ansible_role`. If you install the role from Galaxy (or copy it to `roles/itsm_ansible_role`), you can use `role: itsm_ansible_role` instead and set `roles_path` accordingly.

Override connection or secrets:

```bash
ansible-playbook test/example.yml \
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
| `itsm_ansible_role_check_connection` | Run GET smoke check (default `true`) |
| `itsm_ansible_role_admin_settings` | Apply app title, branding, outbound webhooks (default `true`) |
| `itsm_ansible_role_users` | Create users from list (default `true`) |
| `itsm_ansible_role_asset_types` | POST asset types (default `true`) |
| `itsm_ansible_role_inventory` | POST inventory (default `true`) |
| `itsm_ansible_role_kb` | POST KB articles (default `true`) |
| `itsm_ansible_role_incidents` | POST incidents (default `true`) |
| `itsm_ansible_role_followups` | Comments + close (default `true`) |
| `itsm_app_title` | If set, PUT `/api/v1/settings/app` |
| `itsm_branding` | Dict for PATCH `/api/v1/settings/branding` (only keys present are sent) |
| `itsm_webhooks` | List of `{ url, label?, enabled? }` — idempotent sync against `GET/POST/PATCH /api/v1/settings/webhooks` (after `unique` by `url`, first occurrence wins); missing URLs receive `POST`; existing URLs get `PATCH` when `label` or `enabled` differs |
| `itsm_webhook_url` | **Legacy**: when `itsm_webhooks` is empty and this is a non-empty string (after trim), treated as one enabled webhook — same behaviour as formerly `PUT …/settings/webhook` against older itsm-app |
| `itsm_webhooks_remove_unlisted` | If `true`, `DELETE` any server webhook whose URL is not in the Ansible desired list (dangerous when combined with empty desired — can remove every outbound hook) |
| `itsm_users` | List of `{ username, password, role }` — **admin only**; users whose username already exists are skipped (no password update) |
| `itsm_asset_types` | List of `{ name, description }`; names already in the API are skipped (map filled from `GET /asset-types` first) |
| `itsm_inventory` | List of `{ asset_type_name, hostname, ip_address, group_name }`; **hostname** dedupes — existing hostnames from `GET /inventory` are not created again |
| `itsm_kb_articles` | List of `{ title, description }` |
| `itsm_incidents` | List of `{ incident_key, title, description, severity, created_at, inventory_hostname, inventory_asset_id }` — use `incident_key` for follow-ups; link asset via `inventory_hostname` or numeric `inventory_asset_id` |
| `itsm_incident_followups` | List of `{ incident_key, comments: [{ body }], close: { kb_article_id \| kb_article_title } }` |

### Ordering

1. Connection check
2. Admin settings (app, branding, outbound webhooks)
3. Users
4. Asset types → builds name→id map
5. Inventory → builds hostname→id map
6. KB articles → builds title→id map
7. Incidents (optional asset link) → builds incident_key→`public_id`
8. Follow-ups (comments, close with optional KB resolution)

**Logo upload** (`POST /settings/branding/logo`) is not implemented in vars; use the UI or a separate task with `uri` + file.
