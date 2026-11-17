# Grinning Cat Admin UI

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/Grinning-Cat-RAG/grinning-cat-admin)

A modern web-based administration interface for the [Grinning Cat Core](https://github.com/Grinning-Cat-RAG/grinning-cat-core) framework. This admin UI provides a user-friendly interface to manage, configure, and monitor your Grinning Cat AI assistant instances.

## Features

- 🎯 **Dashboard Overview** - Real-time monitoring of Grinning Cat instances
- 🔧 **Configuration Management** - Easy-to-use interface for system settings
- 🧩 **Plugin Management** - Install, configure, and manage plugins
- 👥 **User Management** - Handle user accounts and permissions
- 📊 **Analytics & Logs** - Monitor conversations and system performance
- 🔐 **Authentication & Security** - Secure access control and user management
- 🎨 **Modern UI** - Clean, responsive interface built with modern web technologies

## Prerequisites

- Python 3.11 or higher
- pip and venv for dependency management
- Running instance of [Grinning Cat Core](https://github.com/Grinning-Cat-RAG/grinning-cat-core)

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/Grinning-Cat-RAG/grinning-cat-admin.git
cd grinning-cat-admin
```

### 2. Create and activate virtual environment

```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

### 3. Install dependencies

```bash
make install
```

### 4. Set up environment variables

Copy the example environment file and configure it:

```bash
cp .env.example .env
```

Edit `.env` with your settings.

### 5. Run the application

```bash
make run
```

The admin interface will be available at `http://localhost:8501`

## Configuration

### Connecting to Grinning Cat Core

The admin UI connects to your Grinning Cat Core instance via REST API. Configure the connection in your `.env` file:

```env
GRINNING_CAT_API_HOST=your-grinning-cat-instance
GRINNING_CAT_API_PORT=1865
GRINNING_CAT_API_SECURE_CONNECTION=false
```

### Authentication

The admin interface supports two ways of authenticating against Grinning Cat Core:

- **Credentials**: username and password are exchanged for a JWT (plus a
  refresh token) on the Core instance. Both are kept in the browser's
  localStorage, and the permissions of the logged-in user decide which sections
  are reachable. The access token is renewed automatically shortly before its
  `exp` claim, and after a page reload once it has expired, using the
  single-use refresh token (which is rotated at every renewal). When the
  refresh token is refused or expires, the user is sent back to the login form.
  With several tabs open in the same browser, the tokens stored by another tab
  are read before every renewal, so a tab never presents an already-rotated
  refresh token (which the Core would treat as theft). Logging out from one tab
  logs out the others too, immediately (the Core only revokes the refresh
  token, so the access token of the other tabs would otherwise keep working
  until its `exp`).
  **Logout** revokes the session on the Core instance, not only in the browser.
  The login form reports wrong credentials and rate limiting (`429`) with a
  readable message.
- **API key**: set `GRINNING_CAT_API_KEY` to the `CAT_API_KEY` of your Core
  instance. The login form is skipped and every call is made with that key, so
  the whole UI is available to anyone who can reach it. Leave it unset unless
  the UI is only exposed to trusted users.

## Batch agent updates

`scripts/batch_agent_update.py` updates many agents in one run. It selects agents by a case-insensitive substring of `agent_id` (or every non-system agent with `--all`), then applies LLM, plugin, metadata and factory-setting patches through the official SDK and REST API. Nothing is written unless you pass `--yes`: the default is a dry run that prints the plan and exits with code 2.

### Prerequisites

The script reads the same environment as the admin UI:

```env
GRINNING_CAT_API_HOST=your-grinning-cat-instance
GRINNING_CAT_API_PORT=1865
GRINNING_CAT_API_KEY=your-api-key
GRINNING_CAT_API_SECURE_CONNECTION=false
```

`GRINNING_CAT_API_KEY` is required; the other three fall back to `localhost`, `1865` and `false`. The key must carry the permissions for every resource the run touches: CHESHIRE_CAT (read and write), LLM, PLUGIN, CHUNKER, VECTOR_DATABASE, AUTH_HANDLER, AGENTIC_WORKFLOW, CONTEXT_RETRIEVER and FILE_MANAGER. Note that the core gates the single-setting chunker GET/PUT on AUTH_HANDLER, so a run that patches `chunking` needs that permission too. `--allow-system-wide` patches the single-setting embedder GET/PUT, which the core gates on EMBEDDER (`check_permissions(AuthResource.EMBEDDER, AuthPermission.READ)` / `...WRITE` in `cat/routes/embedder.py`); add SYSTEM only when the run also touches system endpoints.

### Flags

| Flag | Description |
| --- | --- |
| `--agent SUBSTRING` | agent id or a case-insensitive substring of it (repeatable) |
| `--all` | every agent except system and `__*` |
| `--create-missing` | create an agent when `--agent` matches nothing (never with `--all`) |
| `--exclude SUBSTRING` | drop agents whose id contains this substring, case-insensitive (repeatable) |
| `--llm NAME=JSON` | merge a JSON object into the LLM configuration NAME from `GET /llm/settings` (repeatable) |
| `--plugin NAME=on\|off` | enable or disable a plugin (repeatable) |
| `--metadata KEY=VALUE` | merge one metadata key (repeatable) |
| `--name VALUE` | shortcut for `--metadata name=VALUE` |
| `--set CATEGORY:NAME:KEY=VALUE` | patch one factory setting (repeatable) |
| `--patch FILE` | YAML file (`.yml` or `.yaml`) with per-category patches and `active_plugins` |
| `--yes` | apply the changes (default: dry-run) |
| `--yaml` | print the result as YAML |
| `--backup-out DIR` | directory for the YAML before-snapshot |
| `--restore PATH` | restore a YAML snapshot file or directory |
| `--allow-system-wide` | allow system-wide categories such as embedder |
| `--max-workers INT` | parallel workers, one task per agent (default: 1) |

### Patch syntax

`--set CATEGORY:NAME:KEY=VALUE` patches one factory setting. `CATEGORY` is one of `llm`, `chunking`, `vector_database`, `auth_handler`, `agentic_workflow`, `context_retriever`, `file_manager`, `plugins`, `metadata`, `embedder` or `ingestion`. `NAME` is the setting name (for `llm` the configuration name from `GET /llm/settings`, for `plugins` the plugin id). `KEY` may be dotted (`a.b.c`) to reach nested values. Values are decoded as numbers, booleans, null or collections when possible, otherwise kept as strings.

#### LLM configuration names

The accepted `--llm` / `--set llm:<name>` value is the **configuration name reported by `GET /llm/settings`** — the JSON-schema `title`, i.e. the class name such as `LLMOpenRouterConfig`, `LLMOpenAIChatConfig` or `LLMDefaultConfig`. The friendly label shown in ADMIN (e.g. "OpenAI ChatGPT") is `humanReadableName` and is **not** accepted. If you pass an unknown name, the API error lists all valid ones (`X not supported. Must be one of [...]`); that list is the authoritative source.

`--patch FILE` takes a **YAML** file (`.yml` or `.yaml`) keyed by category, then by name. Any other suffix is rejected before the file is read. JSONC `//` comments are no longer accepted; migrate them to `#` once:

```yaml
# one category per top-level key, then one entry per setting name
llm:
  LLMOpenAIChatConfig:
    temperature: 0.2
plugins:
  foo:
    k: v
metadata:
  name: X
active_plugins:
  foo: on   # reconciled like --plugin
  bar: off
```

The file must contain exactly one YAML document (no `---` separators); YAML anchors and aliases are allowed. Scalars are read without YAML implicit typing, then a leaf is converted only when it is a valid number, boolean or null literal: `label: on` stays the string `on`, `date: 2024-01-01` stays a string, and `1.10` becomes the number `1.1`.

`active_plugins` is the one top-level key that is **not** a category: it maps a plugin name to `on` or `off` (`true`/`false`/`1`/`0` work too, case-insensitive) and produces the same operation as `--plugin`, so it reconciles the current state and never toggles twice. Anything else is a usage error. When the same plugin is named both in the file and on the command line, the explicit `--plugin NAME=on|off` **wins** over the file value.

### How the patches are applied

- `--plugin NAME=on|off` reconciles: it reads the active flag and toggles only when the current state differs, then verifies the result. The core endpoint flips the flag rather than setting it, so a repeated command never toggles twice.
- `--llm` and `--set` do a read-merge-write, because the core replaces the whole settings value on PUT. Only the keys you pass change; everything else is preserved, including plugin settings, whose server-side merge is shallow.
- `--metadata` merges into the existing metadata dict, for the same reason.

### Safety

- Dry run by default. `--yes` is required to write anything.
- Before writing, each agent gets a YAML snapshot (`<agent_id>-<sha1_8>.yml`) under `--backup-out` (default `./agent-backup-<UTC stamp>`); the directory also gets a `manifest.yml`.
- `--restore <dir|file>` re-applies a YAML snapshot (a directory of `*.yml`/`*.yaml` files or a single file) and is itself a dry run unless you add `--yes`.
- `--create-missing` is opt-in and never applies to `--all`: only an `--agent` substring that matches nothing becomes a new agent. A created agent has no before-state, so it is not snapshotted.
- `--allow-system-wide` is required for `embedder` and `ingestion`, because those settings re-embed every agent.
- `--max-workers > 1` runs one task per agent in parallel.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | every operation succeeded |
| 1 | at least one agent or operation failed |
| 2 | dry run, abort or usage error |

### Examples

Dry run against every agent whose id contains `prod`:

```bash
uv run python scripts/batch_agent_update.py \
  --agent prod \
  --set llm:LLMOpenAIChatConfig:temperature=0.2
```

Apply a plugin toggle and a metadata label to every non-system agent, keeping a snapshot in `./backup`:

```bash
uv run python scripts/batch_agent_update.py \
  --all \
  --plugin foo=on \
  --metadata label=b \
  --yes \
  --backup-out ./backup
```

Create an agent that does not exist yet, then configure it in the same run:

```bash
uv run python scripts/batch_agent_update.py \
  --agent nuovo \
  --create-missing \
  --patch examples/patch-full-agent.yml
```

`--create-missing` only applies to `--agent` substrings that match nothing; a substring that matches at least one existing agent never creates. It cannot be combined with `--all` or the dump flags (`--dump`, `--diff`, `--dump-only`, `--dump-secrets`, `--dump-manifest-only`). With `--restore`, it creates the agents the snapshot reports as missing before restoring them.

#### Patch file examples

Two ready-to-use YAML files ship in `examples/`; copy one, adapt the names and values, and run it. A patch file is a small YAML tutorial in itself:

- `#` starts a comment. Quote any value containing `#` (`api_key: "sk-abc#def"`), otherwise the rest of the line is a comment.
- Scalars are not implicitly typed: `on` stays the string `on`, `0123` stays `"0123"`, `2024-01-01` stays a string; `1.10` becomes the number `1.1`.
- One document per file: no `---` separators. Anchors and aliases are allowed.
- `active_plugins` maps a plugin name to `on`/`off` and reconciles like `--plugin`.
- Never commit a real key: prefer `--set llm:<name>:api_key=...`, an environment variable, or a patch file you do not commit.

| File | What it shows |
| --- | --- |
| [`examples/patch-llm-openrouter.yml`](examples/patch-llm-openrouter.yml) | a single LLM change on the `LLMOpenRouterConfig` configuration used from ADMIN (`model` must be a valid OpenRouter id) |
| [`examples/patch-full-agent.yml`](examples/patch-full-agent.yml) | the "everything" shape: metadata, LLM, chunker, plugin settings and the active plugin set |

```bash
uv run python scripts/batch_agent_update.py \
  --agent corso \
  --patch examples/patch-full-agent.yml
```

### Dump and reload a configuration

`--dump DIR` exports the selected agents' current configuration as YAML in the same grammar as a patch file, so a dump can be edited and reloaded with `--patch`. The dump is read-only: it never writes to the core.

```bash
uv run python scripts/batch_agent_update.py --dump ./dump --all
```

Each selected agent gets one file named `DIR/<agent_id>-<sha1_8>.yml`, where `<sha1_8>` is the first eight hex characters of the SHA-1 of the agent id, so the name stays filesystem-safe and collision-free. A `DIR/manifest.yml` lists every agent with its role, file, categories, selected configurations, plugins, warnings and the number of omitted secrets.

Every dump file starts with a YAML comment header: `dump-format`, `generated-at`, `agent-id`, `agent-role`, `categories`, `selected`, `argv`, `source-host`, `secret-policy` and `warnings`. The header documents how and when the file was produced; the body below it is a normal patch document.

The `agent-role` is `system` for the system agent (which also carries the system-wide `embedder` and `ingestion` settings), `widget` for ids starting with `__`, and `agent` for everything else. `--all` includes `system` and the `__*` widgets in a dump.

Secret-looking fields (`api_key`, `*_secret`, `password`, `*_token`) are omitted by default; pass `--dump-secrets` to include them. Omitted fields are preserved on reload because the patch merges into the existing configuration, so a dump without secrets is still safe to reload.

To reload, apply the file with `--patch`, dry-run first:

```bash
uv run python scripts/batch_agent_update.py --agent corso --patch ./dump/corso-1a2b3c4d.yml
```

Reloading a `system` dump that contains `embedder` or `ingestion` requires `--allow-system-wide`, because those settings re-embed every agent.

`--diff` compares an existing dump with the live configuration and never writes: it exits 0 when the dump matches and 1 when it differs.

```bash
uv run python scripts/batch_agent_update.py --dump ./dump --all --diff
```

`--dump-only CATEGORY` restricts the dump to one category (repeatable). `--dump-manifest-only` writes only `manifest.yml`, without the per-agent files.

## Contributing

We welcome contributions! Please see our [Contributing Guide](CONTRIBUTING.md) for details.

### Development Workflow

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/amazing-feature`
3. Make your changes and add tests, then run them with `make test`
4. Commit your changes: `git commit -m 'Add amazing feature'`
5. Push to the branch: `git push origin feature/amazing-feature`
6. Open a Pull Request

## License

This project is licensed under [GPL3](LICENSE).

## Support

- 🐛 [Issue Tracker](https://github.com/Grinning-Cat-RAG/grinning-cat-admin/issues)
- 📧 [Email Support](mailto:matteo.cacciola@gmail.com)

## Acknowledgments

- [Grinning Cat Core](https://github.com/Grinning-Cat-RAG/grinning-cat-core) - The AI framework this admin interface manages
- [FastAPI](https://fastapi.tiangolo.com/) - Modern web framework for building APIs

---

**Made with ❤️ for the Grinning Cat community**
