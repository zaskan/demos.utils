# Ansible role: provision Demo Chat via API

This directory contains role **`chat_app`**, which creates users, channels, memberships, optional seed messages, and instance branding through the Demo Chat REST API. Creates are skipped when resources already exist (idempotent).

## Layout

- `roles/chat_app/` — role (tasks, defaults, meta)
- `playbooks/test.yml` — sample playbook
- `examples/test-vars.yml` — sample variables
- `inventory/hosts` — localhost for local runs

## Requirements

- Ansible 2.14+ on the controller
- Demo Chat API reachable from the controller (`urllib` via `ansible.builtin.uri`)
- An existing **admin** account (default seed: `admin` / `changeme`)

## Variable reference

| Variable | Description |
|----------|-------------|
| `chat_api_base` | Origin only, e.g. `http://localhost:8000` (no trailing slash). |
| `chat_validate_certs` | TLS verification (default `true`). |
| `chat_http_timeout` | HTTP timeout seconds (default `30`). |
| `chat_admin_username` / `chat_admin_password` | Admin login used for all admin API calls. |
| `chat_users` | List of `{ username, password, is_admin }` — users created only if the username does not already exist. |
| `chat_channels` | List of `{ name, ... }` — channels created only if the name does not exist. Optional `allow_anonymous_webhook` and `anonymous_webhook_username` are applied **after** memberships via `PATCH` (the webhook user must already be a **member** of that channel). |
| `chat_memberships` | List of `{ channel, username }` using channel **name** and username string — server already treats duplicate membership as a no-op (204). |
| `chat_seed_messages` | List of `{ channel, username, body }` — posts as that user; optional `password` on the entry. Password resolution: entry field → `chat_users` → admin username / `chat_admin_password`. **Idempotency:** if `body` already appears in the latest 200 messages, `POST` is skipped. |
| `chat_instance_settings` | Optional dict for `PATCH /api/v1/admin/settings` (e.g. `{ branding: { app_title: "..." } }`). A **GET** is performed first; **PATCH** runs only if the merged state would differ from the server. |

Password-bearing tasks use `no_log: true` where appropriate.

## Run the test playbook

From this directory:

```bash
ansible-playbook playbooks/test.yml -i inventory/hosts -e @examples/test-vars.yml
```

Run twice: second run should create nothing that already exists; seed messages and settings patches are skipped when already satisfied.

## Ordering

1. Admin login  
2. Users (create missing)  
3. Channels (create missing)  
4. Memberships  
5. Channel webhook `PATCH` (if configured in `chat_channels`)  
6. Seed messages (if any)  
7. Instance settings (if `chat_instance_settings` is non-empty)
