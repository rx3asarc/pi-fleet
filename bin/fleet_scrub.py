#!/usr/bin/env python3
"""Shape-based redaction for text that leaves a session and becomes durable.

Written for one caller: the fleet's reviewer, which reads an *untrusted* transcript
and writes its account into /root/handoffs/_reaped/*.md. That file outlives the
session, so anything a secret-shaped string carries into it is permanent.

Design rules, in order of importance:

1. **By shape, never by value.** This module knows no keys. It cannot consult the
   value index (that would mean reading /root/.secrets.env inside a writer, and a
   writer that must be safe cannot depend on having the secrets to be safe).
2. **Parity, not invention.** The pattern list below mirrors
   `~/.pi/agent/extensions/secret-shield/shield.mjs` (`SHAPES`, plus its dotenv
   assignment rule and `MIN_SECRET_LEN = 16`). The parity test for that list lives
   in the shield: `~/.pi/agent/extensions/secret-shield/test_parity.py`. If a new
   format is added there, add it here in the same commit -- a divergent copy is a
   copy that stops paying attention.
3. **Idempotent.** scrub(scrub(x)) == scrub(x). The marker contains no
   secret-shaped characters, so a second pass is a no-op. A digest re-generated
   from an already-scrubbed digest must not grow.

Deliberately NOT redacted (each is a decision, not an oversight):

* **Session ids, UUIDs, git SHAs, paths, byte sizes, prices, Swedish prose.** A
  digest that mangles its own evidence is worthless; the point is to keep the
  account readable while making any credential in it unusable.
* **A bare UUID.** Only when the same line assigns it to a name that says
  key/token/secret/password (the shield's `uuid-credential` rule).
* **Short values.** Anything under 16 characters is left alone, because
  `NAME=localhost` and `PORT=8080` are not secrets and redacting them would train
  people to ignore the markers.
* **The transcript body itself.** This module redacts; it does not stop a reader
  from being steered. That is the brief's job (see `test_fleet_scrub.py`,
  TestBriefInvariant).
"""

from __future__ import annotations

import re

MIN_SECRET_LEN = 16  # mirrors shield.mjs

MARKER = "\u00abREDACTED:{label}\u00bb"

# Mirrors SHAPES in ~/.pi/agent/extensions/secret-shield/shield.mjs (order matters:
# specific prefixes before the generic `sk-`).
SHAPES = [
    ("aws-access", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("aws-temp", re.compile(r"\bASIA[0-9A-Z]{16}\b")),
    ("github-pat", re.compile(r"\bghp_[A-Za-z0-9]{30,}\b")),
    ("github-fine", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b")),
    ("anthropic", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{32,}\b")),
    ("openai-style", re.compile(r"\bsk-[A-Za-z0-9_-]{32,}\b")),
    ("tavily", re.compile(r"\btvly-[A-Za-z0-9_-]{16,}\b")),
    ("browser-use", re.compile(r"\bbu_[A-Za-z0-9_-]{20,}\b")),
    ("groq", re.compile(r"\bgsk_[A-Za-z0-9]{40,}\b")),
    ("huggingface", re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")),
    ("slack", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("telegram-bot", re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("pem", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]

# A UUID is safe to print unless the line assigns it to a credential-ish name.
_UUID_CREDENTIAL = re.compile(
    r"\b[A-Za-z0-9_]*(?:API|SECRET|ACCESS|PRIVATE)?[_-]?"
    r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)[A-Za-z0-9_]*[\"']?\s*[:=]\s*[\"']?"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
    re.IGNORECASE,
)

# NAME=value / NAME: value where the NAME says it is a credential.
_ASSIGNMENT = re.compile(
    r"(?P<head>\b[A-Za-z_][A-Za-z0-9_]*?"
    r"(?<![A-Za-z])(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|WEBHOOK|DSN|PWD)"
    r"[A-Za-z0-9_]*\s*[:=]\s*)"
    r"(?P<quote>[\"']?)(?P<value>[^\s\"'`]{8,})(?P=quote)",
    re.IGNORECASE,
)

# Shell-style `--token=abc…` / `--password abc…` arguments.
_CLI_FLAG = re.compile(
    r"(?P<head>--(?:token|password|passwd|secret|api-key|api_key|key)[= ])"
    r"(?P<value>[^\s\"'`]{8,})",
    re.IGNORECASE,
)

# Names that end in KEY/TOKEN but are identifiers, not credentials. Found on real
# data: a handoff lost its Figma `FILE_KEY` to the assignment rule. Exemptions
# narrow false positives; they never widen what is redacted.
_NOT_SECRET_NAMES = {"FILE_KEY", "FILE_ID", "PUBLIC_KEY", "KEY_ID", "KEY_NAME"}

_PLACEHOLDER = re.compile(
    r"^(?:x{4,}|y{4,}|your[_-]?\w*|<[^>]*>|\.\.\.|placeholder|changeme|redacted"
    r"|none|null|true|false|\d+|\*+)$",
    re.IGNORECASE,
)

# An already-redacted marker must survive a second pass unchanged.
_MARKER_OPEN = "\u00abREDACTED:"
_MARKER_RE = re.compile(r"\u00abREDACTED:[a-z-]+\u00bb")

_VERSIONISH = re.compile(r"^(?:v?\d+(?:\.\d+)+|[\d.]+%?|~?\d+(?:\.\d+)?[kmgt]?b?)$",
                         re.IGNORECASE)


def _is_placeholder(value):
    if _PLACEHOLDER.match(value):
        return True
    if _MARKER_RE.fullmatch(value):
        return True
    if _VERSIONISH.match(value):       # 1.0.1, 40.0%, 12mb, 3.5
        return True
    return False


def scrub(text):
    """Return (clean, n_redacted). Shape-based, idempotent, value-blind."""
    if not text:
        return text, 0
    out = text
    # Assignments first: `OPENAI_KEY=sk-...` should be labelled by its shape,
    # not swallowed by the generic assignment rule.
    out = _ASSIGNMENT.sub(_assign, out)
    out = _CLI_FLAG.sub(_cli, out)
    out = _UUID_CREDENTIAL.sub(
        lambda m: m.group(0).replace(m.group(1), MARKER.format(label="uuid-credential")),
        out)
    for label, rx in SHAPES:
        out = rx.sub(MARKER.format(label=label), out)
    # Count markers rather than substitutions: a rule that decides "this is a
    # placeholder" still matches, and counting those would claim a redaction that
    # did not happen (and break the property that a second pass reports 0).
    return out, out.count(_MARKER_OPEN) - text.count(_MARKER_OPEN)


def _shape_of(head, value):
    """The specific label for a value, so `OPENAI_KEY=sk-…` says openai-style."""
    for label, rx in SHAPES:
        if rx.fullmatch(value):
            return label
    if _UUID_CREDENTIAL.search(head + value):
        return "uuid-credential"
    return "assignment"


def _name_of(head):
    return head.split("=")[0].split(":")[0].strip().strip("\"'").upper()


def _assign(m):
    value = m.group("value")
    if _name_of(m.group("head")) in _NOT_SECRET_NAMES:
        return m.group(0)
    if len(value) < MIN_SECRET_LEN or _is_placeholder(value):
        return m.group(0)
    return m.group("head") + m.group("quote") \
        + MARKER.format(label=_shape_of(m.group("head"), value)) + m.group("quote")


def _cli(m):
    value = m.group("value")
    if _is_placeholder(value) or value.startswith("/") or value.startswith("-"):
        return m.group(0)
    return m.group("head") + MARKER.format(label=_shape_of(m.group("head"), value))


def scrub_or_raise(text, what="text"):
    """Scrub, and refuse to return text that still looks credential-shaped.

    A writer that silently passes a secret through is worse than one that fails,
    because the failure is the only observable. Raises ValueError naming the shape
    (never the value).
    """
    clean, _ = scrub(text)
    for label, rx in SHAPES:
        if rx.search(clean):
            raise ValueError(f"{what}: {label} survived scrubbing")
    return clean


def summary(text):
    """(n_redacted, labels) -- for the digest header, without the values."""
    _, n = scrub(text)
    labels = [label for label, rx in SHAPES if rx.search(text or "")]
    return n, labels
