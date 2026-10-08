"""Shared conditional-write and scoped paginator mechanics."""

import base64
import json

from .serialization import decode, encode, integer, MAX_ERROR_CHARS, timestamp
from ..errors import RepositoryBusy, RepositoryConflict, RepositoryDataError, RepositoryNotFound


class DynamoDBRepository:
    def __init__(self, execution, table_name, key_name):
        self.execution, self.table_name, self.key_name = execution, table_name, key_name

    async def _call(self, operation, **kwargs):
        return await self.execution.call(getattr(self.execution.client, operation), TableName=self.table_name, **kwargs)

    async def _get(self, key):
        response = await self._call("get_item", Key=encode({self.key_name: key}), ConsistentRead=True)
        item = response.get("Item")
        return decode(item) if item is not None else None

    async def _create(self, item, validate, compatible):
        validate(item)
        try:
            await self._call("put_item", Item=encode(item),
                             ConditionExpression="attribute_not_exists(#key)",
                             ExpressionAttributeNames={"#key": self.key_name})
            return True
        except self.execution.client.exceptions.ConditionalCheckFailedException:
            existing = await self._get(item[self.key_name])
            if existing is None:
                raise RepositoryBusy("conflicting creation disappeared")
            compatible(validate(existing), validate(item))
            return False

    async def _update(self, key, decide, validate):
        # Each decision is rerun on current durable facts; no blind counter retries.
        for _ in range(self.execution.config.cas_attempts):
            item = await self._get(key)
            if item is None:
                raise RepositoryNotFound(key)
            validate(item)
            changes = decide(item)
            if changes is None:
                return
            sets, removes = changes
            revision = integer(item.get("revision"), "revision")
            new = {**item, **sets, "revision": revision + 1}
            for name in removes:
                new.pop(name, None)
            validate(new)
            encode(new)  # Includes total-record size validation, not only the delta.
            names = {"#key": self.key_name, "#rev": "revision"}
            values = {":old": revision, ":new": revision + 1}
            set_parts = ["#rev = :new"]
            for index, (name, value) in enumerate(sets.items()):
                names[f"#s{index}"] = name
                values[f":s{index}"] = value
                set_parts.append(f"#s{index} = :s{index}")
            remove_parts = []
            for index, name in enumerate(removes):
                names[f"#r{index}"] = name
                remove_parts.append(f"#r{index}")
            expression = "SET " + ", ".join(set_parts)
            if remove_parts:
                expression += " REMOVE " + ", ".join(remove_parts)
            try:
                response = await self._call(
                    "update_item", Key=encode({self.key_name: key}),
                    ConditionExpression="attribute_exists(#key) AND #rev = :old",
                    UpdateExpression=expression, ExpressionAttributeNames=names,
                    ExpressionAttributeValues=encode(values), ReturnValues="ALL_NEW",
                )
                validate(decode(response["Attributes"]))
                return
            except self.execution.client.exceptions.ConditionalCheckFailedException:
                continue
        raise RepositoryBusy("conditional update attempts exhausted")

    async def _failure(self, key, error, at, validate, completed):
        if not isinstance(error, str) or not error:
            raise ValueError("error must be a nonempty string")
        error, at = error[:MAX_ERROR_CHARS], timestamp(at)

        def decide(item):
            if completed(item):
                return None
            old_at = item.get("last_error_at")
            if old_at is not None and old_at >= at:
                if old_at == at and item["last_error"] != error:
                    raise RepositoryConflict("different failures share an attempt timestamp")
                return None
            return ({"retry_count": integer(item.get("retry_count", 0), "retry_count") + 1,
                     "last_error": error, "last_error_at": at}, ())
        await self._update(key, decide, validate)

    async def _query(self, *, index=None, partition, value,
                     page_size=None, token=None, hydrate=True):
        page_size = self.execution.config.page_size if page_size is None else page_size
        if type(page_size) is not int or not 1 <= page_size <= 1000:
            raise ValueError("page_size must be an integer in [1, 1000]")
        scope = [self.execution.config.region, self.table_name, index, partition, value, page_size]
        starting = None
        if token is not None:
            try:
                envelope = json.loads(base64.b64decode(token, validate=True))
                if envelope["scope"] != scope or not isinstance(envelope["sdk_token"], str):
                    raise ValueError("scope mismatch")
                starting = envelope["sdk_token"]
            except (ValueError, TypeError, KeyError, UnicodeError) as exc:
                raise ValueError("invalid or differently scoped continuation token") from exc

        def read():
            paginator = self.execution.client.get_paginator("query")
            params = dict(TableName=self.table_name,
                          KeyConditionExpression="#pk = :pk",
                          ExpressionAttributeNames={"#pk": partition},
                          ExpressionAttributeValues=encode({":pk": value}),
                          PaginationConfig={"PageSize": page_size, "MaxItems": page_size})
            if index is not None:
                params["IndexName"] = index
            else:
                params["ConsistentRead"] = True
            if starting is not None:
                params["PaginationConfig"]["StartingToken"] = starting
            pages = paginator.paginate(**params)
            items = [decode(item) for page in pages for item in page.get("Items", [])]
            continuation = pages.resume_token
            return items, continuation

        items, continuation = await self.execution.call(read)
        if hydrate:
            durable = []
            for item in items:
                if self.key_name not in item:
                    raise RepositoryDataError("index entry missing primary key")
                current = await self._get(item[self.key_name])
                if current is not None:
                    durable.append(current)
            items = durable
        next_token = None
        if continuation is not None:
            next_token = base64.b64encode(json.dumps({"scope": scope, "sdk_token": continuation}).encode()).decode()
        return items, next_token
