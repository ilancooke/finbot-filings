### AWS implementation guidance

When working with AWS services:

- Consult relevant installed AWS skills for implementation guidance.
- Use the `aws-mcp` server to verify current AWS APIs, SDK behavior, service constraints, and recommended patterns when relevant.
- Prefer authoritative AWS documentation over assumptions from model training.
- Treat the project's HLD, LLD, ADRs, and migration plan as architectural sources of truth. These files are in docs folder.
- Use AWS MCP for documentation and read-only resource inspection only. Never create, modify, or delete AWS resources without explicit authorization.
- Keep AWS integration tests offline and mocked unless explicitly authorized otherwise.