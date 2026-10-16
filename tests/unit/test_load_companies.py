"""The operator loader inserts missing CIKs and safely resumes mixed inputs."""

from contextlib import closing
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import boto3
from botocore.stub import ANY, Stubber
import pytest

from finbot_ingestion.repositories.dynamodb.serialization import encode


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("load_companies", ROOT / "scripts/load_companies.py")
loader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loader)
TABLE = "arn:aws:dynamodb:us-east-1:559007813222:table/finbot-prod-companies"
COMPANY = {"repository_schema_version": 1, "cik": "0000000001", "ticker": "TEST",
           "name": "Test Company", "enabled": True, "enabled_marker": "ENABLED"}
SECOND = {**COMPANY, "cik": "0000000002", "ticker": "OTHER"}


def report():
    return {"inserted": 0, "skipped": 0, "would_insert": 0, "existing_differences": []}


def input_file(tmp_path, items):
    path = tmp_path / "companies.json"
    path.write_text(json.dumps({"TransactItems": [{"Put": {"TableName": TABLE,
        "Item": encode(item), "ConditionExpression": "attribute_not_exists(cik)"}} for item in items]}))
    return path


class Table:
    def __init__(self, rows=()):
        self.rows = {row["cik"]: deepcopy(row) for row in rows}
        self.writes = []
        self.meta = SimpleNamespace(client=SimpleNamespace(exceptions=SimpleNamespace(
            ConditionalCheckFailedException=ConditionalFailure)))

    def get_item(self, *, Key, ConsistentRead):
        assert ConsistentRead is True
        row = self.rows.get(Key["cik"])
        return {"Item": deepcopy(row)} if row else {}

    def put_item(self, *, Item, ConditionExpression):
        expression = ConditionExpression.get_expression()
        assert expression["operator"] == "attribute_not_exists"
        assert expression["values"][0].name == "cik"
        if Item["cik"] in self.rows:
            raise ConditionalFailure()
        self.rows[Item["cik"]] = deepcopy(Item)
        self.writes.append(deepcopy(Item))


class ConditionalFailure(Exception):
    pass


def test_production_input_is_valid_and_identical_duplicates_are_collapsed(tmp_path):
    assert len(loader.load_input(loader.DEFAULT_FILE)["items"]) == 72
    plan = loader.load_input(input_file(tmp_path, [COMPANY, COMPANY, SECOND]))
    assert plan["items"] == (COMPANY, SECOND)
    assert plan["duplicates_in_file"] == 1
    assert (plan["table"], plan["account"], plan["region"]) == (TABLE, "559007813222", "us-east-1")


@pytest.mark.parametrize("bad", [
    {**COMPANY, "name": "Conflicting name"},
    {**SECOND, "ticker": "TEST"},
    {**SECOND, "cik": "2"},
    {**SECOND, "enabled": False},
    {**SECOND, "ticker": "../unsafe"},
    {**SECOND, "extra": "not a company field"},
])
def test_invalid_later_record_fails_before_aws_clients_or_any_writes(tmp_path, monkeypatch, capsys, bad):
    path = input_file(tmp_path, [COMPANY, bad])
    monkeypatch.setattr(loader.boto3, "Session", lambda **kw: pytest.fail("invalid input must fail before AWS"))
    assert loader.main(["--companies-file", str(path)]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["inserted"] == output["skipped"] == 0


@pytest.mark.parametrize("change", [
    {"TableName": "companies"},
    {"TableName": TABLE.replace("finbot-prod-companies", "another-table")},
    {"ConditionExpression": "attribute_exists(cik)"},
])
def test_mixed_targets_or_unprotected_requests_are_rejected(tmp_path, change):
    path = input_file(tmp_path, [COMPANY, SECOND])
    request = json.loads(path.read_text())
    request["TransactItems"][1]["Put"].update(change)
    path.write_text(json.dumps(request))
    with pytest.raises(ValueError):
        loader.load_input(path)


def test_repeated_mixed_load_preserves_existing_disabled_records_and_extra_fields():
    existing = {**COMPANY, "enabled": False, "name": "Operator edited name", "custom": "keep"}
    existing.pop("enabled_marker")
    table = Table([existing])
    first = report()
    loader.load_missing(table, (COMPANY, SECOND), first)
    assert (first["inserted"], first["skipped"]) == (1, 1)
    assert first["existing_differences"][0]["fields"] == ["enabled", "enabled_marker", "name"]
    assert table.rows[COMPANY["cik"]] == existing
    second = report()
    loader.load_missing(table, (COMPANY, SECOND), second)
    assert (second["inserted"], second["skipped"]) == (0, 2)
    assert table.writes == [SECOND]


def test_concurrent_creation_is_skipped_instead_of_overwritten():
    class RacingTable(Table):
        def put_item(self, *, Item, ConditionExpression):
            self.rows[Item["cik"]] = {**Item, "name": "Concurrent writer"}
            return super().put_item(Item=Item, ConditionExpression=ConditionExpression)

    table, result = RacingTable(), report()
    loader.load_missing(table, (COMPANY,), result)
    assert (result["inserted"], result["skipped"]) == (0, 1)
    assert table.rows[COMPANY["cik"]]["name"] == "Concurrent writer"
    assert not table.writes


def test_dry_run_reads_existing_rows_and_does_not_write():
    table, result = Table([COMPANY]), report()
    loader.load_missing(table, (COMPANY, SECOND), result, dry_run=True)
    assert (result["would_insert"], result["skipped"], result["inserted"]) == (1, 1, 0)
    assert not table.writes and SECOND["cik"] not in table.rows


def test_sdk_errors_are_not_treated_as_existing_and_partial_load_can_resume():
    class InterruptedTable(Table):
        def put_item(self, *, Item, ConditionExpression):
            if Item == SECOND:
                raise RuntimeError("service unavailable")
            return super().put_item(Item=Item, ConditionExpression=ConditionExpression)

    table, first = InterruptedTable(), report()
    with pytest.raises(RuntimeError):
        loader.load_missing(table, (COMPANY, SECOND), first)
    assert first["inserted"] == 1
    resumed = Table(table.rows.values())
    second = report()
    loader.load_missing(resumed, (COMPANY, SECOND), second)
    assert (second["inserted"], second["skipped"]) == (1, 1)
    assert resumed.writes == [SECOND]


def test_real_sdk_accepts_resource_requests_and_expected_conditional_race():
    resource = boto3.resource("dynamodb", region_name="us-east-1")
    with closing(resource.meta.client), Stubber(resource.meta.client) as stub:
        get = {"TableName": TABLE, "Key": {"cik": COMPANY["cik"]}, "ConsistentRead": True}
        stub.add_response("get_item", {}, get)
        stub.add_client_error("put_item", "ConditionalCheckFailedException", expected_params={
            "TableName": TABLE, "Item": COMPANY, "ConditionExpression": ANY})
        stub.add_response("get_item", {"Item": encode(COMPANY)}, get)
        result = report()
        loader.load_missing(resource.Table(TABLE), (COMPANY,), result)
        assert (result["inserted"], result["skipped"]) == (0, 1)
        stub.assert_no_pending_responses()


def test_wrong_aws_account_is_rejected_before_dynamodb(tmp_path, monkeypatch, capsys):
    class Session:
        def client(self, name, **kwargs):
            assert name == "sts"
            return SimpleNamespace(close=lambda: None, get_caller_identity=lambda: {"Account": "000000000000"})

        def resource(self, *args, **kwargs):
            pytest.fail("wrong account must not reach DynamoDB")

    monkeypatch.setattr(loader.boto3, "Session", lambda **kwargs: Session())
    assert loader.main(["--companies-file", str(input_file(tmp_path, [COMPANY]))]) == 1
    assert json.loads(capsys.readouterr().out)["error_type"] == "ValueError"


def test_help_is_offline(monkeypatch):
    monkeypatch.setattr(loader.boto3, "Session", lambda **kw: pytest.fail("help must be offline"))
    with pytest.raises(SystemExit) as exc:
        loader.main(["--help"])
    assert exc.value.code == 0
