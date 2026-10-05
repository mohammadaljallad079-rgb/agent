"""Identifier helpers.

All identifiers are prefixed and URL-safe so they are easy to trace in logs and
audit records without leaking internal ordering.
"""

from __future__ import annotations

import uuid

PREFIX_TENANT = "tnt"
PREFIX_USER = "usr"
PREFIX_AGENT = "agt"
PREFIX_TASK = "tsk"
PREFIX_STEP = "stp"
PREFIX_TOOL_CALL = "tcl"
PREFIX_APPROVAL = "apr"
PREFIX_RUN = "run"
PREFIX_MEMORY = "mem"
PREFIX_MESSAGE = "msg"
PREFIX_JOB = "job"
PREFIX_IDEMPOTENCY = "idm"
PREFIX_REQUEST = "req"
# Workflow engine
PREFIX_WORKFLOW = "wfl"
PREFIX_WORKFLOW_VERSION = "wfv"
PREFIX_WORKFLOW_RUN = "wfr"
PREFIX_STEP_RUN = "spr"
# Triggers
PREFIX_TRIGGER = "trg"
PREFIX_TRIGGER_RUN = "tgr"
PREFIX_WEBHOOK = "whk"
# Local runtime
PREFIX_DEVICE = "dev"
PREFIX_SYNC = "syc"
PREFIX_LOCAL_CAPABILITY = "lcp"


def new_id(prefix: str) -> str:
    """Return a new prefixed, URL-safe identifier."""
    return f"{prefix}_{uuid.uuid4().hex}"
