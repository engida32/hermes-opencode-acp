"""Opt-in real OpenCode ACP handshake/config smoke test, without inference.

Unlike the offline suite, this requires OpenCode and may make its normal catalog
network calls and local records. It sends no chat prompt or Hermes user context.
"""
import argparse
import json
import os
from pathlib import Path
import sys

from support import client as acp, constants


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--command", help="Operator-selected OpenCode executable")
    source.add_argument("--npm-prefix", type=Path, help="Test an npm install's actual platform-specific .bin launcher")
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    command = args.command
    if args.npm_prefix:
        name = "opencode.cmd" if os.name == "nt" else "opencode"
        command = str((args.npm_prefix / "node_modules/.bin" / name).resolve())
    client = acp.OpenCodeFreeClient(command=command)
    try:
        worker = client._ensure_worker(args.timeout)
        with worker.rpc_lock:
            session = worker.new_session(args.timeout)
            advertised = set(acp.options(acp.config_option(session, "model")))
            models = [model for model in constants.FREE_MODELS if model in advertised]
            if not models:
                raise RuntimeError("No curated free models were advertised; no inference was attempted")
            mode = acp.config_option(session, "mode", acp.BRIDGE_MODE)
            model = acp.config_option(session, "model", models[0])
            for option, value in ((mode, acp.BRIDGE_MODE), (model, models[0])):
                result = worker.request("session/set_config_option", {
                    "sessionId": session["sessionId"], "configId": option["id"], "value": value,
                }, timeout=args.timeout)
                if acp.selected_value(result, option["id"]) != value:
                    raise RuntimeError("OpenCode did not confirm the selected mode/model; no inference was attempted")
        print(json.dumps({"ok": True, "platform": sys.platform, "bridge_version": constants.VERSION,
                          "command": client.command, "mode": acp.BRIDGE_MODE, "free_models": models,
                          "inference_requested": False}, ensure_ascii=True, indent=2))
    finally:
        client.close()


if __name__ == "__main__":
    main()
