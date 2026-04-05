# Menu Path Conventions

Menu `path` values are stable identifiers, not presentation numbering.

Key rules:

- Do not renumber paths just to keep them consecutive.
- Menu order is determined by the JSON `children` array order, not by numeric continuity of the path.
- It is valid to remove a node like `1` and still have the first visible top-level menu start at `2`.
- Some application logic depends on specific paths remaining stable.

Examples of path-sensitive behavior:

- dashboard/menu cache sync for fixed paths such as `2.1`, `2.2`, `2.3`, `2.4`
- dynamic menu visibility rules that reference another node by its `path`
- unit/profile logic that references a specific controlling node by `path`

Safe changes:

- reordering sibling nodes in the JSON `children` array
- removing a node without renumbering the remaining paths
- adding new nodes with new unique paths

Risky changes:

- renumbering existing paths after deleting or moving a node
- changing a controlling node path without updating all path references in code and JSON rules
