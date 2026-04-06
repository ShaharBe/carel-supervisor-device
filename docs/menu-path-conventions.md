# Menu Path Conventions

Menu `path` values are stable identifiers, not presentation numbering.

Key rules:

- Do not renumber paths just to keep them consecutive.
- Menu order is determined by the JSON `children` array order, not by numeric continuity of the path.
- It is valid to remove a node like `1` and still have the first visible top-level menu start at `2`.
- Some application logic depends on specific paths remaining stable.

Examples of path-sensitive behavior:

- `dashboard_sync` annotations in `display_menu.json` (see below)
- path-specific scale/limit overrides for `2.1`, `2.3`, `2.4` in `menu_service.py`
- dynamic menu visibility rules that reference another node by its `path`
- unit/profile logic that references a specific controlling node by `path`

## Dashboard sync mapping

Nodes whose live value should appear in the menu widget during periodic
dashboard refreshes carry a `"dashboard_sync"` property in
`display_menu.json`.  The value is a dot-path into the `/api/temp`
response payload (e.g. `"last_setpoint_c"`, `"info.humidifier_status"`).

At page load the backend collects these annotations into
`dashboard_sync_map` and includes it in the page payload.  The frontend
reads this map once at init time and uses it in
`syncMenuCacheFromDashboard()` — no hardcoded path strings in JS.

To add a new dashboard-synced menu node:

1. Add `"dashboard_sync": "<payload_key>"` to the node in
   `display_menu.json`.
2. Ensure the `/api/temp` response already includes the value at that key.
3. No Python or JS code changes required.

Current mappings:

| Menu path | `dashboard_sync` key | Description |
|---|---|---|
| `2.1` | `last_setpoint_c` | Setpoint |
| `2.2` | `info.humidifier_network_enabled` | Humidifier on/off |
| `2.3` | `max_production_pct` | Max production |
| `2.4` | `prop_band_c` | Proportional band |
| `4.1` | `info.humidifier_status` | Humidifier status |
| `4.6` | `info.conductivity` | Conductivity |
| `5.2` | `info.cyl1_hours` | Cylinder 1 hours |
| `5.4` | `device_time_display` | Device date/time |
| `6.2` | `info.cyl1_status` | Cylinder 1 status |
| `6.3` | `info.cyl1_phase` | Cylinder 1 activity |

## Safe changes

- reordering sibling nodes in the JSON `children` array
- removing a node without renumbering the remaining paths
- adding new nodes with new unique paths

## Risky changes

- renumbering existing paths after deleting or moving a node
- changing a controlling node path without updating all path references in code and JSON rules
