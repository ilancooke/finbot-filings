"""GitHub release entry point; execution explicitly pushes/releases to existing AWS."""
import base64
import json
import os
import re
import subprocess
from contextlib import closing

import boto3
from botocore.config import Config
from release import Release, ReleaseError, release_signals


def main():
    values = os.environ
    region, uri = values["AWS_REGION"], values["FINBOT_REPOSITORY_URI"]
    if not re.fullmatch(r"\d{12}\.dkr\.ecr\." + re.escape(region) + r"\.amazonaws\.com/[a-z0-9/_-]+", uri):
        raise ReleaseError("invalid target repository/region")
    sdk = Config(connect_timeout=5, read_timeout=10, retries={"mode": "standard", "total_max_attempts": 3})
    session = boto3.Session(region_name=region)
    digest = values.get("ROLLBACK_DIGEST", "")
    tag = None
    with closing(session.client("ecr", config=sdk)) as ecr, closing(session.client("ecs", config=sdk)) as ecs:
        auth = ecr.get_authorization_token()["authorizationData"][0]
        username, password = base64.b64decode(auth["authorizationToken"]).decode().split(":", 1)
        registry = uri.split("/", 1)[0]
        subprocess.run(["docker", "login", "--username", username, "--password-stdin", registry],
            input=password.encode(), check=True, timeout=60)
        try:
            if digest:
                if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                    raise ReleaseError("invalid rollback digest")
                image = uri + "@" + digest
                subprocess.run(["docker", "pull", "--platform", "linux/arm64", image], check=True, timeout=180)
            else:
                commit = values["RELEASE_COMMIT"]
                if not re.fullmatch(r"[0-9a-f]{40}", commit):
                    raise ReleaseError("invalid release commit")
                tag = f"{commit}-{values['GITHUB_RUN_ID']}-{values['GITHUB_RUN_ATTEMPT']}"
                image = uri + ":" + tag
                subprocess.run(["docker", "tag", "finbot-ingestion:release", image], check=True, timeout=30)
            arch = subprocess.check_output(["docker", "image", "inspect", image, "--format", "{{.Architecture}}"], timeout=30).decode().strip()
            if arch != "arm64":
                raise ReleaseError("image must be ARM64")
            if not digest:
                subprocess.run(["docker", "push", image], check=True, timeout=300)
                response = ecr.describe_images(repositoryName=uri.split("/", 1)[1], imageIds=[{"imageTag": tag}])
                digest = response["imageDetails"][0]["imageDigest"]
            runner = Release(ecs=ecs, ecr=ecr, cluster=values["FINBOT_CLUSTER"], service=values["FINBOT_SERVICE"],
                repository_uri=uri, baseline=values["FINBOT_BASELINE"], manifest_path="release-manifest.json")
            runner.record(image_tag=tag)
            with release_signals():
                runner.deploy(digest, activate=values.get("ACTIVATE") == "true",
                    activation_approved=values.get("FINBOT_ACTIVATION_APPROVED") == "true",
                    source_commit=values["RELEASE_COMMIT"])
        finally:
            subprocess.run(["docker", "logout", registry], check=False, timeout=30)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
