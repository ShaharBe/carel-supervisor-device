from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


NUMBERED_LINE_RE = re.compile(r"^([\d.]+)\.\s*(.+)$")
REGISTER_RE = re.compile(r"\(([AID]),\s*(\d+),\s*(R/W|R)\b")
CAPTION_PREFIX_RE = re.compile(r"^\(Caption\):?\s*", re.IGNORECASE)


def _empty_menu_root() -> dict[str, Any]:
    return {
        "path": "",
        "title": "Root",
        "display_label": "Root",
        "raw_text": "Root",
        "kind": "root",
        "children": [],
    }


def _candidate_menu_paths() -> list[Path]:
    candidates: list[Path] = []

    configured_path = os.environ.get("CAREL_DISPLAY_MENU_PATH", "").strip()
    if configured_path:
        candidates.append(Path(configured_path))

    bundled_json_path = Path(__file__).resolve().parent / "data" / "display_menu.json"
    candidates.append(bundled_json_path)

    repo_docs_path = Path(__file__).resolve().parents[1] / "docs" / "display panel menues.txt"
    candidates.append(repo_docs_path)
    candidates.append(Path(r"C:\Freelance Projects\CarelSupervisor\docs\display panel menues.txt"))

    unique_candidates: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        unique_candidates.append(path)

    return unique_candidates


def resolve_menu_definition_path() -> Path | None:
    for path in _candidate_menu_paths():
        if path.is_file():
            return path
    return None


def _load_menu_root_from_json(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("menu JSON root must be an object")
    if "children" not in loaded:
        raise ValueError("menu JSON root must include 'children'")
    return loaded


def _split_note(raw_text: str) -> tuple[str, str | None]:
    if "Note:" not in raw_text:
        return raw_text.strip(), None

    body, note = raw_text.split("Note:", 1)
    return body.strip(), note.strip() or None


def _extract_display_label(raw_text: str, is_caption: bool) -> str:
    text = CAPTION_PREFIX_RE.sub("", raw_text).strip()
    tokens = [" [", "[", " (A,", " (I,", " (D,", " (Stub", " ???"]

    cut_index = len(text)
    for token in tokens:
        token_index = text.find(token)
        if token_index != -1:
            cut_index = min(cut_index, token_index)

    label = text[:cut_index].strip(" :-")
    if label:
        return label

    if is_caption:
        return "Status Caption" if text.startswith("[") else "Caption"

    return raw_text.strip()


def _extract_register(raw_text: str) -> dict[str, Any] | None:
    match = REGISTER_RE.search(raw_text)
    if not match:
        return None

    return {
        "family": match.group(1),
        "index": int(match.group(2)),
        "access": match.group(3),
    }


def _extract_bracket_hint(raw_text: str) -> str | None:
    match = re.search(r"\[([^\]]+)\]", raw_text)
    if not match:
        return None
    return match.group(1).strip() or None


def _detect_page_direction(display_label: str) -> str | None:
    normalized = display_label.strip().lower().replace(".", "")
    if normalized == "next page":
        return "next"
    if normalized == "prev page":
        return "prev"
    return None


def _build_node(path: str, raw_text: str) -> dict[str, Any]:
    body_without_note, note = _split_note(raw_text)
    is_caption = CAPTION_PREFIX_RE.match(body_without_note) is not None
    display_label = _extract_display_label(body_without_note, is_caption=is_caption)

    return {
        "path": path,
        "title": display_label,
        "display_label": display_label,
        "raw_text": raw_text,
        "kind": "leaf",
        "children": [],
        "visible": True,
        "register": _extract_register(raw_text),
        "range_or_options": _extract_bracket_hint(raw_text),
        "note": note,
        "is_caption": is_caption,
        "is_stub": any(token in raw_text for token in ("(Stub", "TBD", "???")),
        "page_direction": _detect_page_direction(display_label),
    }


def _finalize_tree(node: dict[str, Any]) -> None:
    for child in node["children"]:
        _finalize_tree(child)

    if node["path"] == "":
        node["kind"] = "root"
        return

    if node["children"]:
        node["kind"] = "menu"
        return

    if node["page_direction"]:
        node["kind"] = "page_link"
        return

    if node["is_caption"]:
        node["kind"] = "caption"
        return

    if node["is_stub"]:
        node["kind"] = "stub"
        return

    node["kind"] = "leaf"


def parse_menu_definition(text: str) -> dict[str, Any]:
    root = _empty_menu_root()
    nodes_by_path: dict[str, dict[str, Any]] = {"": root}

    numbered_entries: list[tuple[str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.lower() == "root":
            continue

        match = NUMBERED_LINE_RE.match(stripped)
        if not match:
            continue

        numbered_entries.append((match.group(1), match.group(2).strip()))

    for path, raw_text in numbered_entries:
        nodes_by_path[path] = _build_node(path=path, raw_text=raw_text)

    for path, _ in numbered_entries:
        parent_path = ".".join(path.split(".")[:-1])
        parent = nodes_by_path.get(parent_path, root)
        parent["children"].append(nodes_by_path[path])

    _finalize_tree(root)
    return root


def load_display_menu() -> dict[str, Any]:
    source_path = resolve_menu_definition_path()
    if source_path is None:
        return {
            "ok": False,
            "error": "Menu definition file was not found.",
            "source_path": None,
            "root": _empty_menu_root(),
        }

    try:
        if source_path.suffix.lower() == ".json":
            root = _load_menu_root_from_json(source_path)
        else:
            menu_text = source_path.read_text(encoding="utf-8-sig")
            root = parse_menu_definition(menu_text)
        return {
            "ok": True,
            "error": None,
            "source_path": str(source_path),
            "root": root,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Unable to parse menu definition: {exc}",
            "source_path": str(source_path),
            "root": _empty_menu_root(),
        }
