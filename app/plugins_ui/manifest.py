"""Validation of the plugin UI manifest (``ui.json``, schema 1).

The normative contract lives in
``grinning-cat-core/docs/plugins/plugin-ui/README.md`` and is encoded in the
JSON Schema next to it. This module is the ADMIN-side loader: it validates the
manifest and the ADMIN module paths, and it never imports plugin code.

Size limits are enforced before the manifest is parsed and before an ADMIN
module is read, so an oversized file is rejected without being loaded.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

Destination = Literal["admin_system", "admin_agent", "instructor", "chatbot"]

ADMIN_DESTINATIONS: frozenset[str] = frozenset({"admin_system", "admin_agent"})
RITA_DESTINATIONS: frozenset[str] = frozenset({"instructor", "chatbot"})

SLOTS_BY_DESTINATION: dict[str, frozenset[str]] = {
    "instructor": frozenset({"instructor.tab", "instructor.panel"}),
    "chatbot": frozenset({"chatbot.sidebar", "chatbot.header"}),
}

AuthResourceName = Literal[
    "SYSTEM",
    "CHESHIRE_CAT",
    "EMBEDDER",
    "MEMORY",
    "CHAT",
    "SETTING",
    "LLM",
    "AGENTIC_WORKFLOW",
    "AUTH_HANDLER",
    "FILE_MANAGER",
    "CHUNKER",
    "CONTEXT_RETRIEVER",
    "VECTOR_DATABASE",
    "USERS",
    "UPLOAD",
    "PLUGIN",
    "ANALYTICS",
]
PermissionName = Literal["READ", "WRITE", "DELETE"]

PLUGIN_ID_PATTERN = r"^[a-z0-9_]{1,64}$"
COMPONENT_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
MODULE_PATTERN = re.compile(r"^[A-Za-z0-9._/-]+$")

MAX_MANIFEST_BYTES = 64 * 1024
MAX_ADMIN_MODULE_BYTES = 512 * 1024
MAX_COMPONENTS = 20
MAX_CAT_ENDPOINTS = 50
MAX_ENDPOINT_LENGTH = 512

ALLOWED_METHODS: frozenset[str] = frozenset({"GET", "POST"})
FORBIDDEN_ENDPOINT_FRAGMENTS: tuple[str, ...] = ("://", "?", "#", "..", "//", "\r", "\n")


class PluginUiError(Exception):
    """A plugin UI manifest or module violates the contract."""


def validate_endpoint(entry: str) -> str:
    """Validate one ``"METHOD /path"`` allowlist entry and return it unchanged."""
    if not isinstance(entry, str):
        raise ValueError("cat_endpoints entries must be strings")
    if len(entry) > MAX_ENDPOINT_LENGTH:
        raise ValueError(
            f"endpoint is longer than {MAX_ENDPOINT_LENGTH} characters: {entry[:40]!r}..."
        )

    method, separator, path = entry.partition(" ")
    if not separator:
        raise ValueError(f"endpoint must be 'METHOD /path': {entry!r}")
    if method not in ALLOWED_METHODS:
        raise ValueError(f"endpoint method must be GET or POST: {entry!r}")
    if not path.startswith("/"):
        raise ValueError(f"endpoint path must start with '/': {entry!r}")

    for fragment in FORBIDDEN_ENDPOINT_FRAGMENTS:
        if fragment in path:
            raise ValueError(f"endpoint path contains forbidden {fragment!r}: {entry!r}")
    if "*" in path and not path.endswith("*"):
        raise ValueError(f"'*' is allowed only as the final character: {entry!r}")
    if any(character.isspace() or ord(character) < 32 for character in path):
        raise ValueError(f"endpoint path contains whitespace or control characters: {entry!r}")
    return entry


class Permission(BaseModel):
    """The ``has_access`` pair an ADMIN component requires."""

    model_config = ConfigDict(extra="forbid")

    resource: AuthResourceName
    permission: PermissionName


class Launch(BaseModel):
    """How a RITA host draws the launcher button for a component."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=120)
    icon: str = Field(max_length=16)
    mode: Literal["panel", "modal"]


class Component(BaseModel):
    """One UI component declared by a plugin."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=COMPONENT_ID_PATTERN)
    title: str = Field(min_length=1, max_length=120)
    icon: str | None = Field(default=None, max_length=16)
    destination: Destination
    module: str = Field(min_length=1, max_length=512)
    slot: str | None = None
    permission: Permission | None = None
    cat_endpoints: list[str] | None = Field(default=None, max_length=MAX_CAT_ENDPOINTS)
    launch: Launch | None = None
    order: int | None = Field(default=None, ge=0, le=100000)

    @field_validator("cat_endpoints")
    @classmethod
    def _validate_cat_endpoints(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return [validate_endpoint(entry) for entry in value]

    @model_validator(mode="after")
    def _validate_destination_contract(self) -> "Component":
        if self.destination in ADMIN_DESTINATIONS:
            if self.permission is None:
                raise ValueError(
                    f"component '{self.id}': permission is required for "
                    f"destination '{self.destination}'"
                )
            if self.slot is not None:
                raise ValueError(
                    f"component '{self.id}': slot is forbidden for "
                    f"destination '{self.destination}'"
                )
            if self.cat_endpoints is not None:
                raise ValueError(
                    f"component '{self.id}': cat_endpoints is forbidden for "
                    f"destination '{self.destination}'"
                )
            if self.launch is not None:
                raise ValueError(
                    f"component '{self.id}': launch is forbidden for "
                    f"destination '{self.destination}'"
                )
            return self

        allowed_slots = SLOTS_BY_DESTINATION[self.destination]
        if self.slot is None:
            raise ValueError(
                f"component '{self.id}': slot is required for destination "
                f"'{self.destination}'"
            )
        if self.slot not in allowed_slots:
            raise ValueError(
                f"component '{self.id}': slot '{self.slot}' is not valid for "
                f"destination '{self.destination}' (allowed: "
                f"{', '.join(sorted(allowed_slots))})"
            )
        if self.cat_endpoints is None:
            raise ValueError(
                f"component '{self.id}': cat_endpoints is required (it may be "
                f"empty) for destination '{self.destination}'"
            )
        if self.launch is None:
            raise ValueError(
                f"component '{self.id}': launch is required for destination "
                f"'{self.destination}'"
            )
        if self.permission is not None:
            raise ValueError(
                f"component '{self.id}': permission is forbidden for "
                f"destination '{self.destination}'"
            )
        return self


class UiManifest(BaseModel):
    """The whole ``ui.json`` document."""

    model_config = ConfigDict(extra="forbid")

    # The JSON key is `schema`; the attribute cannot use that name because it
    # would shadow `BaseModel.schema`.
    schema_version: Literal[1] = Field(alias="schema")
    plugin_id: str = Field(pattern=PLUGIN_ID_PATTERN)
    components: list[Component] = Field(max_length=MAX_COMPONENTS)

    @model_validator(mode="after")
    def _validate_unique_component_ids(self) -> "UiManifest":
        seen: set[str] = set()
        for component in self.components:
            if component.id in seen:
                raise ValueError(f"duplicate component id '{component.id}'")
            seen.add(component.id)
        return self


def _format_validation_error(error: ValidationError) -> str:
    parts: list[str] = []
    for item in error.errors():
        location = ".".join(str(part) for part in item.get("loc", ())) or "<root>"
        parts.append(f"{location}: {item.get('msg', 'invalid value')}")
    return "; ".join(parts)


def _validate_module_path(plugin_dir: Path, component: Component) -> Path:
    """Validate ``component.module`` and return the resolved path inside the plugin."""
    module = component.module
    if module.startswith("/"):
        raise PluginUiError(
            f"component '{component.id}': module must be relative, got '{module}'"
        )
    if ".." in module:
        raise PluginUiError(
            f"component '{component.id}': module must not contain '..', got '{module}'"
        )
    if "//" in module:
        raise PluginUiError(
            f"component '{component.id}': module must not contain '//', got '{module}'"
        )
    if not MODULE_PATTERN.match(module):
        raise PluginUiError(
            f"component '{component.id}': module contains unsupported characters: '{module}'"
        )

    resolved = (plugin_dir / module).resolve()
    if not resolved.is_relative_to(plugin_dir):
        raise PluginUiError(
            f"component '{component.id}': module resolves outside the plugin directory: '{module}'"
        )

    if component.destination in ADMIN_DESTINATIONS:
        if not module.endswith(".py"):
            raise PluginUiError(
                f"component '{component.id}': module must be a .py file for "
                f"destination '{component.destination}'"
            )
        if not resolved.is_file():
            raise PluginUiError(
                f"component '{component.id}': admin module not found: '{module}'"
            )
        size = resolved.stat().st_size
        if size > MAX_ADMIN_MODULE_BYTES:
            raise PluginUiError(
                f"component '{component.id}': admin module is {size} bytes, "
                f"the limit is {MAX_ADMIN_MODULE_BYTES}"
            )
    elif not module.endswith((".js", ".mjs")):
        raise PluginUiError(
            f"component '{component.id}': module must be a .js or .mjs file for "
            f"destination '{component.destination}'"
        )
    return resolved


def load_manifest(plugin_dir: Path) -> UiManifest:
    """Load and validate ``<plugin_dir>/ui.json``.

    Raises :class:`PluginUiError` with a precise message on any contract
    violation. The manifest size is checked before parsing and ADMIN module
    sizes are checked before the module is ever read.
    """
    plugin_dir = Path(plugin_dir).resolve()
    if not plugin_dir.is_dir():
        raise PluginUiError(f"plugin directory not found: {plugin_dir}")

    manifest_path = plugin_dir / "ui.json"
    if not manifest_path.is_file():
        raise PluginUiError(f"ui.json not found in {plugin_dir}")

    size = manifest_path.stat().st_size
    if size > MAX_MANIFEST_BYTES:
        raise PluginUiError(
            f"ui.json is {size} bytes, the limit is {MAX_MANIFEST_BYTES}"
        )

    try:
        raw = manifest_path.read_bytes()
    except OSError as error:
        raise PluginUiError(f"cannot read {manifest_path}: {error}") from error

    try:
        data: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PluginUiError(f"ui.json is not valid UTF-8 JSON: {error}") from error

    try:
        manifest = UiManifest.model_validate(data)
    except ValidationError as error:
        raise PluginUiError(_format_validation_error(error)) from error

    if manifest.plugin_id != plugin_dir.name:
        raise PluginUiError(
            f"plugin_id '{manifest.plugin_id}' does not match the directory "
            f"name '{plugin_dir.name}'"
        )

    for component in manifest.components:
        _validate_module_path(plugin_dir, component)

    return manifest
