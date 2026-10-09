"""Manual infrastructure application. Default inputs are synth-only fixtures."""
import os
from pathlib import Path
from aws_cdk import App, Environment
from config import InfraConfig
from state_stack import StateStack
from ingestion_stack import IngestionStack


def main():
    config = InfraConfig.read(os.environ.get("FINBOT_INFRA_CONFIG", str(Path(__file__).with_name("config.example.json"))))
    app = App()
    env = Environment(account=config.account, region=config.region)
    state = StateStack(app, config.name + "-state", config=config, env=env)
    IngestionStack(app, config.name + "-runtime", config=config, state=state, env=env)
    app.synth()


if __name__ == "__main__":
    main()
