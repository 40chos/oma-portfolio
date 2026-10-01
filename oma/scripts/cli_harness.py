#!/usr/bin/env python3
"""A bare command-line harness for the Manager's loop -- Phase 6 step 7.
The fastest way to actually exercise the loop end to end before the
real chat UI (Phase 12, ui/chat/server.py) existed -- still useful for
quick manual testing without a browser. Type a message, see what
happens. Special commands:

  /confirm <row_id>     -- confirm a just-proposed rule (activates it)
  /reject <row_id>       -- reject a just-proposed rule
  /approve <task_id>     -- approve a pending tier-3/4 sign-off
  /deny <task_id>        -- reject a pending tier-3/4 sign-off
  /quit                  -- exit

Specialist registration is shared with the chat UI server via
scripts/bootstrap_specialists.py -- not duplicated here.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bootstrap_specialists import register_default_specialists  # noqa: E402

from infra.gateway_client import ModelGatewayClient  # noqa: E402
from manager.correction import confirm_proposed_rule, reject_proposed_rule  # noqa: E402
from manager.loop import resume_after_sign_off, run_turn  # noqa: E402

CLASSIFIER_MODEL = os.environ.get("OMA_MODEL_CLASSIFIER", "qwen3.6-27b")


async def main():
    client = ModelGatewayClient()
    real = register_default_specialists(client)
    history: list[dict] = []
    print("Odoo Manager Agent -- CLI harness (Phase 6-12). Type a message, or /quit.")
    if not real:
        print("[NOTE: OMA_ODOO_DB_DUPLICATE_FOR_BUILD not set -- bug_fix/testing_qa use the DEMO "
              "fake specialist; code_review is registered for real regardless.]")
    try:
        while True:
            try:
                line = input("\nOperator> ").strip()
            except EOFError:
                break
            if not line:
                continue
            if line == "/quit":
                break
            if line.startswith("/confirm "):
                row_id = int(line.split(" ", 1)[1])
                confirm_proposed_rule(row_id)
                print(f"[confirmed rule row {row_id} -- now active]")
                continue
            if line.startswith("/reject "):
                row_id = int(line.split(" ", 1)[1])
                reject_proposed_rule(row_id)
                print(f"[rejected rule row {row_id}]")
                continue
            if line.startswith("/approve "):
                task_id = line.split(" ", 1)[1]
                result = await resume_after_sign_off(task_id, True, client, CLASSIFIER_MODEL)
                print(f"[sign-off approved] {result.get('message', result)}")
                continue
            if line.startswith("/deny "):
                task_id = line.split(" ", 1)[1]
                result = await resume_after_sign_off(task_id, False, client, CLASSIFIER_MODEL)
                print(f"[sign-off denied] {result.get('message', result)}")
                continue

            history.append({"role": "user", "content": line})
            result = await run_turn(line, client, CLASSIFIER_MODEL, conversation_history=history)

            if result["status"] == "paused":
                print(f"[PAUSED: {result['reason']}] {result['message']}")
                continue

            corr = result["correction_result"]
            if corr["status"] == "proposed_rule_pending_confirmation":
                print(f"[PROPOSED RULE #{corr['row_id']}]: {corr['directive']!r} "
                      f"(applies when: {corr['applicability_condition']!r}) "
                      f"-- reply /confirm {corr['row_id']} or /reject {corr['row_id']}")
            elif corr["status"] == "logged_as_possible_correction":
                print(f"[noted a possible correction, confidence={corr['confidence']:.2f}, "
                      f"logged for review, not auto-enforced]")

            reply = (
                f"Manager> task {result['task_id']}: "
                f"{'PASSED' if result['passed'] else 'FAILED'} "
                f"(tier={result['tier']}, capability_class={result['capability_class']})\n"
                f"  {result['verification'].notes}"
            )
            print(reply)
            history.append({"role": "assistant", "content": reply})
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
