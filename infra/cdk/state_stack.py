"""Retained acquisition state; keys/indexes are the LLD adapter contract."""
from aws_cdk import Stack, RemovalPolicy, Duration, CfnOutput
from aws_cdk import aws_s3 as s3, aws_dynamodb as db, aws_sns as sns, aws_sqs as sqs
from aws_cdk import aws_iam as iam, aws_ecr as ecr


class StateStack(Stack):
    def __init__(self, scope, construct_id, *, config, **kwargs):
        super().__init__(scope, construct_id, termination_protection=True, **kwargs)
        self.node.set_context(f"availability-zones:account={config.account}:region={config.region}",
            [config.region + "a", config.region + "b"])
        self.bucket = s3.Bucket(self, "Artifacts", versioned=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True, object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            removal_policy=RemovalPolicy.RETAIN, auto_delete_objects=False)
        for sid, condition in (
            ("RequireCreateOnlyHeader", {"Null": {"s3:if-none-match": "true"}}),
            ("RequireCreateOnlyValue", {"StringNotEquals": {"s3:if-none-match": "*"}}),
        ):
            self.bucket.add_to_resource_policy(iam.PolicyStatement(sid=sid,
                effect=iam.Effect.DENY, principals=[iam.AnyPrincipal()],
                actions=["s3:PutObject"], resources=[self.bucket.arn_for_objects("*")],
                conditions=condition))
        self.tables = {}
        definitions = {
            "companies": ("cik", None, [("EnabledCompanies", "enabled_marker", "cik")]),
            "calendar": ("expected_date", "cik", []),
            "filings": ("accession_number", None, [("PendingFilingEnumeration", "pending_work_kind", "pending_work_sort")]),
            "artifacts": ("artifact_id", None, [("PendingArtifactWork", "pending_work_kind", "pending_work_sort"),
                ("ArtifactsByAccession", "accession_number", "filename")]),
        }
        for name, (pk, sk, indexes) in definitions.items():
            table = db.Table(self, name.capitalize() + "TableResource", table_name=f"{config.name}-{name}",
                partition_key=db.Attribute(name=pk, type=db.AttributeType.STRING),
                sort_key=db.Attribute(name=sk, type=db.AttributeType.STRING) if sk else None,
                billing_mode=db.BillingMode.PAY_PER_REQUEST,
                encryption=db.TableEncryption.DEFAULT, deletion_protection=True,
                point_in_time_recovery_specification=db.PointInTimeRecoverySpecification(
                    point_in_time_recovery_enabled=True), removal_policy=RemovalPolicy.RETAIN)
            for index, ipk, isk in indexes:
                table.add_global_secondary_index(index_name=index,
                    partition_key=db.Attribute(name=ipk, type=db.AttributeType.STRING),
                    sort_key=db.Attribute(name=isk, type=db.AttributeType.STRING),
                    projection_type=db.ProjectionType.KEYS_ONLY)
            self.tables[name] = table
            CfnOutput(self, name.capitalize() + "Table", value=table.table_name)
        # ADR 010 accepts unencrypted SNS message bodies at rest; HTTPS is required.
        self.topic = sns.Topic(self, "ArtifactReady", topic_name=f"{config.name}-artifact-ready")
        self.topic.apply_removal_policy(RemovalPolicy.RETAIN)
        self.topic.add_to_resource_policy(iam.PolicyStatement(effect=iam.Effect.DENY,
            principals=[iam.AnyPrincipal()], actions=["sns:Publish"], resources=[self.topic.topic_arn],
            conditions={"Bool": {"aws:SecureTransport": "false"}}))
        self.failed_work = sqs.Queue(self, "FailedWork", queue_name=f"{config.name}-failed-work",
            encryption=sqs.QueueEncryption.SQS_MANAGED, enforce_ssl=True,
            retention_period=Duration.days(14), removal_policy=RemovalPolicy.RETAIN)
        self.repository = ecr.Repository(self, "Images", repository_name=f"{config.name}-ingestion",
            image_tag_mutability=ecr.TagMutability.IMMUTABLE, image_scan_on_push=True,
            removal_policy=RemovalPolicy.RETAIN, empty_on_delete=False)
        for name, value in {
            "ArtifactBucket": self.bucket.bucket_name, "ArtifactTopicArn": self.topic.topic_arn,
            "FailedWorkQueueUrl": self.failed_work.queue_url,
            "ImageRepositoryUri": self.repository.repository_uri,
        }.items():
            CfnOutput(self, name, value=value)
