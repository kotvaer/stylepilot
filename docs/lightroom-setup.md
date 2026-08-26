# Lightroom Classic POC setup

StylePilot uses the checked-out
[`kotvaer/lightroom-mcp`](https://github.com/kotvaer/lightroom-mcp) fork of
[`Automaat/lightroom-mcp`](https://github.com/Automaat/lightroom-mcp). The fork
retains the upstream MCP/socket bridge and adds the guarded
`create_virtual_copy` operation required by the real apply workflow.

## Prerequisites

- Lightroom Classic on macOS or Windows;
- Node.js and npm;
- the Python environment installed with `uv sync`.

## 1. Build the fork

Clone this project with its submodule and build the MCP server:

```bash
git submodule update --init --recursive
cd vendor/lightroom-mcp/server
npm ci
npm run build
```

The Python runtime starts `vendor/lightroom-mcp/server/dist/index.js` directly,
so the server contract is tied to the reviewed submodule commit rather than a
floating npm package.

## 2. Install the StylePilot plugin

Copy the fork's `plugin/LightroomMCP.lrplugin` directory into Lightroom's
Modules directory. On macOS:

```bash
cp -R vendor/lightroom-mcp/plugin/LightroomMCP.lrplugin \
  "$HOME/Library/Application Support/Adobe/Lightroom/Modules/"
```

Restart Lightroom Classic completely. Open **File → Plug-in Manager → Lightroom
MCP — StylePilot**, then click **Start Server**.

## 3. Configure the Lightroom workspace

Copy `.env.example` to the Git-ignored `.env`, configure the selected model,
then create Lightroom's non-secret launcher file:

```bash
uv run stylepilot lightroom configure-panel
```

The command writes `~/.config/stylepilot/lightroom-runtime.json` with absolute
paths to the current `stylepilot` executable, `.env`, preview directory, and
panel-result directory. It does not copy the API key. Relative private-prompt
paths in `.env` are resolved from the `.env` directory, so Lightroom does not
depend on the process working directory.

Use `--default-profile /absolute/path/profile.json` to make the native panel
use a built Style Profile by default. Re-run the command after moving the
checkout, replacing the virtual environment, or changing that default.

## 4. Use StylePilot directly in Lightroom

1. Open Lightroom's **Library** module and select exactly one photo.
2. Choose **Library → Plug-in Extras → StylePilot — Open Workspace**.
3. Choose **Analyze selected photo** for a read-only proposal.
4. Choose **Apply to virtual copy...** to analyze, review the request-bound
   approval panel, and optionally authorize a virtual-copy edit.

The panel starts the local Python runtime only for the current request and
displays scene classification, suitability, proposed Develop settings,
verification, and rollback status. No Codex session or terminal command is
required. A model request can take tens of seconds; both buttons stay disabled
until the result is written atomically back to Lightroom.

The CLI and external Codex/MCP entry points use the same workflow and remain
available. The Lightroom panel does not run a persistent daemon, but the Lua
socket bridge still supports one client at a time, so do not overlap a panel
job with a CLI Lightroom job.

## 5. Diagnose the connection

```bash
uv run stylepilot lightroom doctor
```

A ready system reports:

```json
{
  "server_version": "0.9.0",
  "missing_tools": [],
  "plugin_connected": true
}
```

If `plugin_connected` is false, keep Lightroom open and start the server from
Plug-in Manager. The plugin uses authenticated localhost sockets on ports 58763
and 58764.

`doctor` also sends a non-mutating cancellation probe with a nonexistent
request ID. An `Unknown action: cancel_stylepilot_approval` error means the MCP
server was rebuilt but Lightroom is still running an older copied Lua plugin;
repeat step 2 and reload or restart Lightroom.

## 6. Run the real read-only analysis from CLI/Codex

Select one photo in Lightroom and run:

```bash
uv run stylepilot lightroom inspect
```

Optional style-target controls:

```bash
uv run stylepilot lightroom inspect \
  --style-name "Bright Clean" \
  --target-luminance 65 \
  --target-chroma 22 \
  --minimum-score 45
```

This command reads the selected photo and metadata, asks Lightroom to export a
2048 px sRGB JPEG, computes objective image metrics, and produces a suitability
report and proposed Develop plan. It passes `apply=false` and performs no
Lightroom catalog or Develop writes.

Generated previews live under `.stylepilot/previews/` and are ignored by Git.

## 7. Apply to a virtual copy from CLI/Codex

```bash
uv run stylepilot lightroom inspect --apply-to-virtual-copy
```

The workflow creates a named virtual copy, records its returned catalog ID,
creates a unique Develop Snapshot, and applies the proposed global settings
only through the StylePilot-specific strict write tool. The source photo is
analysis input but is never an authorized write target.

Before creating the copy, the command opens the native **StylePilot — Review
Edit** floating panel in Lightroom. It shows the request's selected photo,
target style, suitability score, recommended strength, proposed Develop
settings, and risk notes. The CLI waits up to 180 seconds for one of two
request-bound decisions:

- **Reject** exits without creating a virtual copy or changing the catalog;
- **Approve virtual-copy edit** grants authorization only to the displayed
  request and continues the guarded write path.

Bring Lightroom Classic to the foreground while the CLI waits; the floating
panel stays above Lightroom, not above unrelated applications. When the
deadline expires, the client calls `cancel_stylepilot_approval`. The plugin
marks the still-pending request `client_cancelled` and closes the panel so an
orphaned request cannot block the next workflow. Recopy and reload the plugin
after upgrading the fork because this cancellation handler runs in Lua.

Closing the approval window safely rejects the pending request. The workspace
can be opened from **Library → Plug-in Extras → StylePilot — Open Workspace**.
The bridge refuses to treat an approval for a different request ID as valid.

After approval, Lightroom renders a fresh JPEG from the edited virtual copy.
StylePilot recomputes objective metrics and verifies:

- shadow and highlight clipping remain within the Style Profile limits;
- normalized luminance/chroma style distance does not materially regress;
- a safe but unmeasurable change is reported as a warning rather than a false
  failure.

Failed hard conditions restore the Snapshot automatically and return
`status: "rolled_back"`. Successful results include both `rendered_metrics` and
the complete `verification` report.

The JSON result contains a recovery token:

```json
{
  "recovery_snapshot": {
    "photo_id": "38216",
    "name": "StylePilot Before b8c9967f..."
  }
}
```

If the write raises or returns an invalid response, the bridge automatically
restores this Snapshot. It can also be restored after the CLI process exits:

```bash
uv run stylepilot lightroom rollback \
  --photo-id 38216 \
  --snapshot-name "StylePilot Before b8c9967f..."
```

## Current safety boundary

The fork narrows Lightroom's current selection to the requested source before
calling `catalog:createVirtualCopies`, requires exactly one result, and returns
the new photo identifier. The Python bridge adds an independent guard: it only
permits `set_stylepilot_develop_settings` for virtual-copy IDs created by the
same bridge instance. Supplying the original ID is rejected before the write
tool is called.

The final Lua boundary adds another independent check using Lightroom's
`isVirtualCopy` metadata. The StylePilot write contract accepts only eleven
numeric global controls and repeats the Python min/max validation. Every write
is preceded by a unique native Develop Snapshot; failures trigger a named
compensating restore. The snapshot is retained for explicit later recovery.

The CLI apply action is only intent to begin the review flow; it is not itself
write authorization. A request-bound decision from the native Lightroom panel
is required, and library callers cannot enter the write node with `apply=True`
alone.
