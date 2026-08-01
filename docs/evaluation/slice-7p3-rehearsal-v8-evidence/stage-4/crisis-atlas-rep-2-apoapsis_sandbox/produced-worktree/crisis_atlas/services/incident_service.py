"""Create and return incidents."""


class Incident:
    def __init__(self, title):
        self.title = title


class IncidentService:
    def create(self, title):
        return Incident(title)
