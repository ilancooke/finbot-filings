"""Shared stateful AWS boundaries for offline integration and container tests."""

from collections import deque
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import threading

from botocore.exceptions import ClientError, ReadTimeoutError
from finbot_ingestion.repositories.dynamodb.serialization import decode, encode

NOW = datetime(2026, 10, 8, 20, tzinfo=timezone.utc)


class MemoryAWS:
    """Small DynamoDB API fake with condition evaluation and atomic writes.

    It uses fixed table/index contracts, real SDK AttributeValues, and real SDK
    paginators; scripted responses model effects an in-memory store cannot.
    """
    keys = {"companies": ("cik",), "calendar": ("expected_date", "cik"),
            "filings": ("accession_number",), "artifacts": ("artifact_id",)}
    indexes = {"EnabledCompanies": ("enabled_marker", "cik"),
               "PendingFilingEnumeration": ("pending_work_kind", "pending_work_sort"),
               "PendingArtifactWork": ("pending_work_kind", "pending_work_sort"),
               "ArtifactsByAccession": ("accession_number", "filename")}

    def __init__(self, client):
        self.client, self.tables, self.calls = client, {name: {} for name in self.keys}, []
        self.lock = threading.RLock()
        self.lost = deque()
        self.query_responses = deque()
        self.index_snapshots = {}
        self.fail_put = None
        self.before_update = None
        self.query_cap = 1000

    def key(self, table, item):
        return tuple(item[name] for name in self.keys[table])

    def condition(self, expression, item, names, values):
        if " OR " in expression:
            return any(self.condition(part, item, names, values) for part in expression.split(" OR "))
        if " AND " in expression:
            return all(self.condition(part, item, names, values) for part in expression.split(" AND "))
        if expression.startswith("attribute_not_exists("):
            return names[expression[21:-1]] not in item
        if expression.startswith("attribute_exists("):
            return names[expression[17:-1]] in item
        field, op, value = expression.split()
        actual, expected = item.get(names[field]), values[value]
        return actual is not None and ({"=": lambda: actual == expected,
                                      "<": lambda: actual < expected}[op]())

    def api(self, operation, params):
        with self.lock:
            self.calls.append((operation, deepcopy(params)))
            table = params["TableName"]
            rows = self.tables[table]
            if operation == "GetItem":
                assert params["ConsistentRead"] is True
                item = rows.get(self.key(table, decode(params["Key"])))
                return {} if item is None else {"Item": encode(deepcopy(item))}
            if operation in ("PutItem", "UpdateItem"):
                proposed = decode(params.get("Item", params.get("Key")))
                key = self.key(table, proposed)
                item = rows.get(key, {})
                if operation == "PutItem" and self.fail_put is not None and self.fail_put == proposed.get("artifact_id"):
                    raise self.client.exceptions.InternalServerError({"Error": {
                        "Code": "InternalServerError", "Message": "injected"}}, operation)
                if operation == "UpdateItem" and self.before_update is not None:
                    hook, self.before_update = self.before_update, None
                    hook(item)
                names = params.get("ExpressionAttributeNames", {})
                values = decode(params.get("ExpressionAttributeValues", {}))
                if "ConditionExpression" in params and not self.condition(params["ConditionExpression"], item, names, values):
                    raise self.client.exceptions.ConditionalCheckFailedException({"Error": {
                        "Code": "ConditionalCheckFailedException", "Message": "injected"}}, operation)
                if operation == "PutItem":
                    rows[key] = deepcopy(proposed)
                    response = {}
                else:
                    updated = {**proposed, **item}
                    parts = params["UpdateExpression"][4:].split(" REMOVE ")
                    for assignment in parts[0].split(", "):
                        field, value = assignment.split(" = ")
                        updated[names[field]] = values[value]
                    if len(parts) > 1:
                        for field in parts[1].split(", "):
                            updated.pop(names[field], None)
                    rows[key] = updated
                    response = {"Attributes": encode(deepcopy(updated))}
                if self.lost and self.lost[0] == operation:
                    self.lost.popleft()
                    raise ReadTimeoutError(endpoint_url="https://mock.invalid")
                return response
            if operation == "Query":
                if self.query_responses:
                    return self.query_responses.popleft()
                index = params.get("IndexName")
                if index:
                    assert "ConsistentRead" not in params
                    partition, sort = self.indexes[index]
                else:
                    assert params["ConsistentRead"] is True
                    partition, sort = self.keys[table]
                value = decode(params["ExpressionAttributeValues"])[":pk"]
                candidates = self.index_snapshots.get(index, rows.values())
                candidates = [row for row in candidates if row.get(partition) == value and sort in row]
                order = lambda row: (row[sort], self.key(table, row))
                candidates.sort(key=order)
                if "ExclusiveStartKey" in params:
                    start = decode(params["ExclusiveStartKey"])
                    candidates = [row for row in candidates if order(row) > order(start)]
                limit = min(params["Limit"], self.query_cap)
                selected = candidates[:limit]
                key_names = set(self.keys[table]) | {partition, sort}
                output = [{name: row[name] for name in key_names} if index else row for row in selected]
                response = {"Items": [encode(deepcopy(row)) for row in output], "Count": len(output)}
                if len(candidates) > limit:
                    response["LastEvaluatedKey"] = encode({name: selected[-1][name] for name in key_names})
                return response
            raise AssertionError(f"unexpected AWS operation: {operation}")


def aws_error(code, status, operation):
    return ClientError({"Error": {"Code": code, "Message": "injected"},
                        "ResponseMetadata": {"HTTPStatusCode": status}}, operation)


class MemoryS3:
    def __init__(self):
        self.objects, self.calls = {}, []
        self.failures, self.lost = deque(), deque()
        self.clock = NOW + timedelta(seconds=1)

    def api(self, operation, params):
        self.calls.append((operation, deepcopy(params)))
        if self.failures and self.failures[0][0] == operation:
            _, error = self.failures.popleft()
            raise error
        key = (params["Bucket"], params["Key"])
        if operation == "HeadObject":
            if key not in self.objects:
                raise aws_error("404", 404, operation)
            return deepcopy(self.objects[key][1])
        assert operation == "PutObject"
        assert params["IfNoneMatch"] == "*"
        if key in self.objects:
            raise aws_error("PreconditionFailed", 412, operation)
        head = {"Metadata": deepcopy(params["Metadata"]), "ContentLength": len(params["Body"]),
                "LastModified": self.clock, "ContentType": params.get("ContentType", "application/octet-stream")}
        self.objects[key] = (params["Body"], head)
        if self.lost and self.lost[0] == operation:
            self.lost.popleft()
            raise ReadTimeoutError(endpoint_url="https://mock.invalid")
        return {"ETag": '"opaque"'}


class MemoryMessages:
    def __init__(self):
        self.events, self.failures, self.lost = [], deque(), deque()

    def api(self, operation, params):
        if self.failures and self.failures[0][0] == operation:
            _, error = self.failures.popleft()
            raise error
        self.events.append((operation, deepcopy(params)))
        if self.lost and self.lost[0] == operation:
            self.lost.popleft()
            raise ReadTimeoutError(endpoint_url="https://mock.invalid")
        return {"MessageId": f"message-{len(self.events)}"}


