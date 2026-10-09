# ADR 008: Use conservative ECS automatic recovery in v0

- Status: Accepted (2026-10-09); implemented in Phase 8 (2026-10-09), not deployed

## Context

ECS replaces crashed or unhealthy service tasks automatically. Finbot's SEC budget
is process-local, so two processes contacting SEC could exceed the shared ceiling.
The ECS deployment upper bound counts RUNNING/PENDING tasks and does not establish
that a STOPPING container has finished executing.

Fast discovery around expected earnings is a primary purpose of this service.
Nevertheless, v0 accepts a recovery interruption to keep the initial architecture
simple and reduce overlap risk without distributed coordination.

## Decision

- Keep ECS automatic recovery, with active desired count 1,
  `minimumHealthyPercent=0` and `maximumPercent=100`.
- Set Fargate container `stopTimeout=120` seconds.
- In the deployed runtime, wait 150 seconds before any outbound SEC request on
  every process start, including recovery, retries and redirects. Configure this
  delay explicitly for ECS; local execution defaults to no startup delay.
- On SIGTERM, stop admitting all new SEC requests, including queued work, retries
  and redirects. Permit already dispatched I/O and safe durable checkpoints to
  finish; leave interrupted work for restart recovery without counting shutdown
  as a workflow failure.
- Give ECS health checks a 300-second startup allowance to accommodate the quiet
  period and initialization. Calendar readiness remains separate from liveness;
  calendar outages alarm without causing otherwise healthy tasks to restart.
- Controlled releases and infrastructure maintenance stop the old task, wait for
  STOPPED, then start the replacement. The startup delay still applies.
- Do not introduce distributed coordination in v0.

This is a conservative recovery policy, not a formal mutual-exclusion guarantee.
The startup delay is a buffer against shutdown overlap; AWS does not document a
hard end-to-end handoff bound that makes this delay proof of exclusion.
The user accepts that limitation for v0.

## Consequences

- ECS can restore a failed service without waiting for an operator.
- Each startup adds 2.5 minutes before SEC polling resumes. Failure detection,
  shutdown, task provisioning and initialization can make the total outage longer.
- During active earnings windows, the delay can be substantial compared with the
  normal seconds-scale polling cadence and can miss the intended discovery target.
  Durable recovery preserves work but cannot recover lost timeliness.
- Current Phase 7 shutdown drains queues and can start further SEC requests;
  Phase 8 adds a dispatch admission guard, not merely a scheduler admission stop.
- Offline tests establish application behavior and configuration, not a guarantee
  about every ECS control-plane failure scenario.

## Revisit when

Revisit after measuring recovery during active earnings windows, or sooner if the
150-second quiet period prevents required ingestion latency. Also revisit if a
strict global request ceiling across failures becomes mandatory, overlap is
observed, or multiple ingestion tasks become necessary.

Track the future investigation in [BACKLOG.md, ARCH-001](../BACKLOG.md#arch-001--reduce-recovery-delay-during-active-earnings-windows).
A future decision should compare faster verified handoff, coordination/fencing,
and centralized SEC dispatch, with realistic failure tests and measured costs.
These are investigation candidates, not authorized additions to v0.

## References

- [ECS service replacement behavior](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ecs_services.html)
- [ECS deployment upper bounds](https://docs.aws.amazon.com/sdk-for-kotlin/api/latest/ecs/aws.sdk.kotlin.services.ecs.model/-deployment-configuration/-builder/maximum-percent.html)
- [ECS graceful shutdown](https://aws.amazon.com/blogs/containers/graceful-shutdowns-with-ecs/)
- [ECS health-check startup allowance](https://docs.aws.amazon.com/cdk/api/v2/dotnet/api/Amazon.CDK.AWS.ECS.CfnTaskDefinition.HealthCheckProperty.html)
