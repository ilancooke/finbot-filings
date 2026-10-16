"""Insert missing companies from the consolidated JSON; preserve existing rows."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import re

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from finbot_ingestion.repositories.dynamodb.companies import DynamoDBCompanyRepository
from finbot_ingestion.repositories.dynamodb.serialization import decode, record
from finbot_ingestion.repositories.errors import RepositoryDataError


DEFAULT_FILE = Path(__file__).resolve().parents[1] / "infra/seed/companies.prod.json"
TABLE_ARN = re.compile(r"arn:(aws|aws-us-gov|aws-cn):dynamodb:([a-z0-9-]+):([0-9]{12}):table/([A-Za-z0-9_.-]{3,255})")
FIELDS = {"repository_schema_version", "cik", "ticker", "name", "enabled", "enabled_marker"}


def load_input(path):
    """Validate the entire input before opening any AWS clients or writing rows."""
    request = json.loads(Path(path).read_text())
    if not isinstance(request, dict) or not isinstance(request.get("TransactItems"), list) or not request["TransactItems"]:
        raise ValueError("company input must contain a nonempty TransactItems list")
    table_arn, items, tickers, duplicates = None, {}, {}, 0
    for action in request["TransactItems"]:
        if not isinstance(action, dict) or set(action) != {"Put"} or not isinstance(action["Put"], dict):
            raise ValueError("company input must contain only Put records")
        put = action["Put"]
        if (set(put) != {"TableName", "Item", "ConditionExpression"}
                or put["ConditionExpression"] != "attribute_not_exists(cik)"
                or not isinstance(put["Item"], dict)
                or not isinstance(put["TableName"], str)
                or not TABLE_ARN.fullmatch(put["TableName"])):
            raise ValueError("expected create-only company records with a table ARN")
        if table_arn is not None and table_arn != put["TableName"]:
            raise ValueError("all company records must target the same table")
        table_arn = put["TableName"]
        item = decode(put["Item"])
        if set(item) != FIELDS:
            raise ValueError("unexpected company fields")
        company = DynamoDBCompanyRepository._validate(item)
        if (not company.enabled or item != {**record(company), "enabled_marker": "ENABLED"}
                or not re.fullmatch(r"[A-Z0-9][A-Z0-9.-]*", company.ticker)):
            raise ValueError("company must use canonical identity and be enabled")
        if company.cik in items:
            if items[company.cik] != item:
                raise ValueError("conflicting duplicate CIK in company input")
            duplicates += 1
            continue
        if company.ticker in tickers:
            raise ValueError("ticker maps to multiple CIKs in company input")
        items[company.cik], tickers[company.ticker] = item, company.cik
    match = TABLE_ARN.fullmatch(table_arn)
    return {"table": table_arn, "region": match[2], "account": match[3],
            "items": tuple(items.values()), "duplicates_in_file": duplicates}


def skip_existing(existing, desired, report):
    company = DynamoDBCompanyRepository._validate(existing)
    if company.cik != desired["cik"]:
        raise ValueError("existing company key disagrees with requested CIK")
    report["skipped"] += 1
    differences = sorted(key for key in FIELDS if existing.get(key) != desired[key])
    if differences:
        report["existing_differences"].append({"cik": desired["cik"],
            "ticker": desired["ticker"], "fields": differences})


def load_missing(table, items, report, *, dry_run=False):
    """Read existing keys strongly; conditional creates also protect racing writers."""
    for item in items:
        key = {"cik": item["cik"]}
        existing = table.get_item(Key=key, ConsistentRead=True).get("Item")
        if existing is not None:
            skip_existing(existing, item, report)
            continue
        if dry_run:
            report["would_insert"] += 1
            continue
        try:
            table.put_item(Item=item, ConditionExpression=Attr("cik").not_exists())
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            # Another loader or an acknowledged-lost retry may have created it.
            existing = table.get_item(Key=key, ConsistentRead=True).get("Item")
            if existing is None:
                raise
            skip_existing(existing, item, report)
        else:
            report["inserted"] += 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="Load new company records; skip existing CIKs without changing them")
    parser.add_argument("--companies-file", type=Path, default=DEFAULT_FILE,
                        help="consolidated company JSON (default: infra/seed/companies.prod.json)")
    parser.add_argument("--profile", help="AWS profile; otherwise use the normal credential chain")
    parser.add_argument("--dry-run", action="store_true", help="read AWS and report missing companies without writing")
    args = parser.parse_args(argv)
    report = {"status": "failed", "dry_run": args.dry_run, "inserted": 0,
              "skipped": 0, "would_insert": 0, "existing_differences": []}
    try:
        plan = load_input(args.companies_file)
        report.update(table=plan["table"], requested=len(plan["items"]), duplicates_in_file=plan["duplicates_in_file"])
        config = Config(connect_timeout=5, read_timeout=10,
                        retries={"total_max_attempts": 3, "mode": "standard"})
        session = boto3.Session(profile_name=args.profile, region_name=plan["region"])
        with closing(session.client("sts", config=config)) as sts:
            if sts.get_caller_identity()["Account"] != plan["account"]:
                raise ValueError("AWS caller account does not match the input table ARN")
        resource = session.resource("dynamodb", config=config)
        with closing(resource.meta.client):
            load_missing(resource.Table(plan["table"]), plan["items"], report, dry_run=args.dry_run)
        report["status"] = "complete"
    except (OSError, ValueError, TypeError, KeyError, RepositoryDataError, BotoCoreError, ClientError) as exc:
        # SDK/provider messages may include sensitive details; retain type/code only.
        report["error_type"] = type(exc).__name__
        if isinstance(exc, ClientError):
            report["error_code"] = exc.response["Error"]["Code"]
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
