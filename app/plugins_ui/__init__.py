"""Plugin-exported UI for the ADMIN host.

A plugin declares its UI in ``ui.json`` (schema 1, see
``grinning-cat-core/docs/plugins/plugin-ui/README.md``). This package validates
that manifest, discovers the components addressed to ADMIN, and imports a page
module only when the page is rendered.

Submodules are imported explicitly so that ``app.plugins_ui.manifest`` can be
used without pulling in Streamlit:

* :mod:`app.plugins_ui.manifest` - pydantic models and ``load_manifest``;
* :mod:`app.plugins_ui.discovery` - ``discover`` and ``AdminComponent``;
* :mod:`app.plugins_ui.registry` - ``PluginPageRegistry`` and the default registry;
* :mod:`app.plugins_ui.context` - ``PluginPageContext`` and ``build_context``.
"""
