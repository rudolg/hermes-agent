"""Stop clinic and appraisal text before a Hermes vendor wire.

The markers match the local Sol and Claude doors. The system prompt is not
part of the text those doors scan, and it is not scanned here either.
"""

from __future__ import annotations

import json
import re

MARKERS = (
    "hospital number",
    "nhs number",
    "date of birth",
    "clinic letter",
    "this is an appraisal",
)
# One of these can be an ordinary question. Two together are a letter.
LETTER_CUES = (
    "dictated but not signed",
    "procedure code",
    "on examination",
    "past history",
    "red flag",
    "follow-up appointment",
    "medial branch block",
)
DENIED = (
    "/usr/local/LOCAL_Private_Patients_Local_Only",
    "/usr/local/PatentVault",
    "/Volumes/ClinicalHandover",
    "/Users/spinec/Library/CloudStorage/OneDrive-NHS",
    "/Users/spinec/MDT",
    "/Users/spinec/MDT-today",
    "/Users/spinec/Documents/Heidi",
    "/Users/spinec/scripts/Heidi",
    "/Users/spinec/Reports/spireclinic",
    "/Users/spinec/Reports/mdt",
    "~/MDT",
    "~/MDT-today",
    "~/Documents/Heidi",
    "~/scripts/Heidi",
    "~/Reports/spireclinic",
    "~/Reports/mdt",
    "~/Library/CloudStorage/OneDrive-NHS",
)
CLAUDE_REFUSAL = (
    "Stopped before Claude. This looks like clinic or appraisal material. "
    "Open the Claude app for that."
)
# Any backend that is not Claude and not Codex is still a non-Anthropic vendor: same screen, own wording.
OTHER_REFUSAL = (
    "Stopped before a non-Anthropic vendor. This looks like clinic or appraisal material. "
    "Use Claude for that."
)
CODEX_REFUSAL = (
    "Stopped before Codex. This looks like clinic or appraisal material. "
    "Use Claude for that."
)


def _quote_span(folded: str, start: int, end: int) -> tuple[int, int] | None:
    # Return the single-line double-quoted or backticked span that contains the match.
    line_start = folded.rfind("\n", 0, start) + 1
    line_end = folded.find("\n", end)
    if line_end < 0:
        line_end = len(folded)
    line = folded[line_start:line_end]
    rel_start = start - line_start
    rel_end = end - line_start
    i = 0
    while i < len(line):
        ch = line[i]
        if ch not in {'"', "`"}:
            i += 1
            continue
        j = i + 1
        while j < len(line):
            if ch != "`" and line[j] == "\\":
                j += 2
                continue
            if line[j] == ch:
                if i < rel_start and rel_end <= j:
                    return line_start + i, line_start + j
                i = j + 1
                break
            j += 1
        else:
            return None
    return None


def _has_phrase(folded: str, phrase: str) -> bool:
    # Match the phrase as its own words, skipping a quoted mention in a longer text.
    pattern = r"(?<!\w)" + re.escape(phrase) + r"(?!\w)"
    for match in re.finditer(pattern, folded):
        before = folded[match.start() - 1] if match.start() else ""
        after = folded[match.end()] if match.end() < len(folded) else ""
        if before in {"'", '"', "`"} and after in {"'", '"', "`"}:
            rest = folded[match.end() + 1 :].lstrip()
            if not rest.startswith(":"):
                continue
        else:
            span = _quote_span(folded, match.start(), match.end())
            if span is not None:
                open_at, close_at = span
                inside = close_at - open_at - 1
                rest = folded[close_at + 1 :].lstrip()
                outside = len(folded) - inside
                if not rest.startswith(":") and inside <= 160 and outside > inside:
                    continue
            if _escaped_quote_mention(folded, match.start(), match.end()):
                continue
        return True
    return False


def _is_clinic(folded: str) -> bool:
    if any(_has_phrase(folded, marker) for marker in MARKERS):
        return True
    hits = 0
    for cue in LETTER_CUES:
        if _has_phrase(folded, cue):
            hits += 1
            if hits >= 2:
                return True
    return False


_PATH_END = set("/ \t\r\n\"'`.,;:)]?!}")
_CODE_PROJECT_ROOT_NAMES = (
    "/usr/local/PatentVault/LOCAL_Patents_Never_Cloud/PROJECTS",
    "/usr/local/PatentVault/LOCAL_Patents_Never_Cloud/PROJECTS-worktrees",
)


def _is_code_project_root_name(folded: str, start: int) -> bool:
    # These directory names are ordinary repository references. This exception
    # does not cover descendants, file contents, traversal or access permissions.
    token = re.split(r"[\s\"'`<>{}\[\],;:)?!]", folded[start:], maxsplit=1)[0]
    return any(token in {root.casefold(), root.casefold() + "."}
               for root in _CODE_PROJECT_ROOT_NAMES)


def _escaped_quote_mention(folded: str, start: int, end: int) -> bool:
    # Return true when a short \"...\" span in a longer JSON text only names the folder.
    opener = folded.rfind('\\"', 0, start)
    if opener < 0 or start - (opener + 2) > 160:
        return False
    closer = folded.find('\\"', end)
    if closer < 0:
        return False
    inside = closer - (opener + 2)
    if inside <= 0 or inside > 160 or len(folded) - inside <= inside:
        return False
    if any(ch in folded[opener + 2 : start] for ch in ",{}[]:"):
        return False
    nxt = folded[closer + 2] if closer + 2 < len(folded) else ""
    if nxt not in {",", "}", "]", ":", "", " ", "\n", "\t", ")", "\\", '"', "'", "`"}:
        return False
    return folded[closer + 2 :].lstrip().startswith(":") is False


def _comment_mention(folded: str, start: int, end: int, after: str) -> bool:
    # Return true when a hash comment in a long text names the folder.
    if after == "/":
        return False
    window_at = max(0, start - 12)
    window = folded[window_at:start]
    hash_at = window.rfind("#")
    if hash_at < 0 or window[hash_at + 1 :].strip() != "":
        return False
    prev = folded[window_at + hash_at - 1] if window_at + hash_at else ""
    if prev not in {"", " ", "\t", "\n", "|"}:
        return False
    return len(folded) > 400


def _path_is_only_named(folded: str, start: int, end: int) -> bool:
    # Return true when the folder is named in a quote or a comment, not opened.
    before = folded[start - 1] if start else ""
    after = folded[end] if end < len(folded) else ""
    if before in {"'", '"', "`"} and after in {"'", '"', "`"}:
        rest = folded[end + 1 :].lstrip()
        inside = end - start
        if not rest.startswith(":") and len(folded) - inside > inside:
            return True
    span = _quote_span(folded, start, end)
    if span is not None:
        open_at, close_at = span
        inside = close_at - open_at - 1
        rest = folded[close_at + 1 :].lstrip()
        if not rest.startswith(":") and inside <= 160 and len(folded) - inside > inside:
            return True
    if _escaped_quote_mention(folded, start, end):
        return True
    if after == "/":
        return False
    return _comment_mention(folded, start, end, after)


def _has_denied_path(folded: str) -> bool:
    # Return true when the text opens a denied folder.
    for root in DENIED:
        needle = root.casefold()
        begin = 0
        while True:
            at = folded.find(needle, begin)
            if at < 0:
                break
            stop = at + len(needle)
            nxt = folded[stop] if stop < len(folded) else ""
            if nxt == "" or nxt in _PATH_END:
                if not _is_code_project_root_name(folded, at) and not _path_is_only_named(folded, at, stop):
                    return True
            begin = at + 1
    return False


def stays_with_claude(text: str) -> bool:
    """Routing hint, not a refusal: letter-shaped text or a denied folder keeps the turn on the Claude
    mouth, so a seat swap never carries it to a non-Anthropic backend."""
    if not text:
        return False
    folded = text.casefold()
    return _is_clinic(folded) or _has_denied_path(folded)


def refusal_for(text: str, *, vendor: str) -> str:
    # The Claude door is OPEN (owner, 2026-10-03: "remove clinical block ffs"; "remove this shit from
    # hermes now"). Claude is allowed this material; only the Codex door keeps the screen below,
    # because patient material never goes to a non-Anthropic vendor.
    if vendor == "claude":
        return ""
    if not text:
        return ""
    folded = text.casefold()
    if _is_clinic(folded):
        return CODEX_REFUSAL if vendor == "codex" else OTHER_REFUSAL
    if _has_denied_path(folded):
        return CODEX_REFUSAL if vendor == "codex" else OTHER_REFUSAL
    return ""


def _block_text(block) -> str:
    if isinstance(block, str):
        return block
    if not isinstance(block, dict):
        return ""
    kind = block.get("type")
    if kind in {"text", "input_text"} and isinstance(block.get("text"), str):
        return block["text"]
    if kind == "tool_result":
        return content_text(block.get("content"))
    return ""


def content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_block_text(item) for item in content)
    if isinstance(content, dict):
        return _block_text(content)
    return ""


def anthropic_user_text(body: bytes) -> str:
    """User and tool-result text only. Returns "" when the body is not a message list."""
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError):
        return ""
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(messages, list):
        return ""
    parts = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = content_text(message.get("content"))
        if text:
            parts.append(text)
    return "\n".join(parts)


def anthropic_requests_stream(body: bytes) -> tuple[str, bool]:
    model = "claude-opus-5-5"
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError):
        return model, False
    if not isinstance(payload, dict):
        return model, False
    requested = payload.get("model")
    if isinstance(requested, str) and requested:
        model = requested
    return model, payload.get("stream") is True
