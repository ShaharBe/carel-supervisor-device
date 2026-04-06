# Resolved-Editor Architecture

How menu-editor metadata flows from the backend to the frontend.

---

## Problem

Before this change the backend and frontend each independently derived
editor metadata (type, options, scale, limits, editability) for every
menu node.  `menu_service.py` had the authoritative inference functions
(`infer_menu_editor_type`, `infer_menu_numeric_scale`,
`infer_menu_numeric_limits`, `normalize_editor_options`, etc.) but the
frontend duplicated similar logic in `menu-widget.js`
(`getLeafEditor`, `inferNumericEditorType`, `isNumericRangeHint`,
`isMenuNodeEditable`, etc.).

This created two problems:

1. **Duplication** — every inference rule existed in two languages with no
   guarantee they stayed in sync.
2. **Extra round-trips or stale UI** — the frontend had to compute
   editability and form shape before or alongside a value read, sometimes
   using incomplete information.

## Solution

A single resolver on the backend (`resolve_node_editor`) builds one
`resolved_editor` object per menu node.  This object is attached to
the node in two places:

1. The **page-load menu tree** — so the UI has full metadata from the
   first render, with zero extra requests.
2. Every **`/api/menu-value` response** — so metadata stays fresh after
   any value read or write.

## Data Flow

```
┌──────────────────────────────────────────────────────┐
│                      Backend                         │
│                                                      │
│  display_menu.json ──► load_display_menu()           │
│                            │                         │
│                    annotate_menu_tree(root)           │
│                            │                         │
│                  ┌─────────┴──────────┐              │
│                  │  resolve_node_     │              │
│                  │  editor(node)      │              │
│                  │  ┌───────────────┐ │              │
│                  │  │ type          │ │              │
│                  │  │ options       │ │              │
│                  │  │ scale         │ │              │
│                  │  │ limits        │ │              │
│                  │  │ step          │ │              │
│                  │  │ editable      │ │              │
│                  │  │ modbus_backed │ │              │
│                  │  │ writable      │ │              │
│                  │  └───────────────┘ │              │
│                  └────────────────────┘              │
│                            │                         │
│              ┌─────────────┼─────────────┐           │
│              ▼                           ▼           │
│     GET / (index)              GET/POST              │
│     menu tree JSON             /api/menu-value       │
│     with resolved_editor       serialize_menu_value  │
│     on every node              includes              │
│                                resolved_editor       │
└──────────────┬─────────────────────┬─────────────────┘
               │                     │
               ▼                     ▼
┌──────────────────────────────────────────────────────┐
│                     Frontend                         │
│                                                      │
│  parseMenuPayload()                                  │
│  └─ every node already carries resolved_editor       │
│                                                      │
│  getLeafEditor(node)                                 │
│  └─ prefers node.resolved_editor when present        │
│  └─ falls back to local inference for safety         │
│                                                      │
│  isMenuNodeModbusBacked(node)                        │
│  isMenuNodeWritable(node)                            │
│  isMenuNodeEditable(node)                            │
│  └─ all prefer resolved_editor flags                 │
│                                                      │
│  fetchMenuNodeValue(node)                            │
│  └─ merges resolved_editor from API response         │
│     onto both runtime and base node                  │
└──────────────────────────────────────────────────────┘
```

## `resolved_editor` Shape

```jsonc
{
  // Editor type derived by infer_menu_editor_type().
  // One of: "boolean", "integer", "float", "enum", or null.
  "type": "float",

  // Normalized option list.  Populated for boolean and enum types.
  // Each entry has { "value": <any>, "label": <string> }.
  "options": [],

  // Numeric scaling factor (raw_register_value / scale = display_value).
  // Defaults to 1.0 for integer types, 10.0 for float types.
  // Path-specific overrides exist for setpoint (10), prop band (10), etc.
  "scale": 10.0,

  // Numeric limits as { "low": <float>, "high": <float> }, or null
  // when no limits can be determined.
  "limits": { "low": -20.0, "high": 100.0 },

  // HTML input step attribute: "any" for float, "1" for integer,
  // or null for non-numeric types.  Can be overridden by an explicit
  // editor.step in the JSON definition.
  "step": "any",

  // Whether the UI should offer an edit control for this node.
  // false for menus, captions, page_links, and read-only Modbus leaves.
  "editable": true,

  // Whether the node is mapped to a Modbus register or coil.
  "modbus_backed": true,

  // Whether the register allows writes (access == "R/W").
  "writable": true
}
```

## Resolver Functions

All live in `menu_service.py`.

| Function | Purpose |
|---|---|
| `resolve_node_editor(node)` | Builds the `resolved_editor` dict for one node |
| `annotate_menu_tree(root)` | Walks the tree and attaches `resolved_editor` to every node |
| `infer_menu_editor_type(node)` | Determines `type` from register family, hints, and explicit editor |
| `infer_menu_numeric_scale(node, editor_type)` | Determines `scale` from path overrides, hints, and defaults |
| `infer_menu_numeric_limits(node)` | Determines `limits` from path overrides and range hints |
| `normalize_editor_options(node)` | Normalizes explicit or hint-derived option lists |
| `parse_choice_tokens(text)` | Splits comma- or slash-separated option strings |

## Where Each Surface Gets Metadata

| Surface | Source | Timing |
|---|---|---|
| Page load (menu tree) | `annotate_menu_tree()` in the `GET /` route | Once, at page render |
| Value read | `serialize_menu_value()` in `GET /api/menu-value` | Every value fetch |
| Value write | `serialize_menu_value()` in `POST /api/menu-value` | Every write response |

## Frontend Fallback

The frontend retains its local inference functions (`inferNumericEditorType`,
`parseChoiceTokens`, etc.) as a fallback.  `getLeafEditor()` checks for
`node.resolved_editor` first; if absent it runs the old local path.  This
means:

- Nodes cloned at runtime (e.g. `rebuildRuntimeMenuTree`) that lose the
  property will still render correctly.
- A future removal of the fallback code is safe once the backend is the
  only source of truth and all nodes are guaranteed to carry the property.

## Adding a New Editor Field

1. Compute the value in `resolve_node_editor()` inside `menu_service.py`.
2. Add it to the returned dict.
3. The frontend will receive it automatically via both the page-load tree
   and the `/api/menu-value` API.
4. Add a test in `tests/test_resolved_editor.py`.
