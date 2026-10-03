"""Integration layer: outbound connectors kept out of the agent core.

An integration is anything the platform talks to beyond its own storage:
email, ERP, WhatsApp Business, Instagram, Facebook, calendar, storage. Each
connector is registered by name and reached through a uniform interface so a new
provider never touches the agent loop.
"""

from .base import (
    Integration,
    IntegrationCall,
    IntegrationRegistry,
    IntegrationResult,
)
from .webhook import WebhookIntegration

__all__ = [
    "Integration",
    "IntegrationCall",
    "IntegrationRegistry",
    "IntegrationResult",
    "WebhookIntegration",
]
