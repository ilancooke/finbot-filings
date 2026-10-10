"""Stopped-by-default Fargate runtime, release federation and health alarms."""
from aws_cdk import Stack, Duration, RemovalPolicy, CfnOutput
from aws_cdk import aws_ec2 as ec2, aws_ecs as ecs, aws_iam as iam
from aws_cdk import aws_logs as logs, aws_cloudwatch as cw, aws_cloudwatch_actions as actions
from aws_cdk import aws_sns as sns


class IngestionStack(Stack):
    def __init__(self, scope, construct_id, *, config, state, **kwargs):
        super().__init__(scope, construct_id, termination_protection=True, **kwargs)
        self.node.set_context(f"availability-zones:account={config.account}:region={config.region}",
            [config.region + "a", config.region + "b"])
        # Fixed AZ names avoid availability-zone context lookups during offline synth.
        vpc = ec2.Vpc(self, "Network", availability_zones=[config.region + "a", config.region + "b"],
            nat_gateways=0, subnet_configuration=[ec2.SubnetConfiguration(
                name="Outbound", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24)])
        group = ec2.SecurityGroup(self, "TaskNetwork", vpc=vpc, allow_all_outbound=False)
        group.add_egress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(443), "SEC, provider and AWS HTTPS")
        self.cluster = ecs.Cluster(self, "Cluster", vpc=vpc, cluster_name=config.name)
        trust = iam.ServicePrincipal("ecs-tasks.amazonaws.com", conditions={
            "StringEquals": {"aws:SourceAccount": self.account},
            "ArnLike": {"aws:SourceArn": f"arn:aws:ecs:{self.region}:{self.account}:*"}})
        self.task_role = iam.Role(self, "TaskRole", assumed_by=trust)
        self.execution_role = iam.Role(self, "ExecutionRole", assumed_by=trust)
        def policy(role, ops, resources, **options):
            role.add_to_policy(iam.PolicyStatement(actions=ops, resources=resources, **options))
        for name, table in state.tables.items():
            policy(self.task_role, ["dynamodb:GetItem", "dynamodb:Query"], [table.table_arn])
            if name != "companies":
                policy(self.task_role, ["dynamodb:PutItem", "dynamodb:UpdateItem"], [table.table_arn])
            if name != "calendar":
                policy(self.task_role, ["dynamodb:Query"], [table.table_arn + "/index/*"])
        policy(self.task_role, ["s3:GetObject"], [state.bucket.arn_for_objects("*")])
        policy(self.task_role, ["s3:ListBucket"], [state.bucket.bucket_arn])
        policy(self.task_role, ["s3:PutObject"], [state.bucket.arn_for_objects("*")],
            conditions={"StringEquals": {"s3:if-none-match": "*"}})
        policy(self.task_role, ["s3:DeleteObject", "s3:DeleteObjectVersion"],
            [state.bucket.arn_for_objects("*")], effect=iam.Effect.DENY)
        policy(self.task_role, ["sns:Publish"], [state.topic.topic_arn])
        policy(self.task_role, ["sqs:SendMessage"], [state.failed_work.queue_arn])
        state.repository.grant_pull(self.execution_role)
        log_group = logs.LogGroup(self, "Logs", log_group_name=f"/finbot/{config.name}",
            retention=logs.RetentionDays.ONE_MONTH, removal_policy=RemovalPolicy.RETAIN)
        log_group.grant_write(self.execution_role)
        family = config.name + "-ingestion"
        self.task = ecs.FargateTaskDefinition(self, "Task", family=family,
            cpu=config.cpu, memory_limit_mib=config.memory_mib,
            task_role=self.task_role, execution_role=self.execution_role,
            runtime_platform=ecs.RuntimePlatform(cpu_architecture=ecs.CpuArchitecture.ARM64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX))
        self.task.add_volume(name="heartbeat")
        env = {"AWS_REGION": config.region, "SEC_USER_AGENT": config.sec_user_agent,
            "SEC_MAX_REQUESTS_PER_SECOND": "5", "CALENDAR_PROVIDER": config.calendar_provider,
            "CALENDAR_LOOKAHEAD_DAYS": str(config.calendar_lookahead_days),
            "CALENDAR_FULL_REFRESH_SECONDS": str(config.calendar_full_refresh_seconds),
            "CALENDAR_NEAR_TERM_REFRESH_SECONDS": str(config.calendar_near_term_refresh_seconds),
            "CALENDAR_PROVIDER_ATTEMPTS": "1" if config.calendar_provider == "yahoo" else "3",
            "YAHOO_CACHE_DIR": config.yahoo_cache_dir,
            "ARTIFACT_BUCKET": state.bucket.bucket_name,
            "ARTIFACT_READY_TOPIC_ARN": state.topic.topic_arn,
            "INGESTION_DEAD_LETTER_QUEUE_URL": state.failed_work.queue_url,
            "RUNTIME_SEC_STARTUP_QUIET_SECONDS": "150", "RUNTIME_METRICS_ENVIRONMENT": config.environment}
        env.update({name.upper() + "_TABLE": table.table_name for name, table in state.tables.items()})
        linux = ecs.LinuxParameters(self, "Linux")
        linux.drop_capabilities(ecs.Capability.ALL)
        container = self.task.add_container("Ingestion", container_name="ingestion", essential=True,
            image=ecs.ContainerImage.from_ecr_repository(state.repository, config.image_digest),
            environment=env, user="10001:10001", readonly_root_filesystem=True,
            stop_timeout=Duration.seconds(120),
            linux_parameters=linux,
            logging=ecs.LogDrivers.aws_logs(log_group=log_group, stream_prefix="ingestion"),
            health_check=ecs.HealthCheck(command=["CMD", "python", "-m", "finbot_ingestion.main", "--health-check"],
                interval=Duration.seconds(30), timeout=Duration.seconds(5),
                start_period=Duration.seconds(300), retries=3))
        container.add_mount_points(ecs.MountPoint(source_volume="heartbeat", container_path="/tmp", read_only=False))
        self.service = ecs.FargateService(self, "Service", cluster=self.cluster,
            service_name=config.name, task_definition=self.task, desired_count=0,
            assign_public_ip=True, vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            security_groups=[group], min_healthy_percent=0, max_healthy_percent=100,
            platform_version=ecs.FargatePlatformVersion.VERSION1_4,
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=False), enable_execute_command=False)
        provider = (iam.OpenIdConnectProvider.from_open_id_connect_provider_arn(self, "GitHub",
            config.github_oidc_provider_arn) if config.github_oidc_provider_arn else
            iam.OpenIdConnectProvider(self, "GitHub", url="https://token.actions.githubusercontent.com",
                client_ids=["sts.amazonaws.com"]))
        self.release_role = iam.Role(self, "ReleaseRole", assumed_by=iam.FederatedPrincipal(
            provider.open_id_connect_provider_arn, {"StringEquals": {
                "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                "token.actions.githubusercontent.com:sub": config.github_subject}}, "sts:AssumeRoleWithWebIdentity"))
        state.repository.grant_pull_push(self.release_role)
        policy(self.release_role, ["ecr:DescribeImages"], [state.repository.repository_arn])
        policy(self.release_role, ["ecs:DescribeServices", "ecs:UpdateService"], [self.service.service_arn])
        policy(self.release_role, ["ecs:ListTasks"], ["*"],
            conditions={"ArnEquals": {"ecs:cluster": self.cluster.cluster_arn}})
        policy(self.release_role, ["ecs:DescribeTasks"], [f"arn:aws:ecs:{self.region}:{self.account}:task/{config.name}/*"])
        policy(self.release_role, ["ecs:DescribeTaskDefinition"], ["*"])
        # Current service authorization reference supports task-definition scope.
        # Older ECS policy examples still show '*'; use the current family ARN.
        policy(self.release_role, ["ecs:RegisterTaskDefinition"],
            [f"arn:aws:ecs:{self.region}:{self.account}:task-definition/{family}:*"])
        policy(self.release_role, ["iam:PassRole"], [self.task_role.role_arn, self.execution_role.role_arn],
            conditions={"StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}})
        dimensions = {"Service": "finbot-ingestion", "Environment": config.environment}
        def metric(name, statistic="Maximum"):
            return cw.Metric(namespace="Finbot/Ingestion", metric_name=name,
                dimensions_map=dimensions, statistic=statistic, period=Duration.minutes(1))
        alarm_specs = {
            "RuntimeMissing": (metric("RuntimeHealthy", "Minimum"), 1, cw.ComparisonOperator.LESS_THAN_THRESHOLD, cw.TreatMissingData.BREACHING),
            "CalendarStale": (metric("CalendarStale"), 1, cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD, cw.TreatMissingData.NOT_BREACHING),
            "CalendarScope": (metric("CalendarScopeMatches", "Minimum"), 1, cw.ComparisonOperator.LESS_THAN_THRESHOLD, cw.TreatMissingData.NOT_BREACHING),
            "CalendarProvider": (metric("CalendarProviderConfigured", "Minimum"), 1, cw.ComparisonOperator.LESS_THAN_THRESHOLD, cw.TreatMissingData.NOT_BREACHING),
            "TerminalFailures": (metric("TerminalFailures", "Sum"), 1, cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD, cw.TreatMissingData.NOT_BREACHING),
            "SecErrors": (metric("SecRequestErrors", "Sum"), config.sec_error_threshold, cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD, cw.TreatMissingData.NOT_BREACHING),
            "PublishErrors": (metric("ArtifactPublishFailures", "Sum"), config.publish_error_threshold, cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD, cw.TreatMissingData.NOT_BREACHING),
            "DiscoveryLatency": (metric("DiscoveryLatencyMs"), config.discovery_latency_ms, cw.ComparisonOperator.GREATER_THAN_THRESHOLD, cw.TreatMissingData.NOT_BREACHING),
            "FailedWorkVisible": (state.failed_work.metric_approximate_number_of_messages_visible(period=Duration.minutes(1)), 1, cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD, cw.TreatMissingData.NOT_BREACHING),
            "FailedWorkInflight": (state.failed_work.metric_approximate_number_of_messages_not_visible(period=Duration.minutes(1)), 1, cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD, cw.TreatMissingData.NOT_BREACHING),
            "FailedWorkAge": (state.failed_work.metric_approximate_age_of_oldest_message(period=Duration.minutes(1)), config.failed_work_age_seconds, cw.ComparisonOperator.GREATER_THAN_THRESHOLD, cw.TreatMissingData.NOT_BREACHING),
        }
        for name, (m, threshold, comparison, missing) in alarm_specs.items():
            alarm = cw.Alarm(self, name, metric=m, threshold=threshold, comparison_operator=comparison,
                evaluation_periods=3 if name == "RuntimeMissing" else 2,
                datapoints_to_alarm=3 if name == "RuntimeMissing" else 1,
                treat_missing_data=missing, actions_enabled=config.monitoring_enabled,
                alarm_description="See docs/DEPLOYMENT.md; intentional stops also breach liveness.")
            if config.alarm_action_arn:
                alarm.add_alarm_action(actions.SnsAction(sns.Topic.from_topic_arn(self, name + "Action", config.alarm_action_arn)))
        for name, value in {"ClusterName": self.cluster.cluster_name, "ServiceName": self.service.service_name,
            "TaskFamily": family, "BaselineTaskDefinitionArn": self.task.task_definition_arn,
            "ReleaseRoleArn": self.release_role.role_arn, "TaskRoleArn": self.task_role.role_arn,
            "ExecutionRoleArn": self.execution_role.role_arn, "LogGroupName": log_group.log_group_name}.items():
            CfnOutput(self, name, value=value)
