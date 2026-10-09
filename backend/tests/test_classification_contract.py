"""Mechanical validation of the proposed contract; routes await owner freeze."""

from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator


@pytest.mark.parametrize("cost", [None, "0", "0.003", "1E-8"])
def test_unclassified_projection_accepts_null_method_and_decimal_accounting(cost):
    contract = yaml.safe_load(
        (Path(__file__).parents[2] / "contracts/openapi.yaml").read_text("utf-8")
    )
    schema = {
        **contract["components"]["schemas"]["DocumentClassification"],
        "components": contract["components"],
    }
    Draft202012Validator(schema).validate(
        {
            "status": "unclassified",
            "revision": 0,
            "path": None,
            "method": None,
            "total_cost_usd": cost,
            "review_required": True,
        }
    )
