from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

import uvicorn

from .agent import AgentRunner
from .api import create_app
from .config import load_agent_config, load_central_config
from .logging_setup import configure_logging


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rtmp-monitor", description="RTMP stream monitoring agent and central server")
    sub = parser.add_subparsers(dest="command", required=True)
    agent = sub.add_parser("agent", help="run a probe agent")
    agent.add_argument("--config", default=os.environ.get("RTMP_MONITOR_CONFIG", "config/agent.yaml"))
    server = sub.add_parser("server", help="run the central API and dashboard")
    server.add_argument("--config", default=os.environ.get("RTMP_MONITOR_CONFIG", "config/central.yaml"))
    server.add_argument("--host")
    server.add_argument("--port", type=int)
    token = sub.add_parser("show-admin-token", help="print the dashboard token from the configured local token file")
    token.add_argument("--config", default=os.environ.get("RTMP_MONITOR_CONFIG", "config/central.yaml"))
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    if args.command == "agent":
        config = load_agent_config(args.config)
        configure_logging(config.log_dir, agent_name=config.agent.name)
        logging.getLogger(__name__).info("Starting probe agent %s (%s)", config.agent.name, config.agent.role)
        asyncio.run(AgentRunner(config).run())
    elif args.command == "server":
        config = load_central_config(args.config)
        configure_logging(config.logs_dir, config.log_retention_days)
        app = create_app(config)
        uvicorn.run(app, host=args.host or config.bind_host, port=args.port or config.bind_port, log_config=None, access_log=False)
    else:
        config = load_central_config(args.config)
        path = config.admin_token_file
        if not path.exists():
            parser = argparse.ArgumentParser()
            parser.error(f"Admin token has not been created yet. Start the central server once; token file will be created at {path}")
        print(path.read_text(encoding="utf-8").strip())


if __name__ == "__main__":
    main()
