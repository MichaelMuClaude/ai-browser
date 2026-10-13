# Hermes native tool integration for ai-browser

`ai_browser_tool.py` is a thin wrapper that registers native Hermes tool functions
under `toolset="ai_browser"`:

`ai_browser_search`, `ai_browser_navigate`, `ai_browser_click`, `ai_browser_scroll`,
`ai_browser_look`, `ai_browser_type`, `ai_browser_clear_field`, `ai_browser_go_back`,
`ai_browser_go_forward`, `ai_browser_open_tab`, `ai_browser_switch_tab`,
`ai_browser_list_tabs`, `ai_browser_scratchpad_add`, `ai_browser_scratchpad_get`,
`ai_browser_scratchpad_clear`, `ai_browser_smart_fetch`

These are Hermes-native tools, not MCP. They call the local ai-browser HTTP daemon
(`http://127.0.0.1:8888` by default) through its `BrowserClient`.

## Configuration

- `AI_BROWSER_SRC`: path to the ai-browser `src/` directory. Defaults to the
  `src/` folder of the checkout this file belongs to.
- `AI_BROWSER_BASE_URL`: daemon address. Defaults to `http://127.0.0.1:8888`.

## Installing into a hermes-agent installation

Hermes loads native tools from its own `tools/` directory at startup, so this file
has to be placed (or symlinked) there:

1. Start the ai-browser daemon (`python -m src.main server`).
2. Find the hermes-agent `tools/` directory in your installation.
3. Link or copy the file:

   ```bash
   ln -s /path/to/ai-browser/integrations/hermes/ai_browser_tool.py <tools_dir>/ai_browser_tool.py
   ```

4. Enable the `ai_browser` toolset for the agent profile that should use it.
5. Confirm with a real browsing request and check the tool calls in Hermes' state
   database.

A reinstall or upgrade of hermes-agent can remove files placed inside its package,
so re-check the link after upgrades.

This wrapper was verified against hermes-agent 0.18.2's `tools/registry.py`. If a
later hermes-agent version changes the registration API, the wrapper will need
updating.
