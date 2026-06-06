# ITSM Ansible role

An Ansible role that creates ITSM objects via the REST API (`/api/v1`) using HTTP Basic auth and variables. Intended for demos and empty-database bootstrap — **mostly non-idempotent** (each run POSTs new rows). **Users, asset types, assets, templates, and custom field keys** are partially idempotent (existing names/keys are skipped after a GET).

Compatible with [itsm-app](https://github.com/zaskan/itsm-app) **main** (assets API, service requests, changes, templates, custom fields).

## Requirements

- Ansible 2.14+ (uses `ansible.builtin.uri`)
- A running ITSM app with at least one admin user (see itsm-app README / bootstrap env vars)

## Layout

- Role: this directory ([`roles/itsm_ansible_role/`](.) in the monorepo)
- Example playbook: [`test/example.yml`](test/example.yml)
- Example vars: [`test/vars/example.yml`](test/vars/example.yml)
- Dynamic inventory script: [`files/itsm_inventory.py`](files/itsm_inventory.py)
- Example inventory config: [`inventory/itsm.yml`](inventory/itsm.yml)

## Dynamic inventory (sync assets)

ITSM assets flagged **Include in external inventory** (`external_inventory: true` in the API or UI) are exported via:

```http
GET /api/v1/assets?external_only=true
```

The role ships [`files/itsm_inventory.py`](files/itsm_inventory.py) — a Python 3 dynamic inventory script (stdlib only) that calls that endpoint and builds Ansible groups:

| Group | Contents |
|-------|----------|
| `itsm` | All exported assets (host name = asset `name`) |
| `itsm_type_<asset_type>` | Assets grouped by `asset_type_name` (sanitized) |
| `itsm_env_<value>` | Assets with custom field `environment` |

Each host receives ITSM fields as host vars (`description`, `asset_type_name`, `custom_fields`, `assigned_username`, …). Custom fields are merged into host vars; **`ip_address`**, **`management_ip`**, or **`ansible_host`** custom fields set `ansible_host` automatically.

### Configure credentials

```bash
export ITSM_API_BASE_URL=https://itsm-app.example.com   # or ITSM_API_BASE
export ITSM_API_USER=admin
export ITSM_API_PASSWORD=admin
# export ITSM_VALIDATE_CERTS=false   # optional, for self-signed TLS
# export ITSM_INVENTORY_QUERY=db     # optional ?q= filter
```

Mark assets for export when bootstrapping with the role:

```yaml
itsm_assets:
  - name: demo-host-01.example.com
    description: App server
    asset_type_name: Server
    external_inventory: true
    custom_fields:
      ip_address: "10.0.0.50"
      environment: dev
```

Or patch an existing asset: `PATCH /api/v1/assets/{id}` with `{"external_inventory": true}`.

### Run

From this role directory:

```bash
# List synced hosts
./files/itsm_inventory.py --list --pretty

# Use as inventory for playbooks
ansible-inventory -i files/itsm_inventory.py --list
ansible-playbook -i files/itsm_inventory.py site.yml

# Or via the YAML wrapper (script inventory plugin)
ansible-playbook -i inventory/itsm.yml site.yml
```

Host limit example:

```bash
ansible-playbook -i files/itsm_inventory.py site.yml --limit itsm_type_server
```

To include **all** assets (not only `external_inventory`), set `ITSM_INVENTORY_EXTERNAL_ONLY=false`.

## Run

From this directory:

```bash
cd roles/itsm_ansible_role
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

Skip workflow sections for a minimal bootstrap:

```bash
ansible-playbook test/example.yml \
  -e itsm_ansible_role_service_requests=false \
  -e itsm_ansible_role_changes=false \
  -e itsm_ansible_role_change_tasks=false
```

Use **Ansible Vault** for `itsm_api_password` and user passwords in production.

## Migration: inventory → assets

Older playbooks used `itsm_inventory` with `GET/POST /api/v1/inventory` and fields `hostname`, `ip_address`, `group_name`. The current app uses **`/api/v1/assets`** with `name`, `description`, `asset_type_id`, optional `assigned_user_id`, `parent_asset_id`, `external_inventory`, and `custom_fields`.

- Prefer **`itsm_assets`** (see [`test/vars/example.yml`](test/vars/example.yml)).
- **`itsm_inventory`** is deprecated but still accepted when `itsm_assets` is empty (legacy items are mapped automatically).
- Incident asset links: use **`asset_name`** (or legacy **`inventory_hostname`** when it matches an asset name).

## Role variables (reference)

### Connection

| Variable | Description |
|----------|-------------|
| `itsm_api_base_url` | Base URL without trailing slash (e.g. `http://127.0.0.1:8000`) |
| `itsm_api_user` | Basic auth username |
| `itsm_api_password` | Basic auth password |
| `itsm_validate_certs` | TLS verify (default `true`) |
| `itsm_http_timeout` | Seconds for HTTP calls |

### Section toggles (all default `true`)

| Variable | Controls |
|----------|----------|
| `itsm_ansible_role_check_connection` | GET smoke check |
| `itsm_ansible_role_admin_settings` | App title, branding, webhooks |
| `itsm_ansible_role_users` | User creation + username→id map |
| `itsm_ansible_role_asset_types` | Asset types + optional custom fields |
| `itsm_ansible_role_assets` | Assets (`/api/v1/assets`) |
| `itsm_ansible_role_inventory` | **Deprecated** alias for assets toggle |
| `itsm_ansible_role_kb` | KB articles |
| `itsm_ansible_role_incidents` | Incidents |
| `itsm_ansible_role_followups` | Comments + close |
| `itsm_ansible_role_task_templates` | Task templates + fields |
| `itsm_ansible_role_change_templates` | Change templates + fields |
| `itsm_ansible_role_request_templates` | Request templates + fields |
| `itsm_ansible_role_service_requests` | Service requests + optional submit |
| `itsm_ansible_role_changes` | Standalone changes from templates |
| `itsm_ansible_role_change_tasks` | CTASK start/complete progress |

### Admin settings

| Variable | Description |
|----------|-------------|
| `itsm_app_title` | If set, PUT `/api/v1/settings/app` |
| `itsm_branding` | Dict for PATCH `/api/v1/settings/branding` (`preset`, `sidebar_background`, `sidebar_text`, …) |
| `itsm_webhooks` | List of `{ url, label?, enabled? }` — idempotent sync via GET/POST/PATCH/DELETE |
| `itsm_webhook_url` | **Legacy** single URL when `itsm_webhooks` is empty |
| `itsm_webhooks_remove_unlisted` | DELETE server webhooks not in the desired list |

**Logo upload** (`POST /api/v1/settings/branding/logo`) is not implemented; use the UI or a custom `uri` task with multipart.

### Content lists

| Variable | Item shape | Idempotency |
|----------|------------|-------------|
| `itsm_users` | `{ username, password, role }` | Skip existing usernames |
| `itsm_asset_types` | `{ name, description, fields? }` | Skip existing names; fields by `field_key` |
| `itsm_assets` | `{ name, description, asset_type_name?, assigned_username?, parent_asset_name?, external_inventory?, custom_fields? }` | Skip existing names |
| `itsm_inventory` | **Legacy** `{ asset_type_name, hostname, ip_address?, group_name? }` | Used when `itsm_assets` empty |
| `itsm_kb_articles` | `{ title, description }` | Non-idempotent (always POST) |
| `itsm_incidents` | `{ incident_key, title, description, severity, created_at?, asset_name?, inventory_hostname?, inventory_asset_id? }` | Non-idempotent |
| `itsm_incident_followups` | `{ incident_key, comments: [{ body }], close: { kb_article_id \| kb_article_title } }` | Non-idempotent |
| `itsm_task_templates` | `{ name, title, description, assigned_username?, kb_article_title?, fields? }` | Skip existing names |
| `itsm_change_templates` | `{ name, description, change_type, task_template_names[], fields? }` | Skip existing names |
| `itsm_request_templates` | `{ name, description, change_template_name, require_standard_change?, fields? }` | Skip existing names |
| `itsm_service_requests` | `{ request_key, name, description, request_template_name?, specifications?, submit? }` | Non-idempotent |
| `itsm_changes` | `{ change_key, change_template_name, approve?, custom_fields? }` | Non-idempotent |
| `itsm_change_task_progress` | `{ change_ref?, change_key?, request_key?, mode }` | Non-idempotent |

### Custom field definitions

Nested under asset types or templates as `fields`:

```yaml
fields:
  - field_key: vcpu
    label: vCPU
    field_type: number        # text | textarea | number | boolean | select | date
    required: false
    options: []               # for select
    sort_order: 0
```

### CTASK progress modes

| `mode` | Behavior |
|--------|----------|
| `complete_all` | Start and complete every CTASK on the change |
| `half` | Complete the first half of CTASKs |
| `last_in_progress` | Complete all but the last; start the last |
| `pending` | No action (document only) |

Reference a change by **`change_ref`**, **`change_key`** (from `itsm_changes`), or **`request_key`** (first change from a submitted service request).

## Execution order

1. Connection check
2. Admin settings (app title, branding, webhooks)
3. Users → `itsm_user_by_username`
4. Asset types (+ custom fields) → `itsm_asset_type_by_name`
5. Assets → `itsm_asset_by_name`
6. KB articles → `itsm_kb_by_title`
7. Incidents → `itsm_incident_by_key`
8. Follow-ups (comments, close with optional KB resolution)
9. Task templates → `itsm_task_template_by_name`
10. Change templates → `itsm_change_template_by_name`
11. Request templates → `itsm_request_template_by_name`
12. Service requests (+ submit) → `itsm_request_by_key`, `itsm_changes_from_request`
13. Standalone changes → `itsm_change_by_key`
14. CTASK progress (start/complete)

## Example: Linux VM workflow

The example vars file defines a full catalog chain matching itsm-app’s `populate_fake_data.py` demo:

1. **Task templates** linked to KB articles (provision VM, install packages, deploy app, smoke test).
2. **Change template** `Linux VM — Standard Change` (standard type, ordered task templates).
3. **Request template** `New Linux Virtual Machine` with custom fields (`vcpu`, `ram_gb`, …) and linked change template.
4. **Service request** submitted with specifications → auto-creates CHG + CTASKs.
5. **Standalone change** from the same change template.
6. **CTASK progress** — `complete_all` on the submitted request’s change, `half` on the standalone change.

See [`test/vars/example.yml`](test/vars/example.yml) for the full variable definitions.

## Not covered

This role bootstraps data via REST only. It does **not** configure deployment env vars (MCP token, embedding/RAG, session secret) or upload branding logos.
