"""Multi-tenancy model.

Tenant isolation is enforced at three layers: the API authenticates a
:class:`Principal`; every store takes ``tenant_id`` on read and write; and
records carry their ``tenant_id`` so a store bug cannot silently cross tenants.
"""

from .models import AgentProfile, Role, Tenant, User

__all__ = ["AgentProfile", "Role", "Tenant", "User"]
