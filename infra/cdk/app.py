"""Manual infrastructure application. Default inputs are synth-only fixtures."""
import os
from pathlib import Path
from aws_cdk import App, Environment
from config import InfraConfig
from state_stack import StateStack
from ingestion_stack import IngestionStack


def main():
    config_path = os.environ.get("FINBOT_INFRA_CONFIG")
    private_path = os.environ.get("FINBOT_INFRA_PRIVATE_CONFIG")
    if config_path is None:
        if private_path is not None:
            raise ValueError("FINBOT_INFRA_PRIVATE_CONFIG requires FINBOT_INFRA_CONFIG")
        config_path = Path(__file__).with_name("config.example.json")
        private_path = Path(__file__).with_name("config.private.example.json")
    config = InfraConfig.read(config_path, private_path=private_path)
    app = App()
    env = Environment(account=config.account, region=config.region)
    state = StateStack(app, config.name + "-state", config=config, env=env)
    IngestionStack(app, config.name + "-runtime", config=config, state=state, env=env)
    app.synth()


if __name__ == "__main__":
    main()
