import unittest

from crisis_atlas.services.export_service import ExportService
from crisis_atlas.services.incident_service import IncidentService


class ServiceTests(unittest.TestCase):
    def test_create_returns_the_incident(self) -> None:
        self.assertEqual(IncidentService().create('x').title, 'x')

    def test_export_is_deterministic(self) -> None:
        incidents = [IncidentService().create('b')]
        service = ExportService()
        self.assertEqual(
            service.to_json(incidents), service.to_json(incidents)
        )
