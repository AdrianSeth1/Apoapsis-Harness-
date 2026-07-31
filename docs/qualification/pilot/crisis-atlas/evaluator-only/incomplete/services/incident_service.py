"""Incident service layer over PersistenceService."""
from __future__ import annotations

from typing import Optional

from domain.models import (
    ActionItem,
    EventType,
    Incident,
    Severity,
    Status,
    TimelineEvent,
)
from persistence.storage import PersistenceService


class IncidentNotFoundError(Exception):
    """Raised when an incident is not found."""
    pass


class IncidentService:
    """Business logic for managing incidents."""

    def __init__(self, persistence: PersistenceService) -> None:
        self._persistence = persistence

    def _load_data(self) -> dict:
        """Load current data from persistence."""
        data = self._persistence.load()
        if not data:
            return {"incidents": [], "version": 1}
        return data

    def _save_data(self, data: dict) -> None:
        """Save data to persistence."""
        self._persistence.save(data)

    def create_incident(
        self,
        title: str,
        severity: Severity = Severity.LOW,
        owner: Optional[str] = None,
    ) -> Incident:
        """Create a new incident and persist it."""
        incident = Incident(title=title, severity=severity, owner=owner)

        # Add a created event
        incident.add_event(
            TimelineEvent(event_type=EventType.CREATED, detail="Incident created")
        )

        data = self._load_data()
        data["incidents"].append(incident.to_dict())
        data["version"] = data.get("version", 0) + 1
        self._save_data(data)

        return incident

    def get_incident(self, incident_id: str) -> Incident:
        """Retrieve an incident by ID."""
        data = self._load_data()
        for inc_data in data["incidents"]:
            if inc_data["incident_id"] == incident_id:
                return Incident.from_dict(inc_data)
        raise IncidentNotFoundError(f"Incident {incident_id} not found")

    def add_timeline_event(
        self,
        incident_id: str,
        event_type: EventType,
        detail: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> Incident:
        """Add a timeline event to an incident."""
        incident = self.get_incident(incident_id)
        event = TimelineEvent(event_type=event_type, detail=detail, actor=actor)
        incident.add_event(event)

        data = self._load_data()
        for i, inc_data in enumerate(data["incidents"]):
            if inc_data["incident_id"] == incident_id:
                data["incidents"][i] = incident.to_dict()
                break
        data["version"] = data.get("version", 0) + 1
        self._save_data(data)

        return incident

    def add_action_item(
        self,
        incident_id: str,
        description: str,
        assignee: Optional[str] = None,
        due_date: Optional[str] = None,
    ) -> Incident:
        """Add an action item to an incident."""
        incident = self.get_incident(incident_id)
        item = ActionItem(description=description, assignee=assignee, due_date=due_date)
        incident.add_action_item(item)

        data = self._load_data()
        for i, inc_data in enumerate(data["incidents"]):
            if inc_data["incident_id"] == incident_id:
                data["incidents"][i] = incident.to_dict()
                break
        data["version"] = data.get("version", 0) + 1
        self._save_data(data)

        return incident

    def update_status(
        self,
        incident_id: str,
        new_status: Status,
        actor: Optional[str] = None,
    ) -> Incident:
        """Update the status of an incident."""
        incident = self.get_incident(incident_id)
        old_status = incident.status
        incident.status = new_status
        incident.updated_at = TimelineEvent().timestamp

        # Add status change event
        detail = f"Status changed from {old_status.value} to {new_status.value}"
        incident.add_event(
            TimelineEvent(
                event_type=EventType.STATUS_CHANGED,
                detail=detail,
                actor=actor,
            )
        )

        data = self._load_data()
        for i, inc_data in enumerate(data["incidents"]):
            if inc_data["incident_id"] == incident_id:
                data["incidents"][i] = incident.to_dict()
                break
        data["version"] = data.get("version", 0) + 1
        self._save_data(data)

        return incident

    def list_incidents(self) -> list[Incident]:
        """List all incidents."""
        data = self._load_data()
        return [Incident.from_dict(inc) for inc in data["incidents"]]
