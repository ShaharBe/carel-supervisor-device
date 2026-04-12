# Resource Cache Architecture

The app uses one canonical cache entry for each physical CAREL/Modbus value.
Menu paths remain UI locators only.

## Motivation

The older implementation had two independent cache owners for some values:

- dashboard fields such as `cache.info_cyl1_status`
- menu-path entries such as `cache.menu_values["6.2"]`

That allowed split-brain UI behavior. For example, menu path `6.2` and the
dashboard field `info.cyl1_status` both represent register `I:140`. If the
menu read refreshed `I:140` to `3` while the dashboard cache still held `2`,
the frontend could flicker between the two values.

The fix is to make the physical value identity explicit.

## Identities

There are two separate identifiers:

| Identifier | Example | Meaning |
|---|---|---|
| Menu path | `6.2` | Where the item appears in the menu tree |
| Resource key | `I:140` | The physical/controller value |

The resource key is derived automatically from a menu node's `register`
metadata:

```text
{family}:{index}
```

Examples:

```text
A:19
I:140
D:54
```

Do not add resource keys manually to `display_menu.json` unless there is a
strong future reason. The JSON register field is the source.

## Cache Shape

Canonical values live in `runtime.cache.resource_values`:

```python
cache.resource_values["I:140"] = {
    "raw": 3,
    "value": 3,
    "source": "poll",
    "updated_utc": "...",
    "error": None,
}
```

`source` describes how the latest value arrived:

| Source | Meaning |
|---|---|
| `poll` | Background poll block read |
| `modbus` | Direct menu/API read |
| `write` | Successful write path |

The source is not a user-action history. It only describes the latest cache
update.

## Data Flow

Menu reads and writes still use menu paths at the API boundary:

```text
GET /api/menu-value?path=6.2
POST /api/menu-value { "path": "3.3.1.1", "value": 6 }
```

Internally the backend resolves:

```text
path -> menu node -> register -> resource key
```

So:

```text
6.2       -> I:140
4.1       -> I:136
3.3.1.1   -> I:136
```

This means aliases share the same cached value. `4.1` and `3.3.1.1` are
different UI leaves, but both represent `I:136`.

## Dashboard Payloads

`/api/temp` keeps its existing dashboard-shaped fields for compatibility, but
for overlapping Modbus values it prefers the canonical resource cache.

Example:

```text
info.cyl1_status -> resource_values["I:140"].value
```

The response also includes lightweight freshness metadata:

```json
{
  "resources": {
    "I:140": {
      "updated_utc": "...",
      "source": "poll",
      "error": null
    }
  }
}
```

The frontend does not currently need to compare timestamps. This metadata is
primarily for debugging and future hardening.

## Dashboard Sync

`dashboard_sync` remains a frontend convenience:

```json
"dashboard_sync": "info.cyl1_status"
```

It means:

```text
This menu path can be populated from /api/temp because both refer to the same
canonical resource.
```

It does not create an independent value owner.

## Locking

Keep locking simple:

- `modbus_lock` serializes all controller reads and writes.
- `cache_lock` protects short in-memory cache access.
- Do not add per-register locks.
- Do not hold `cache_lock` while waiting for Modbus.
- `interactive_modbus_priority` is a scheduling hint, not the correctness
  mechanism.

The preferred order is:

```text
modbus_lock -> Modbus operation -> release -> cache_lock -> cache update
```

If a path updates cache while still holding `modbus_lock`, keep the ordering
consistent and never acquire `cache_lock` first and then wait for `modbus_lock`.

## Maintenance Checklist

When adding a new dashboard-synced Modbus value:

1. Add or confirm the menu node has a `register` field.
2. Add `dashboard_sync` only if `/api/temp` should populate the menu value.
3. Update the relevant poll/app serialization mapping so `/api/temp` prefers
   `resource_values["family:index"]`.
4. Add or update a test in `tests/test_resource_cache.py` if the sync map
   changes.

When adding a non-Modbus dashboard value, such as date/time, document it as an
intentional non-resource sync entry.
