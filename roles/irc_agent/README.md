# irc_agent — OpenShift deploy for the Ergo + LLM IRC bot

This Ansible role deploys a small Python bot that connects to **Ergo** (or any compatible IRCd), joins a channel, and uses an **OpenAI-compatible** HTTP API (`/v1/chat/completions`) to **decide** whether each line deserves a public reply (not only when the bot is mentioned). **Phase 2** adds an in-process **MCP client**: allowed servers and caps are defined in a rendered **`mcp.json`** (`ConfigMap`), tools are discovered at startup, and the model may call them via OpenAI-style **`tools` / `tool_calls`** when it answers (after the JSON “respond?” step). Optional **`IRC_REPLY_ONLY_WHEN_MENTIONED=1`** restores the older behaviour (only `!a …` or nick in the line). It is meant to run **next to** the `irc_stack` role (Ergo in one OpenShift project, the bot in another, or the same; cross-namespace Service DNS is normal).

**License:** SPDX GPL-2.0-or-later (see role files).

---

## What you need first

1. **OpenShift (or Kubernetes) access** with permission to create Projects, ConfigMaps, Secrets, Deployments, and Jobs in the target namespace.
2. **Ansible** 2.12+ and the **`kubernetes.core`** collection on the control node (the machine where you run `ansible-playbook`).
3. **Cluster credentials** available to Ansible the same way as `oc` / `kubectl` (usually `~/.kube/config` after `oc login`, or `KUBECONFIG`).
4. **Ergo already deployed** (for example with the `irc_stack` role), and a **channel** the bot should use.
5. An **LLM HTTP endpoint** that implements **OpenAI-style** chat completions, and an **API key** (or equivalent bearer token) if the service requires it.

---

## 1. Install the Ansible collection

On the host where you run the playbook:

```bash
ansible-galaxy collection install kubernetes.core
```

---

## 2. Log in to the cluster

```bash
oc login https://<api-server>:<port> -u <user> -t <token>
# or: export KUBECONFIG=/path/to/your/kubeconfig
```

Confirm you can read namespaces used by your stack, for example:

```bash
oc get project,ns 2>/dev/null | head -20
oc get configmap/ergo-ircd -n <irc_stack_namespace>  # optional: Ergo ircd ConfigMap
```

The role (by default) reads the live `ircd.yaml` from ConfigMap `ergo-ircd` in the **irc_stack** namespace to verify that **public NickServ registration** is allowed when auto-provisioning is enabled. If you have not deployed `irc_stack` yet, deploy it first or disable inspection (see environment variables below).

---

## 3. Choose connection values

### IRC (Ergo Service, in-cluster)

With `irc_stack` defaults, the Ergo Service is `ergo` on port `6667` in the **irc_stack** namespace (e.g. `chat-irc-stack`):

- **Host (DNS):** `ergo.<irc_stack_namespace>.svc` or `ergo.<irc_stack_namespace>.svc.cluster.local`
- **Port:** `6667` (plaintext) when Ergo is exposed the same way as in `irc_stack`
- **TLS:** for that plaintext in-cluster path, set **no TLS** (role default `IRC_AGENT_IRC_TLS=0`).

Map these to the role (environment or `defaults/main.yml`):

| Variable | Environment variable | Meaning |
|----------|----------------------|--------|
| `irc_agent_irc_host` | `IRC_AGENT_IRC_HOST` | Ergo **Service** hostname (required) |
| `irc_agent_irc_port` | `IRC_AGENT_IRC_PORT` | Usually `6667` |
| `irc_agent_irc_channel` | `IRC_AGENT_IRC_CHANNEL` | Channel, e.g. `#it-operations` (required) |
| `irc_agent_irc_nick` | `IRC_AGENT_IRC_NICK` | Bot nickname (must match the NickServ account) |
| `irc_agent_irc_tls` | `IRC_AGENT_IRC_TLS` | `0` = plaintext, `1` = TLS to Ergo |

### LLM (HTTP API)

| Variable | Environment variable | Meaning |
|----------|----------------------|--------|
| `irc_agent_llm_base_url` | `IRC_AGENT_LLM_BASE_URL` | Base URL ending with `/v1` (required), e.g. `https://api.openai.com/v1` |
| `irc_agent_llm_api_key` | `IRC_AGENT_LLM_API_KEY` | **Bearer** token for `Authorization: Bearer ...` (required) |
| `irc_agent_llm_model` | `IRC_AGENT_LLM_MODEL` | Model name (default `gpt-4o-mini` if you use OpenAI) |
| `irc_agent_llm_timeout` | `IRC_AGENT_LLM_TIMEOUT` | Seconds (string in defaults) |
| `irc_agent_llm_max_input_tokens` | `IRC_AGENT_LLM_MAX_INPUT_TOKENS` / **`IRC_LLM_MAX_INPUT_TOKENS`** (ConfigMap) | Estimated input token budget for **messages + tools JSON** before each completion. The bot trims long **tool result** text, channel/system text, then **compacts `tools[]`** (shorter descriptions → minimal JSON schemas → drops tools from the end, keeping at least one). Default **32000** (set **0** to disable). Use **28000** on strict ~40k models if the gateway still errors. |

**Container-only env** (set on the Deployment if you customize it; not templated by default): `LLM_SYSTEM` (persona + appended JSON reply rules), `IRC_REPLY_ONLY_WHEN_MENTIONED` (`1` = only react to `!a …` or nick mention; default off). Default mode calls the LLM **once per channel line** from others, so traffic volume maps directly to API usage.

**Conversation context** (templated by default; override with play vars or `IRC_AGENT_IRC_CONTEXT_*` when running Ansible): the bot keeps a **rolling in-memory transcript** of recent channel lines plus its own replies for this TCP session (`IRC_CONTEXT_MAX_MESSAGES`, `IRC_CONTEXT_MAX_CHARS`, `IRC_CONTEXT_DISABLED=1` to turn off). Context is **lost on reconnect** to Ergo; it is not persisted to disk.

### MCP (Phase 2)

The role materializes **`templates/mcp.json.j2`** into ConfigMap **`irc-agent-mcp`** (key `mcp.json`) and mounts it at **`/etc/mcp/mcp.json`**. The pod sets **`MCP_CONFIG_PATH`** and **`IRC_AGENT_MCP_ENABLED`** (`1`/`0` from `irc_agent_mcp_enabled`). There is **one audit surface**: that JSON (servers, optional `ircAgent.allowedTools`, and token/round-trip caps). **Secrets** stay out of the ConfigMap: the AAP bearer token is **`AAP_MCP_SERVER_TOKEN`** in the `irc-agent-credentials` **Secret** (from Ansible var or `AAP_MCP_SERVER_TOKEN` / `IRC_AGENT_AAP_MCP_SERVER_TOKEN` in the environment when you run the playbook).

| Variable | Environment (playbook) | Meaning |
|----------|------------------------|--------|
| `irc_agent_mcp_enabled` | `IRC_AGENT_MCP_ENABLED` | `false`/`0` disables MCP loading (no tool calls) |
| `irc_agent_aap_mcp_server_url` | `IRC_AGENT_AAP_MCP_SERVER_URL` or **`AAP_MCP_SERVER_URL`** | Streamable **HTTP** MCP base URL for the bundled AAP logical servers (empty = no AAP HTTP entries in `mcp.json`) |
| `irc_agent_aap_mcp_server_token` | `IRC_AGENT_AAP_MCP_SERVER_TOKEN` or **`AAP_MCP_SERVER_TOKEN`** | Bearer token for those HTTP servers (required if the URL is set and MCP is enabled) |
| `irc_agent_mcp_kubernetes_stdio` | (play var only) | If `true`, adds **`kubernetes-mcp-server`** (`npx …`) to `mcp.json`; requires **Node/npx** in the container image (not provided by default `python:3.12-slim`) |
| `irc_agent_mcp_allowed_tools` | (play var only) | If non-empty, only those tool names (OpenAI-safe names or MCP tool names) are exposed to the model |
| `irc_agent_mcp_max_tool_roundtrips` / `*_result_chars` / `*_catalog_chars` | (play var → `mcp.json` `ircAgent`) | Caps for tool loops, tool result size, and catalog size in the “respond?” system prompt |
| `irc_agent_mcp_first_tool_choice` | `IRC_MCP_FIRST_TOOL_CHOICE` / `IRC_AGENT_MCP_FIRST_TOOL_CHOICE` | Default **`auto`** (omit forcing `tool_choice` on turn one). Use **`required`** only if your gateway supports it and you want a guaranteed first tool call. |
| `irc_agent_mcp_http_transport` | `IRC_MCP_HTTP_TRANSPORT` | **`streamable`** (default) or **`sse`** for MCP HTTP servers that only expose the legacy SSE transport. |

**Behaviour:** The bot loads MCP config once at startup, runs **`tools/list`** per distinct HTTP URL (shared session for tool calls) and per stdio server, builds OpenAI **`tools[]`**, and injects a short **catalog** into the JSON decision step so the model knows which integrations exist. When it answers with tools, **`call_tool`** is mapped to the MCP Python client (`mcp` package). The tool pass **does not stop at “checking…”**: it nudges hollow replies, runs an extra **plain** completion to turn tool output into IRC text when needed, and the JSON step may use **`"message":""`** when tools will supply the answer. **Ansible Automation Platform** operators typically set **`AAP_MCP_SERVER_URL`** and **`AAP_MCP_SERVER_TOKEN`** (or the `IRC_AGENT_AAP_MCP_*` aliases) before `ansible-playbook`.

**AAP URL and token in the pod:** The role always renders **`AAP_MCP_SERVER_URL`** and **`IRC_AGENT_AAP_MCP_SERVER_URL`** (same value) on the **`irc-agent-env`** ConfigMap, and **`AAP_MCP_SERVER_TOKEN`** plus **`IRC_AGENT_AAP_MCP_SERVER_TOKEN`** on the **`irc-agent-credentials`** Secret, so `envFrom` always injects those keys (values may be empty until you set play vars or patch the objects). At runtime, **`mcp_client`** replaces the HTTP base URL in **`mcp.json`** for every server that uses **`auth.bearerTokenEnv: AAP_MCP_SERVER_TOKEN`** when **`AAP_MCP_SERVER_URL`** or **`IRC_AGENT_AAP_MCP_SERVER_URL`** is set in the environment—so you can point the bot at an in-cluster route (e.g. `http://ansible-mcp.aap.svc.cluster.local`) without re-rendering the ConfigMap. Bearer auth reads **`AAP_MCP_SERVER_TOKEN`** or **`IRC_AGENT_AAP_MCP_SERVER_TOKEN`**. Bare URLs with no path get **`/mcp`** appended by default (override with **`IRC_MCP_HTTP_PATH`** set to `-` to disable, or another path if your gateway differs).

### OpenShift / role behaviour

| Variable | Environment variable | Meaning |
|----------|----------------------|--------|
| `irc_agent_namespace` | `IRC_AGENT_NAMESPACE` | Project/namespace for the **bot** (default `chat-irc-agent`) |
| `irc_agent_create_project` | `IRC_AGENT_CREATE_PROJECT` | `true`/`1` to create the OpenShift `Project` (default on) |
| `irc_agent_auto_provision_ergo_account` | `IRC_AGENT_AUTO_PROVISION` | `true` = one-shot **NickServ REGISTER** Job + random password; `false` = you set `irc_agent_irc_password` (manual account) |
| `irc_agent_fail_on_ergo_seed` | `IRC_AGENT_FAIL_ON_ERGO_SEED` | `false` (default): a failed/timeout **registration Job** does not block creating the **Secret** and **Deployment**; set `true` to fail the play if the Job does not succeed |
| `irc_agent_ergo_ircd_namespace` | `IRC_AGENT_ERGO_IRCD_NAMESPACE` | Where **Ergo** `ConfigMap/ergo-ircd` lives (default `chat-irc-stack`) for live `ircd.yaml` check |
| `irc_agent_ergo_inspect_ircd` | `IRC_AGENT_ERGO_INSPECT_IRCD` | `false` to skip reading that ConfigMap (e.g. irc_stack not in cluster yet) |
| `irc_agent_ergo_ircd_configmap` | `IRC_AGENT_ERGO_IRCD_CONFIGMAP` | Default `ergo-ircd` (must match irc_stack) |
| `irc_agent_ergo_ircd_config_key` | `IRC_AGENT_ERGO_IRCD_CONFIG_KEY` | Key inside the ConfigMap (default `ircd.yaml`) |

When **auto-provision** is on and the `irc-agent-credentials` **Secret** already exists in the bot namespace, the role **reuses** the stored `IRC_PASSWORD` and **does not** re-run the registration Job.

When **auto-provision** is off, you must set `IRC_AGENT_IRC_PASSWORD` to the existing NickServ password for the chosen nick.

---

## 4. Export environment variables (example)

Minimal example: Ergo in `chat-irc-stack`, bot in default namespace, public registration enabled on Ergo, OpenAI API:

```bash
export IRC_AGENT_IRC_HOST='ergo.chat-irc-stack.svc'
export IRC_AGENT_IRC_CHANNEL='#it-operations'
export IRC_AGENT_IRC_NICK='MyLlmBot'
# Optional if defaults match: IRC_AGENT_NAMESPACE, IRC_AGENT_ERGO_IRCD_NAMESPACE

export IRC_AGENT_LLM_BASE_URL='https://api.openai.com/v1'
export IRC_AGENT_LLM_API_KEY='sk-...'   # treat as a secret; use a vault in production

# Optional: pin model and timeouts
export IRC_AGENT_LLM_MODEL='gpt-4o-mini'
export IRC_AGENT_LLM_TIMEOUT='120'
```

If the bot namespace must be created and you are not using defaults, set:

```bash
export IRC_AGENT_NAMESPACE='chat-irc-agent'
```

To **disable** automatic NickServ registration and use an account you created yourself (or via oper `SAREGISTER` when registration is closed in `ircd.yaml`):

```bash
export IRC_AGENT_AUTO_PROVISION='0'
export IRC_AGENT_IRC_PASSWORD='your-nickserv-password'
```

---

## 5. Run the test playbook (recommended first run)

The role ships a playbook under `tests/` that sets `roles_path` so the role is found as `irc_agent` when you run the playbook from that directory.

```bash
cd /path/to/demos.utils/roles/irc_agent/tests
# Optional: load example exports (edit LLM key first; remove the file when done)
source ./generate_test_env.sh
ansible-galaxy collection install kubernetes.core
ansible-playbook -i inventory test.yml
```

What the role does in order (non–check mode):

1. Asserts **IRC host, channel, LLM base URL, LLM key** (and, if manual account mode, **IRC password**).
2. Creates the **OpenShift Project** (if `irc_agent_create_project` is true).
3. Reads **`ergo-ircd` ConfigMap** in the irc_stack namespace (unless disabled) to ensure **public registration** is not disabled if auto-provision is on.
4. Reuses an existing **Secret** or runs a one-shot **Job** to **`NickServ REGISTER`** the bot, then stores credentials in a **Secret**.
5. Creates **ConfigMaps** (bot code + env) and a **Deployment**; waits for the **Pod** to become **Ready**.

---

## 6. (Optional) Dry run syntax check

To validate the playbook and role variable wiring without a cluster (limited; some `kubernetes.core` tasks need a real API in full runs):

```bash
ansible-playbook -i inventory test.yml --syntax-check
```

A full `ansible-playbook -C` (check mode) run still expects a resolvable API and existing namespaces for some `kubernetes` calls; for a real deploy, run **without** `-C` after `oc login`.

---

## 7. Verify the deployment

```bash
oc get deploy,pods -n "${IRC_AGENT_NAMESPACE:-chat-irc-agent}"
oc logs -n "${IRC_AGENT_NAMESPACE:-chat-irc-agent}" -l "app=irc-agent" -f
```

In IRC (e.g. via Convos in `irc_stack`), join the same channel, mention the bot nick in a line or use `!a <question>`; the bot should reply with a short LLM answer (if the LLM API is reachable from the pod).

---

## 8. Use the role from another playbook

In a playbook in your repository, add the `roles` path and reference the role:

```yaml
- hosts: localhost
  connection: local
  gather_facts: false
  roles:
    - role: irc_agent
  vars:   # or use group_vars, vault, or -e @extra.yml
    irc_agent_irc_host: "ergo.chat-irc-stack.svc"
    irc_agent_irc_channel: "#it-operations"
    irc_agent_llm_base_url: "https://api.openai.com/v1"
    irc_agent_llm_api_key: !vault |
          $ANSIBLE_VAULT;...
```

Or rely on **`IRC_AGENT_*` environment variables**; they override `defaults/main.yml` and most play vars, as set in `tasks/init.yaml`.

---

## Troubleshooting (short)

- **Playbook fails on “registration.enabled false”** with auto-provision: your live `ircd.yaml` has closed public registration. Create the account (e.g. with oper `SAREGISTER` / manual registration), set `IRC_AGENT_IRC_PASSWORD`, and set `IRC_AGENT_AUTO_PROVISION=0`, then re-run.
- **Cannot pull `python:3.12-slim`**: your cluster may restrict registries. Set `irc_agent_image` and `irc_agent_ergo_seed_image` to allowed mirrors (e.g. via `IRC_AGENT_IMAGE` / `IRC_AGENT_ERGO_SEED_IMAGE` if you wire them in `init`, or by play `vars` — the seed image is also in `defaults` as `irc_agent_ergo_seed_image`).
- **Bot in CrashLoop, logs show connection errors to Ergo**: recheck `IRC_HOST` / `IRC_PORT` / `IRC_TLS` against the in-cluster **Service** for Ergo (and cross-namespace DNS if the bot is not in the irc_stack namespace).
- **LLM 401/403**: check `LLM_BASE_URL` (must be the `/v1` base; the app calls `…/v1/chat/completions`) and `LLM_API_KEY`.
- **Namespace not found in check mode**: a full install requires the project to exist or `irc_agent_create_project: true` to create it; run a real `ansible-playbook` after `oc login` with a user that can create the project if needed.

For the full list of **defaults and behaviour**, see `defaults/main.yml`, `tasks/init.yaml`, `tasks/main.yaml`, and `tasks/ergo_*.yaml`.
