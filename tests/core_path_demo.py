#!/usr/bin/env python3
"""spawn-governor verification harness — the whole story through core's OWN pre-spawn path.

    python tests/core_path_demo.py
    HERMES_HOME=/tmp/sg-demo python tests/core_path_demo.py   # ledger lands under $HERMES_HOME/logs

WHY IT LIVES UNDER ``tests/``: it imports two Hermes internals on purpose — ``agent.delegation_registry``
(to register the provider) and ``tools.delegate_tool_routes`` (``resolve_task_routes``, the exact
function ``delegate_task`` calls at ``tools/delegate_tool.py:511``). That is the point: the refusal and
the downgrade below are core's resolution, not a private harness. It is a development harness and
never runs inside Hermes, which is the same reason the catalogue validator skips ``tests/`` when it
lints for private imports. The plugin's own runtime imports only ``agent.delegation_provider`` — a
documented public surface.

Requires Hermes >= 0.22.5 (the delegation-provider surface).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = {"backend": "spawn_governor", "model": "big"}
BACKEND = {"window": "total", "estimate_chars_per_token": 4,
           "rates": {"big": 0.14, "cheap": 0.001}}


def _load_plugin():
    spec = importlib.util.spec_from_file_location("spawn_governor", ROOT / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # type: ignore[union-attr]
    return mod


def _cfg(**over):
    backend = {**BACKEND, **over}
    return {**BASE, "backends": {"spawn_governor": backend}}


def main() -> int:
    try:
        from agent.delegation_provider import ROUTE_KEYS  # noqa: F401
        from agent import delegation_registry as registry
        from tools.delegate_tool_routes import resolve_task_routes
    except Exception as exc:  # core is older than the delegation-provider surface
        print(f"spawn-governor needs Hermes >= 0.22.5 (delegation provider surface): {exc}")
        return 2

    home = os.environ.get("HERMES_HOME") or tempfile.mkdtemp(prefix="spawn-governor-demo-")
    os.environ["HERMES_HOME"] = home
    Path(home).mkdir(parents=True, exist_ok=True)
    ledger = Path(home) / "logs" / "spawn-governor-ledger.jsonl"
    ledger.unlink(missing_ok=True)
    # the reconciliation hook reads its own settings from config.yaml, so give it one
    (Path(home) / "config.yaml").write_text(
        "delegation:\n  backend: spawn_governor\n  backends:\n    spawn_governor:\n"
        f"      rates:\n        big: 0.14\n        cheap: 0.001\n", encoding="utf-8")

    mod = _load_plugin()
    registry._registry.register(mod.SpawnGovernor())
    provider = registry.get_active_provider(_cfg(envelope_usd=0.30))
    parent = types.SimpleNamespace(model="big", provider="custom", base_url="", api_mode="")

    def resolve(cfg, tasks):
        return resolve_task_routes(tasks, cfg, parent)[0]

    def kinds():
        if not ledger.exists():
            return []
        return [json.loads(line)["kind"] for line in ledger.read_text(encoding="utf-8").splitlines()]

    line = "-" * 78
    print("spawn-governor — admission control for delegation")
    print(f"core selected the backend by name: {provider.name}")
    print(line)

    print("\n[1] ARM        envelope $0.30 / total · rates: big 0.14/1k · cheap 0.001/1k")

    print("\n[2] REFUSE     3 tasks requested; task 1 is forecast over the remaining headroom")
    cfg = _cfg(envelope_usd=0.30)
    ledger.unlink(missing_ok=True)
    for i, route in enumerate(resolve(cfg, [
            {"goal": "summarise the changelog", "context": "tidbits", "index": 0},
            {"goal": "x" * 40000, "context": "", "index": 1},
            {"goal": "check the ledger tail", "context": "short", "index": 2}])):
        if route.error:
            print(f"              task {i}: REFUSED   {route.error.split(': ', 1)[-1][:96]}…")
            print(f"              receipt:  {json.dumps(route.refused_entry(i))[:104]}…")
        else:
            print(f"              task {i}: routed    (siblings are unaffected)")

    print("\n[3] DOWNGRADE  on_breach: downgrade — the spawn runs CHEAPER instead of dying")
    ledger.unlink(missing_ok=True)
    cfg = _cfg(envelope_usd=0.30, on_breach="downgrade",
               downgrade_ladder=[{"model": "cheap", "junk_key": "silently dropped"}])
    route = resolve(cfg, [{"goal": "x" * 40000, "context": "", "index": 0}])[0]
    print(f"              refused? {bool(route.error)}   resolved model: {route.creds.get('model')}")
    print(f"              label:    {route.label}")
    print(f"              ledger:   {kinds()}")

    print("\n[4] LOOP       the forecast is steered by what the provider ACTUALLY reported")
    ledger.unlink(missing_ok=True)
    cfg = _cfg(envelope_usd=0)
    mod._settings_from_config_file._cache = cfg["backends"]["spawn_governor"]  # type: ignore[attr-defined]
    task = {"goal": "z" * 4000, "context": "", "index": 0}
    for step in range(3):
        if step:
            mod._on_post_api_request(model="big",
                                     usage={"input_tokens": 4000, "output_tokens": 8000})
        resolve(cfg, [dict(task)])
        last = json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])
        print(f"              step {step}: forecast ${last['usd']:.4f}   "
              f"calibration factor {last['calibration']:.2f}  (pairs {last['calibration_pairs']})")

    print("\n[5] HONEST     the refusal says *forecast*; the ledger says *actual*.")
    print(f"              ledger kinds: {kinds()}")
    print(line)
    print("Routing plugins pick a route and cannot price it. Metering plugins price it and cannot")
    print("act on it. This one refuses the spawn, or takes a cheaper rung, before it starts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
