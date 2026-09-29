"""Stop clinic and appraisal text before a Hermes vendor wire.

The markers match the local Sol and Claude doors. The system prompt is not
part of the text those doors scan, and it is not scanned here either.
"""

from __future__ import annotations

import json

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
CODEX_REFUSAL = (
    "Stopped before Codex. This looks like clinic or appraisal material. "
    "Use Claude for that."
)


def _is_clinic(folded: str) -> bool:
    if any(marker in folded for marker in MARKERS):
        return True
    hits = 0
    for cue in LETTER_CUES:
        if cue in folded:
            hits += 1
            if hits >= 2:
                return True
    return False


def refusal_for(text: str, *, vendor: str) -> str:
    if not text:
        return ""
    folded = text.casefold()
    if _is_clinic(folded):
        return CODEX_REFUSAL if vendor == "codex" else CLAUDE_REFUSAL
    for root in DENIED:
        if root.casefold() in folded:
            return CODEX_REFUSAL if vendor == "codex" else CLAUDE_REFUSAL
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
