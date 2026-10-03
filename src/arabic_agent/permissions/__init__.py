"""Permission and policy layer.

Nothing the agent does reaches a tool without passing through the policy
engine. Tools *declare* the permissions they need; policies *decide* whether a
principal may exercise them. This indirection is what makes the platform safe
to extend with powerful tools (email, ERP, printing, social) later.
"""

from .approvals import Approval, ApprovalStore, InMemoryApprovalStore
from .base import (
    Permission,
    PolicyDecision,
    Principal,
    Rule,
    default_permission_risk,
)
from .policy import PolicyEngine

__all__ = [
    "Approval",
    "ApprovalStore",
    "InMemoryApprovalStore",
    "Permission",
    "PolicyDecision",
    "PolicyEngine",
    "Principal",
    "Rule",
    "default_permission_risk",
]
