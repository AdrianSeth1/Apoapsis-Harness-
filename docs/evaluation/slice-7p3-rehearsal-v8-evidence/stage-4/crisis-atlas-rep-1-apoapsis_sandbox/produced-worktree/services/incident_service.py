"""Business operations over the persisted Crisis Atlas incidents."""
from __future__ import annotations


class IncidentService:
    def create_incident(self, title):
        return {'title': title}
