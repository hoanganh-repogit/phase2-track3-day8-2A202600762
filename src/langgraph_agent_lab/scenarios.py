"""Scenario loading."""

from __future__ import annotations

import json
from pathlib import Path

from .state import Route, Scenario


def load_scenarios(path: str | Path) -> list[Scenario]:
    path_obj = Path(path)
    scenarios: list[Scenario] = []

    if path_obj.suffix == ".json":
        with path_obj.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        for item in data:
            # Determine expected route based on the question text
            expected_route = Route.SIMPLE
            requires_approval = False
            tags = item.get("grading_criteria", [])
            
            # Map some to other routes to demonstrate rich flow visualizer
            # capabilities if matching keywords
            question_lower = item["question"].lower()
            if "hoàn tiền" in question_lower or "phê duyệt" in question_lower:
                expected_route = Route.RISKY
                requires_approval = True
                tags.append("hitl")

            scenarios.append(Scenario(
                id=item["id"],
                query=item["question"],
                expected_route=expected_route,
                requires_approval=requires_approval,
                should_retry=False,
                max_attempts=3,
                tags=tags
            ))
    else:
        with path_obj.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    scenarios.append(Scenario.model_validate_json(line))
                except Exception as exc:
                    raise ValueError(f"Invalid scenario at line {line_no}: {exc}") from exc

    if len(scenarios) < 6:
        raise ValueError("At least 6 scenarios are required for grading")
    return scenarios
