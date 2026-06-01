"""
DOM accessibility tree extractor and fuzzy structural hasher.

Extracts only interactable elements from the AX tree via CDP.
Assigns stable deterministic IDs based on role+label hash (NOT DOM position).
Computes a fuzzy structural hash that survives minor CSS changes.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

try:
    import mmh3
    HAS_MMH3 = True
except ImportError:
    HAS_MMH3 = False

from terx.cdp.bridge import CDPBridge

logger = logging.getLogger(__name__)

# Roles that represent interactable elements
INTERACTABLE_ROLES = {
    "button", "link", "textbox", "searchbox", "combobox",
    "listbox", "option", "checkbox", "radio", "switch",
    "menuitem", "tab", "treeitem", "spinbutton", "slider",
    "scrollbar", "menuitemcheckbox", "menuitemradio",
}


@dataclass
class AXElement:
    """A single interactable element from the accessibility tree."""
    id: int                          # stable deterministic ID (hash-based)
    role: str
    label: str
    node_id: str                     # Chrome AX node ID
    backend_dom_id: int              # Chrome backend DOM node ID
    bounds: dict | None = None       # {x, y, width, height} if available
    depth: int = 0


@dataclass
class DOMSnapshot:
    """Full accessibility snapshot of a page at one point in time."""
    url: str
    title: str
    elements: list[AXElement]
    structural_hash: str             # fuzzy hash of the interactable tree
    role_sequence: str               # raw role sequence for similarity comparison
    element_count: int = field(init=False)

    def __post_init__(self) -> None:
        self.element_count = len(self.elements)

    def find_by_id(self, element_id: int) -> AXElement | None:
        for el in self.elements:
            if el.id == element_id:
                return el
        return None

    def find_by_label(self, label: str) -> AXElement | None:
        """Case-insensitive label match."""
        label_lower = label.lower()
        for el in self.elements:
            if label_lower in el.label.lower():
                return el
        return None


class DOMExtractor:
    """
    Extracts the accessibility tree from Chrome via CDP
    and returns a clean DOMSnapshot with stable element IDs.
    """

    async def snapshot(self, bridge: CDPBridge) -> DOMSnapshot:
        """Extract the full AX tree for the current page."""
        # Get URL via Runtime.evaluate (reliable, no extra params needed)
        url_result = await bridge.send("Runtime.evaluate", {
            "expression": "window.location.href"
        })
        url = url_result.get("result", {}).get("value", "")

        # Get title the same way (avoids Target.getTargetInfo needing targetId)
        title_result = await bridge.send("Runtime.evaluate", {
            "expression": "document.title"
        })
        title = title_result.get("result", {}).get("value", "")

        # Get full accessibility tree (no extra params — fetchRelativeNodes doesn't exist)
        ax_result = await bridge.send("Accessibility.getFullAXTree")
        nodes: list[dict] = ax_result.get("nodes", [])

        elements = self._extract_interactable(nodes)
        role_seq = _build_role_sequence(elements)
        struct_hash = _structural_hash(role_seq)

        return DOMSnapshot(
            url=url,
            title=title,
            elements=elements,
            structural_hash=struct_hash,
            role_sequence=role_seq,
        )

    def _extract_interactable(self, nodes: list[dict]) -> list[AXElement]:
        """Filter to interactable nodes and assign stable deterministic IDs."""
        elements: list[AXElement] = []

        for node in nodes:
            role = self._get_value(node.get("role"))
            if role not in INTERACTABLE_ROLES:
                continue

            label = (
                self._get_value(node.get("name"))
                or self._get_value(node.get("description"))
                or ""
            )

            # Skip nodes with no meaningful label
            if not label.strip() and role not in ("textbox", "searchbox"):
                continue

            label_clean = label.strip()[:80]
            parent_id = node.get("parentId", "")

            # BUG 8 FIX: Deterministic ID based on role + label + parent
            # This is stable across snapshots — same element always gets same ID.
            # Using a hash mod to keep IDs in reasonable range.
            id_input = f"{role}:{label_clean}:{parent_id}".encode()
            stable_id = int(hashlib.md5(id_input).hexdigest()[:8], 16) % 100_000

            el = AXElement(
                id=stable_id,
                role=role,
                label=label_clean,
                node_id=node.get("nodeId", ""),
                backend_dom_id=node.get("backendDOMNodeId", 0),
                depth=len(str(parent_id).split(".")) if parent_id else 0,
            )
            elements.append(el)

        # Deduplicate IDs (hash collision fallback)
        seen_ids: set[int] = set()
        for el in elements:
            while el.id in seen_ids:
                el.id = (el.id + 1) % 100_000
            seen_ids.add(el.id)

        return elements

    def _get_value(self, prop: dict | None) -> str:
        """Extract string value from an AX property dict."""
        if prop is None:
            return ""
        if isinstance(prop, dict):
            return str(prop.get("value", ""))
        return str(prop)


# ------------------------------------------------------------------ #
# Structural hasher + REAL similarity                                   #
# ------------------------------------------------------------------ #

def _build_role_sequence(elements: list[AXElement]) -> str:
    """
    Build the canonical role sequence string for similarity comparison.

    What we include (stable across minor UI changes):
      - Role name
      - Label prefix (first 20 chars — ignores dynamic counters like "Inbox (47)")
      - Depth

    What we IGNORE:
      - CSS classes, element IDs, data-* attributes, pixel positions
    """
    parts = [
        f"{el.role}:{el.label[:20]}:{el.depth}"
        for el in elements
    ]
    return "|".join(parts)


def _structural_hash(role_sequence: str) -> str:
    """Hash the role sequence. For exact-match lookup in the DB."""
    content = role_sequence.encode()
    if HAS_MMH3:
        raw = mmh3.hash_bytes(content)
        return raw.hex()
    else:
        return hashlib.sha256(content).hexdigest()


def hash_similarity(seq_a: str, seq_b: str) -> float:
    """
    Real fuzzy similarity between two DOM role sequences.

    Uses normalized Levenshtein distance on the role sequence strings,
    NOT hex character comparison (which was broken — BUG 2 fix).

    Returns 0.0 (completely different) to 1.0 (identical).
    """
    if seq_a == seq_b:
        return 1.0
    if not seq_a or not seq_b:
        return 0.0

    # Split into tokens for faster comparison
    tokens_a = seq_a.split("|")
    tokens_b = seq_b.split("|")

    # Use token-level Levenshtein (each token = one element)
    dist = _levenshtein_distance(tokens_a, tokens_b)
    max_len = max(len(tokens_a), len(tokens_b))
    if max_len == 0:
        return 1.0

    return 1.0 - (dist / max_len)


def _levenshtein_distance(s: list, t: list) -> int:
    """
    Classic Levenshtein on two lists of tokens.
    O(n*m) time, O(min(n,m)) space.
    """
    if len(s) < len(t):
        return _levenshtein_distance(t, s)

    # Previous and current row of distances
    prev = list(range(len(t) + 1))
    curr = [0] * (len(t) + 1)

    for i, s_tok in enumerate(s, 1):
        curr[0] = i
        for j, t_tok in enumerate(t, 1):
            cost = 0 if s_tok == t_tok else 1
            curr[j] = min(
                curr[j - 1] + 1,      # insertion
                prev[j] + 1,          # deletion
                prev[j - 1] + cost,   # substitution
            )
        prev, curr = curr, prev

    return prev[len(t)]
