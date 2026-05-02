# glpi_operations — GLPI REST API (Tickets / incidents)

Ansible role that drives the [GLPI REST API](https://raw.githubusercontent.com/glpi-project/glpi/main/apirest.md) with `ansible.builtin.uri` only (no extra collections). It supports **incident Tickets** (`type: 1`): create and move to **in progress**, add tasks, documents, ITIL solutions, approval requests, and close—each action uses its own `initSession` / `killSession` pair.

**License:** SPDX-License-Identifier: MIT-0 (see `defaults/main.yml`).

---

## Requirements

- Ansible **2.12+** on the control node.
- GLPI with the API enabled and a user with rights matching what you automate (tickets, tasks, documents, solutions, validations). Some installations require an **application token** (`App-Token`); others allow **HTTP Basic** login alone—match your GLPI **Setup → API** settings.
- Network reachability from the control node (or target host, if you run the role there) to `{{ glpi_api_url }}`.

---

## Actions (`glpi_action`)

| Value | Behaviour |
|-------|------------|
| `open_incident` | `initSession` → `POST /Ticket/` → `PATCH` ticket to status **2** (processing) → `set_stats` + `set_fact` (see below) → `killSession` |
| `close_incident` | `initSession` → `PATCH /Ticket/{id}` with status **6** (closed) and a **solution** string → `killSession` |
| `add_ticket_task` | `initSession` → `POST /TicketTask/` → `killSession` |
| `add_ticket_document` | `initSession` → upload `POST /Document/` (multipart when `glpi_document_file` is set) or use existing `glpi_document_id` → `POST /Document_Item/` to link the document to the ticket → `killSession` |
| `add_ticket_solution` | `initSession` → `POST /ITILSolution/` (e.g. `itemtype: Ticket`, `items_id`, `content`, optional `solutiontypes_id`, `status`) → `killSession` |
| `request_ticket_approval` | `initSession` → `POST /TicketValidation/` → `killSession` |

GLPI ticket **status** values used for open/close match the usual ITIL mapping (see your GLPI help or `Ticket` dropdowns): **2** in progress, **6** closed.

---

## Variables

### Always (every action)

| Variable | Description |
|----------|-------------|
| `glpi_action` | One of: `open_incident`, `close_incident`, `add_ticket_task`, `add_ticket_document`, `add_ticket_solution`, `request_ticket_approval`. |
| `glpi_api_url` | Base URL of `apirest.php`, without a trailing slash, e.g. `https://glpi.example.org/apirest.php`. |

### Optional (every action)

| Variable | Description |
|----------|-------------|
| `glpi_app_token` | Application token from GLPI **Setup → API**. If unset or empty, the role **omits** the `App-Token` header on every call (useful for **HTTP Basic–only** access when GLPI allows it). |

### Authentication (choose one)

| Variable | Description |
|----------|-------------|
| `glpi_user_token` | User “remote access key” from the user’s preferences. If this is **non-empty**, it is sent as `Authorization: user_token …` and login/password are **not** used. |
| `glpi_username` / `glpi_password` | GLPI login and password. Used as **HTTP Basic** on `initSession` when `glpi_user_token` is unset or empty. **Both** must be non-empty for Basic auth to activate. You can use **only** username and password with **no** `glpi_app_token` if your GLPI API is configured that way. |

`initSession` is called with `session_write=true` so create/update works when the API defaults to read-only sessions. The init task uses `no_log: true` so tokens and passwords are not echoed in Ansible output.

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

### `add_ticket_task`

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `glpi_incident_id` | yes | — | Ticket id. |
| `glpi_task_content` | yes | — | Task body (`content`; HTML is common). |
| `glpi_task_state` | no | `1` | Task state (GLPI planning / task state scale). |
| `glpi_task_users_id_tech` | no | omitted | If set and non-zero, sent as `users_id_tech`. |
| `glpi_task_groups_id_tech` | no | omitted | If set and non-zero, sent as `groups_id_tech`. |

### `add_ticket_document`

Provide **either** a file to upload **or** an existing GLPI document id. If **both** are set, the role uploads first and links the **new** document to the ticket.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `glpi_incident_id` | yes | — | Ticket id. |
| `glpi_document_file` | if uploading | — | Absolute or relative path on the **Ansible controller** to the file to upload. The role sends multipart `uploadManifest` + `filename[0]` using the mapping form so `ansible.builtin.uri` reads the file from disk (a bare string path would **not** upload file bytes). |
| `glpi_document_upload_name` | no | basename of `glpi_document_file` | Document name in GLPI (`input.name` in the manifest). |
| `glpi_document_id` | if linking only | — | Existing GLPI `Document` id; skips upload and only `POST /Document_Item/`. |

### `add_ticket_solution`

Adds an **ITILSolution** row for the ticket (separate from the plain `solution` text used in `close_incident`).

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `glpi_incident_id` | yes | — | Ticket id (`items_id`). |
| `glpi_solution_content` | yes | — | Solution HTML/text. |
| `glpi_solution_status` | no | `2` | GLPI solution status: **1** none, **2** waiting, **3** accepted, **4** refused. |
| `glpi_solutiontypes_id` | no | omitted | If set and non-zero, sent as `solutiontypes_id`. |

### `request_ticket_approval`

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `glpi_incident_id` | yes | — | Ticket id. |
| `glpi_validation_users_id` | yes | — | GLPI **user id** of the validator (`users_id_validate`). That user needs the appropriate validation rights in GLPI. |
| `glpi_validation_comment` | no | omitted | If non-empty, sent as `comment_submission`. |

### Other optional (every action)

| Variable | Default | Description |
|----------|---------|-------------|
| `glpi_validate_certs` | `true` | Passed to `ansible.builtin.uri` for TLS verification. |

---

## Outputs and chaining

- **`set_stats`:** `data.incident` is set to the new ticket id as a **string** (useful for Ansible Tower / AAP job metadata, similar to a ServiceNow incident number field name).
- **`set_fact`:** After a successful open, `glpi_last_ticket_id` is set on the host so later tasks in the **same play** can call the role again with `glpi_incident_id: "{{ glpi_last_ticket_id }}"` for `close_incident`, `add_ticket_task`, `add_ticket_document`, `add_ticket_solution`, or `request_ticket_approval`.

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

Chaining in one play after `open_incident` (same API URL and auth for each call):

```yaml
    glpi_action: add_ticket_task
    glpi_incident_id: "{{ glpi_last_ticket_id }}"
    glpi_task_content: "<p>Follow-up check in 24h.</p>"
```

Ensure the same API URL, app token, and auth vars are available for every call.

---

## Integration test

Under `tests/`, `test.yml` opens a ticket, runs **add task → add solution → request approval → attach document (file upload) → close**, using `glpi_last_ticket_id` between steps.

1. Edit **`tests/vars.yml`** once (API URL, TLS, requester id, validation user id, test strings — no secrets).
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
- **Close fails or GLPI demands a solution type:** Set `glpi_solutiontypes_id` to a valid id from your GLPI instance (also usable on `add_ticket_solution` when required).
- **Document upload `ERROR_UPLOAD_FILE_TOO_BIG_POST_MAX_SIZE` with a tiny file:** Usually a malformed multipart request (e.g. file part not sent as real file bytes). This role uses the mapping form under `filename[0]` so Ansible reads the path from the controller disk. If the error persists, raise PHP **`post_max_size`** / **`upload_max_filesize`** and GLPI **maximum document size** (**Setup → General → Management**).
- **Ticket validation fails:** Confirm `glpi_validation_users_id` is a user allowed to receive validation requests for that ticket type, and that your API profile has **Create** validation rights where needed.
- **TLS errors in a lab:** `-e 'glpi_validate_certs=false'` (avoid in production).

For defaults and inline comments, see `defaults/main.yml`. For behaviour details, see `tasks/*.yaml` (`open_incident`, `close_incident`, `add_ticket_task`, `add_ticket_document`, `add_ticket_solution`, `request_ticket_approval`).
