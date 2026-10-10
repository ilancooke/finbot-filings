"""Validate the reviewed initial company seed without constructing AWS clients."""

import json
from pathlib import Path
import re

from botocore.session import get_session
from botocore.validate import validate_parameters

from finbot_ingestion.repositories.dynamodb.companies import DynamoDBCompanyRepository
from finbot_ingestion.repositories.dynamodb.serialization import decode


ROOT = Path(__file__).resolve().parents[2]


def test_initial_seed_matches_selected_universe_and_cannot_overwrite():
    request = json.loads((ROOT / "infra/seed/companies.prod.json").read_text())
    model = get_session().get_service_model("dynamodb").operation_model("TransactWriteItems")
    validate_parameters(request, model.input_shape)
    selected = re.search(r"```text\n(.*?)\n```",
                         (ROOT / "docs/PRODUCTION_UNIVERSE.md").read_text(), re.S).group(1).splitlines()
    actions = request["TransactItems"]
    assert len(actions) == len(selected) == 50
    companies = []
    for action in actions:
        assert set(action) == {"Put"}
        put = action["Put"]
        assert put["TableName"] == "arn:aws:dynamodb:us-east-1:559007813222:table/finbot-prod-companies"
        assert put["ConditionExpression"] == "attribute_not_exists(cik)"
        item = decode(put["Item"])
        assert set(item) == {"repository_schema_version", "cik", "ticker", "name", "enabled", "enabled_marker"}
        company = DynamoDBCompanyRepository._validate(item)
        assert company.enabled is True
        assert re.fullmatch(r"[0-9]{10}", put["Item"]["cik"]["S"])
        companies.append(company)
    assert [company.ticker for company in companies] == selected
    assert len({company.cik for company in companies}) == 50
