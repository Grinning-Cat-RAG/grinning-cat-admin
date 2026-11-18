# AGENTS.md

## Repository
`DEH-RAG/grinning-cat-admin`: Streamlit admin UI for Grinning Cat Core.

## Commands
- Install: `make install`
- Run: `make run` (http://localhost:8501)
- Test: `make test`; plugin-UI tests: `pytest tests/plugins_ui`

## Plugin-exported UI

ADMIN renders UI that plugins export through a `ui.json` manifest. The contract
(manifest shape, destinations, settings, limits) lives in the core docs at
`grinning-cat-core/docs/plugins/plugin-ui/`. Do not restate it here; link to it.

### Environment
| Variable | Default | Meaning |
| --- | --- | --- |
| `GRINNING_CAT_PLUGIN_UI_DIRS` | (empty) | `os.pathsep`-separated plugin directories to scan for `ui.json`. Empty disables plugin UI. |
| `GRINNING_CAT_PLUGIN_UI_REFRESH_SECONDS` | `60` | Discovery cache TTL. `0` disables the cache. |
| `GRINNING_CAT_PLUGIN_UI_REQUIRE_INSTALLED` | `true` | When true, a plugin must be active in CAT to be discovered. `false` only in dev. |

### Read-only mount
ADMIN only reads the plugin directories. Mount the shared plugin folder
read-only (`./data/grinning/plugins:/opt/cat-plugins:ro`) and point
`GRINNING_CAT_PLUGIN_UI_DIRS` at it. A plugin installed via upload/registry lands
in the core's plugin folder (the same directory), so it appears without a
rebuild.

### Destination gating
- `admin_system`: selected agent == `system` AND plugin active for `system` AND
  `has_access(permission)`.
- `admin_agent`: selected agent != `system` AND plugin active for that agent AND
  `has_access(permission)`.
- `instructor` and `chatbot` belong to RITA; ADMIN does not render them.

### Trust model
Plugin code is trusted: an ADMIN page runs in the same trust domain as the
plugin's Python inside CAT. Only mount plugin directories you control.

## Conventions
- Python: PEP8, type hints, `uv` for dependencies.
- Never commit secrets or API keys.
