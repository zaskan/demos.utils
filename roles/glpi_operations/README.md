# glpi_operations — GLPI REST API (open / close incidents as Tickets)

Ansible role that drives the [GLPI REST API](https://raw.githubusercontent.com/glpi-project/glpi/main/apirest.md) with `ansible.builtin.uri` only (no extra collections). It creates and updates **Tickets** with `type: 1` (incident): open a new ticket, move it to **in progress**, then optionally close it in a later step.

**License:** SPDX-License-Identifier: MIT-0 (see `defaults/main.yml`).

---

## Requirements

- Ansible **2.12+** on the control node.
- GLPI with the API enabled and a user allowed to create/update tickets. Some installations require an **application token** (`App-Token`); others allow **HTTP Basic** login alone—match your GLPI **Setup → API** settings.
- Network reachability from the control node (or target host, if you run the role there) to `{{ glpi_api_url }}`.

---

## Actions (`glpi_action`)

| Value | Behaviour |
|-------|------------|
| `open_incident` | `initSession` → `POST /Ticket/` → `PATCH` ticket to status **2** (processing) → `set_stats` + `set_fact` (see below) → `killSession` |
| `close_incident` | `initSession` → `PATCH /Ticket/{id}` with status **6** (closed) and a **solution** string → `killSession` |

GLPI ticket **status** values used here match the usual ITIL mapping (see your GLPI help or `Ticket` dropdowns): **2** in progress, **6** closed.

---

## Variables

### Always (both actions)

| Variable | Description |
|----------|-------------|
| `glpi_action` | `open_incident` or `close_incident`. |
| `glpi_api_url` | Base URL of `apirest.php`, without a trailing slash, e.g. `https://glpi.example.org/apirest.php`. |

### Optional (both actions)

| Variable | Description |
|----------|-------------|
| `glpi_app_token` | Application token from GLPI **Setup → API**. If unset or empty, the role **omits** the `App-Token` header on every call (useful for **HTTP Basic–only** access when GLPI allows it). |

### Authentication (choose one)

| Variable | Description |
|----------|-------------|
| `glpi_user_token` | User “remote access key” from the user’s preferences. If this is **non-empty**, it is sent as `Authorization: user_token …` and login/password are **not** used. |
| `glpi_username` / `glpi_password` | GLPI login and password. Used as **HTTP Basic** on `initSession` when `glpi_user_token` is unset or empty. **Both** must be non-empty for Basic auth to activate. You can use **only** username and password with **no** `glpi_app_token` if your GLPI API is configured that way. |

`initSession` is called with `session_write=true` so ticket create/update works when the API defaults to read-only sessions. The init task uses `no_log: true` so tokens and passwords are not echoed in Ansible output.

### `open_incident`

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `glpi_incident_title` | yes | — | Ticket `name`. |
| `glpi_incident_description` | yes | — | Ticket `content` (HTML is common in GLPI). |
| `glpi_incident_impact` | no | `3` | GLPI impact scale. |
| `glpi_incident_urgency` | no | `3` | GLPI urgency scale. |
| `glpi_incident_requester_id` | no | omitted | GLPI **user id** for `_users_id_requester` (omit or `0` to skip). |

### `close_incident`

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `glpi_incident_id` | yes | — | Numeric GLPI ticket **id** (the value returned when the ticket was created). |
| `glpi_incident_notes` | yes | — | Maps to the ticket **solution** body when closing. |
| `glpi_solutiontypes_id` | no | omitted | If set and non-zero, sent as `solutiontypes_id` (some GLPI setups require a solution type). |

### Other optional (both actions)

| Variable | Default | Description |
|----------|---------|-------------|
| `glpi_validate_certs` | `true` | Passed to `ansible.builtin.uri` for TLS verification. |

---

## Outputs and chaining

- **`set_stats`:** `data.incident` is set to the new ticket id as a **string** (useful for Ansible Tower / AAP job metadata, similar to a ServiceNow incident number field name).
- **`set_fact`:** After a successful open, `glpi_last_ticket_id` is set on the host so another task in the **same play** can call the role again with `glpi_action: close_incident` and `glpi_incident_id: "{{ glpi_last_ticket_id }}"`.

---

## Example

```yaml
- hosts: localhost
  connection: local
  gather_facts: false
  roles:
    - role: glpi_operations
  vars:
    glpi_action: open_incident
    glpi_api_url: https://glpi.example.org/apirest.php
    glpi_app_token: "{{ vault_glpi_app_token }}"
    glpi_user_token: "{{ vault_glpi_user_token }}"
    glpi_incident_title: "Disk space low on app-01"
    glpi_incident_description: "<p>/var is 95% full.</p>"
    glpi_incident_requester_id: 2
```

Login and password instead of `glpi_user_token` (with or without `glpi_app_token`, depending on GLPI):

```yaml
    glpi_username: glpi
    glpi_password: "{{ vault_glpi_password }}"
```

Login and password **only** (no application token, no user token), when GLPI allows API access without `App-Token`:

```yaml
    glpi_api_url: https://glpi.example.org/apirest.php
    glpi_username: glpi
    glpi_password: "{{ vault_glpi_password }}"
```

Close (often a second play or job template, passing the id you stored):

```yaml
    glpi_action: close_incident
    glpi_incident_id: 12345
    glpi_incident_notes: "<p>Cleaned logs; volume below 80%.</p>"
    glpi_solutiontypes_id: 1   # optional
```

Ensure the same API URL, app token, and auth vars are available for both calls.

---

## Integration test

Under `tests/`, `test.yml` opens a ticket and closes it using `glpi_last_ticket_id`.

1. Edit **`tests/vars.yml`** once (API URL, TLS, requester id, test title/description text — no secrets).
2. Copy **`tests/credentials.yml.example`** to **`tests/credentials.yml`** (gitignored) and fill in only GLPI credentials.
3. Run:

```bash
cd roles/glpi_operations/tests
ansible-playbook -i inventory test.yml -e @credentials.yml
```

You can still override anything with extra `-e` arguments. For ad hoc runs without `vars.yml`, pass the same variables as before via `-e` (see comments in `test.yml`).

---

## Troubleshooting (short)

- **401 on `initSession`:** Wrong user token or Basic credentials, or GLPI requires an `App-Token` you did not set—enable/configure the API under **Setup → API** and match your server’s rules (some sites require an application token for every call).
- **Close fails or GLPI demands a solution type:** Set `glpi_solutiontypes_id` to a valid id from your GLPI instance.
- **TLS errors in a lab:** `-e 'glpi_validate_certs=false'` (avoid in production).

For defaults and inline comments, see `defaults/main.yml`. For behaviour details, see `tasks/open_incident.yaml` and `tasks/close_incident.yaml`.
