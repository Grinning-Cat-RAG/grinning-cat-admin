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

## Plugin-exported UI

ADMIN renders UI that plugins export through a `ui.json` manifest. The contract
(manifest shape, destinations, settings, limits) is owned by the core docs:
[`docs/plugins/plugin-ui/`](https://github.com/Grinning-Cat-RAG/grinning-cat-core/tree/main/docs/plugins/plugin-ui).
This section documents only the ADMIN side.

### Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `GRINNING_CAT_PLUGIN_UI_DIRS` | (empty) | `os.pathsep`-separated plugin directories to scan for `ui.json`. Empty disables plugin UI. |
| `GRINNING_CAT_PLUGIN_UI_REFRESH_SECONDS` | `60` | Discovery cache TTL. `0` disables the cache (useful in dev/QA). |
| `GRINNING_CAT_PLUGIN_UI_REQUIRE_INSTALLED` | `true` | When true, a plugin must be active in CAT to be discovered. Set `false` only in dev. |

### Read-only mount

ADMIN only reads the plugin directories: it never writes to them. Mount the
shared plugin folder read-only, for example in compose:

```yaml
volumes:
  - ./data/grinning/plugins:/opt/cat-plugins:ro
environment:
  GRINNING_CAT_PLUGIN_UI_DIRS: /opt/cat-plugins
```

A plugin installed through upload or the registry lands in the core's plugin
folder, which is the same directory mounted here, so it becomes visible to ADMIN
without a rebuild.

### Destination gating

ADMIN renders two of the four destinations:

- `admin_system` only when the selected agent is the `system` agent, the plugin
  is active for `system`, and the user passes `has_access(permission)`.
- `admin_agent` only when the selected agent is not `system`, the plugin is
  active for that agent, and the user passes `has_access(permission)`.

The `instructor` and `chatbot` destinations belong to RITA and are not rendered
by ADMIN.

The loader validates the `rita` capability block on those components but ignores
it at runtime: ADMIN pages use `ctx.client` for data, not the RITA capability
bridge. The block is accepted so a shared `ui.json` validates the same way in
both hosts.

### Trust model

Plugin code is trusted: an ADMIN page runs in the same trust domain as the
plugin's Python inside CAT. Only mount plugin directories you control.

### Test

```bash
pytest tests/plugins_ui
```

## Batch maintenance moved to the `cat_batch` plugin

Batch agent configuration (dump, diff, plan, apply, reload, restore) and ingestion reindexing now live in the [`cat_batch`](https://github.com/DEH-RAG/cat-batch) plugin. The old `scripts/batch_agent_update.py` in this repository and `scripts/reindex_ingestion.py` in the core are gone; both are replaced by the plugin's `/custom/batch/v1/*` endpoints and by its thin operator CLIs.

All routes live under `/custom/batch/v1` (`/dump`, `/diff`, `/plan`, `/apply`, `/reload`, `/restore`, `/reingest`, `/jobs`, `/job`, `/job/cancel`) and require `AuthResource.SYSTEM` credentials. Long-running actions run as durable jobs, and every write needs a confirmed plan token; the plugin README documents the error model and the plan/confirm/idempotency flow.

The same operations are available inside a running container through the plugin's scripts directory (not executable, so call the interpreter):

```bash
# batch agent configuration: dry run by default, exit 2 without --yes
docker exec <container> python /app/cat/plugins/cat_batch/scripts/batch_agent_update.py \
  --agent <substring> --metadata label=production

# ingestion reindexing: dry run by default, exit 2 without --yes
docker exec <container> python /app/cat/plugins/cat_batch/scripts/reindex_ingestion.py \
  --agent <id|substring> --all
```

The flags are unchanged: selection by `--agent`/`--all`/`--exclude`, `--patch` YAML files, `--set`, `--plugin`, `--metadata`, `--dump`/`--diff`, `--restore`, `--reload-dir`, `--backup-out`, `--allow-system-wide` and `--max-workers`, with the same exit codes (0 = every operation succeeded, 1 = at least one failed, 2 = dry run, abort or usage error).

### Migration

An existing ADMIN dump stays valid: the YAML grammar and the `# agent-id:` header are unchanged, so it reloads through the plugin's `/reload` endpoint or the in-container CLI. Secrets are still omitted unless you ask for them, and omitted fields are preserved on reload because patches merge into the live configuration. Replace any old `scripts/batch_agent_update.py` invocation with the plugin CLI above or the matching endpoint; this repository ships no batch scripts anymore.

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
