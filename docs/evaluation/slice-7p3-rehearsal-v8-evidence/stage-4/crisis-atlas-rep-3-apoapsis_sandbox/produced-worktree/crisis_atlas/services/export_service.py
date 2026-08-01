"""Serialise incidents to deterministic JSON."""

import json


class ExportService:
    def to_json(self, incidents):
        return json.dumps(
            [item.title for item in incidents], sort_keys=True
        )
