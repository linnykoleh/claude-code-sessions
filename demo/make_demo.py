"""Make a fake Claude Code data folder for the README demo, so no real session ends up in the GIF.

    python demo/make_demo.py /tmp/ccs-demo
    CLAUDE_CONFIG_DIR=/tmp/ccs-demo/claude XDG_CACHE_HOME=/tmp/ccs-demo/cache CCS_OFFLINE=1 ccs

Every project, title, prompt and path here is made up. Times are relative to now, so the
"Today" and "Last 7 days" rows always have something in them. Two sessions are shown as open
in Claude Code: each gets a `sleep` process to stand in for a running Claude (they exit after
--live-seconds).
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Illustrative prices in USD per token, enough to give the demo realistic-looking costs.
# "context" is the context window in tokens, as ccs keeps it from LiteLLM's table.
PRICES = {
    "claude-opus-5-5": {"input": 5e-6, "output": 25e-6, "cache_write": 6.25e-6, "cache_write_1h": 10e-6,
                        "cache_read": 0.5e-6, "context": 1_000_000},
    "claude-sonnet-5-5": {"input": 3e-6, "output": 15e-6, "cache_write": 3.75e-6, "cache_write_1h": 6e-6,
                          "cache_read": 0.3e-6, "context": 1_000_000},
}
CONTEXT_PER_RESPONSE = 4_000  # how much each response adds to the context, so long sessions fill up more
# Sessions whose context ends elsewhere, so the CTX column shows yellow and red too (share of the 1M window)
CONTEXT_AT_END = {"Add idempotency keys to webhooks": 0.86, "Upgrade React Native to 0.79": 0.71}

HOME = "/Users/you/code"
MINUTE, HOUR, DAY = 60, 3600, 86400

# (project, branch, title, started this long ago, lasted, responses, state, prompts)
# state: "busy" = Claude is working now, "idle" = open and waiting, "cut" = stopped mid-turn, "" = finished
SESSIONS = [
    ("payments-api", "PAY-512-refund-retries", "Fix retry logic in the refund worker", 3 * HOUR, 3 * HOUR - 60, 90, "busy", [
        "The refund worker retries forever when the bank API returns 409. Find out why.",
        "Make the retry policy configurable and add a test for the 409 case.",
        "Run the whole test suite and fix what breaks.",
        "Write the MR description.",
    ]),
    ("infra", "region-eu-west-3", "Terraform plan for the new region", 5 * HOUR, 40 * MINUTE, 35, "idle", [
        "We are adding eu-west-3. Copy the eu-central-1 stack and adjust the CIDRs.",
        "Show me the plan, only the resources that change.",
    ]),
    ("web-app", "main", "Dark mode for the settings page", 7 * HOUR, 55 * MINUTE, 40, "", [
        "Add a dark theme toggle to the settings page, keep the choice in localStorage.",
        "The toggle flickers on page load. Fix it.",
    ]),
    ("payments-api", "PAY-498-idempotency", "Add idempotency keys to webhooks", DAY + 4 * HOUR, 9 * HOUR, 120, "cut", [
        "Webhooks are sometimes delivered twice. Add idempotency keys.",
        "Store the keys in Redis with a 24h TTL.",
        "Now the integration test for duplicate deliveries.",
    ]),
    ("docs", "onboarding", "Rewrite the onboarding guide", DAY + 2 * HOUR, 12 * MINUTE, 12, "", [
        "Rewrite docs/onboarding.md: shorter, steps in the order a new hire needs them.",
    ]),
    ("data-pipeline", "DP-77-late-events", "Late events break the daily aggregation", 2 * DAY + 2 * HOUR, 2 * HOUR, 70, "", [
        "Events that arrive after midnight land in the wrong day. Why?",
        "Use event time, not ingestion time, and backfill last week.",
    ]),
    ("mobile-app", "release-4.2", "Crash on Android 15 when opening the camera", 2 * DAY + 6 * HOUR, 50 * MINUTE, 45, "", [
        "Users on Android 15 crash when they open the camera screen. Here is the stack trace.",
    ]),
    ("auth-service", "AUTH-301-sso", "SAML login for enterprise customers", 3 * DAY + 3 * HOUR, 4 * HOUR, 150, "", [
        "Plan SAML SSO for enterprise tenants. What do we need to change?",
        "Implement the ACS endpoint and the metadata endpoint.",
        "Add tests with a fake IdP.",
    ]),
    ("web-app", "perf-bundle", "Cut the bundle size of the dashboard", 4 * DAY, 1 * HOUR, 50, "", [
        "The dashboard bundle is 2.4 MB. Find what is heavy.",
    ]),
    ("infra", "main", "Alert when disk usage goes above 85%", 4 * DAY + 5 * HOUR, 20 * MINUTE, 18, "", [
        "Add a Prometheus alert for disk usage above 85% for 10 minutes.",
    ]),
    ("payments-api", "PAY-470-currency", "Support JPY amounts without decimals", 5 * DAY, 90 * MINUTE, 60, "", [
        "JPY has no minor units. Our amount formatting adds .00. Fix it everywhere.",
    ]),
    ("docs", "api-reference", "Generate the API reference from OpenAPI", 6 * DAY, 30 * MINUTE, 25, "", [
        "Generate docs/api from openapi.yaml and add it to the docs build.",
    ]),
    ("data-pipeline", "main", "Explain the nightly job failures", 8 * DAY, 25 * MINUTE, 20, "", [
        "The nightly job failed three times this week. Look at the logs and explain.",
    ]),
    ("auth-service", "AUTH-288-tokens", "Rotate refresh tokens on every use", 10 * DAY, 2 * HOUR, 80, "", [
        "Rotate refresh tokens on every use and revoke the whole family on reuse.",
    ]),
    ("mobile-app", "main", "Upgrade React Native to 0.79", 12 * DAY, 2 * DAY, 160, "", [
        "Upgrade React Native to 0.79 and fix the build.",
        "iOS build still fails on the pods. Look again.",
    ]),
    ("web-app", "a11y", "Accessibility audit of the checkout", 15 * DAY, 70 * MINUTE, 55, "", [
        "Audit the checkout pages for accessibility issues and fix the obvious ones.",
    ]),
    ("infra", "k8s-1-33", "Upgrade the clusters to Kubernetes 1.33", 18 * DAY, 5 * HOUR, 140, "", [
        "Plan the upgrade of staging and prod to Kubernetes 1.33.",
    ]),
    ("payments-api", "main", "Why is p99 latency up since Monday?", 21 * DAY, 45 * MINUTE, 40, "", [
        "p99 of POST /payments went from 300 ms to 900 ms on Monday. Find out why.",
    ]),
    ("data-pipeline", "DP-60-dbt", "Move the reports to dbt", 25 * DAY, 2 * DAY, 130, "", [
        "Move the SQL reports in reports/ to dbt models with tests.",
    ]),
    ("docs", "main", "Fix broken links in the docs", 28 * DAY, 10 * MINUTE, 10, "", [
        "Find and fix broken links in docs/.",
    ]),
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("out", type=Path, help="folder to create (replaced if it exists)")
    parser.add_argument("--live-seconds", type=int, default=600, help="how long the two open sessions stay open")
    args = parser.parse_args()

    if args.out.exists():
        shutil.rmtree(args.out)
    claude_dir, cache_dir = args.out / "claude", args.out / "cache" / "cc-sessions"
    cache_dir.mkdir(parents=True)
    (cache_dir / "prices-v3.json").write_text(json.dumps(PRICES))
    (claude_dir / "sessions").mkdir(parents=True)

    rng = random.Random(42)
    now = time.time()
    for project, branch, title, ago, lasted, responses, state, prompts in SESSIONS:
        sid = str(uuid.UUID(int=rng.getrandbits(128), version=4))
        cwd = f"{HOME}/{project}"
        records = transcript(rng, sid, cwd, branch, title, now - ago, lasted, responses, state, prompts)
        path = claude_dir / "projects" / cwd.replace("/", "-") / f"{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in records))
        if state in ("busy", "idle"):
            pid = subprocess.Popen(["sleep", str(args.live_seconds)], start_new_session=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).pid
            (claude_dir / "sessions" / f"{pid}.json").write_text(
                json.dumps({"pid": pid, "sessionId": sid, "cwd": cwd, "status": state}))
    print(f"{len(SESSIONS)} demo sessions in {claude_dir}")


def transcript(rng, sid, cwd, branch, title, start, lasted, responses, state, prompts):
    """Records of one session: each prompt, then Claude's responses and the turn's duration."""
    base = {"sessionId": sid, "cwd": cwd, "gitBranch": branch}
    records = [{"type": "ai-title", "aiTitle": title, "sessionId": sid}]
    per_prompt = max(1, responses // len(prompts))
    step = lasted / len(prompts)
    final = CONTEXT_AT_END.get(title, 0.0) * 1_000_000 or responses * CONTEXT_PER_RESPONSE
    sent, total = 0, per_prompt * len(prompts)
    for i, prompt in enumerate(prompts):
        t = start + i * step
        records.append({**base, "type": "user", "timestamp": iso(t), "message": {"role": "user", "content": prompt}})
        last_turn = i == len(prompts) - 1
        if last_turn and state == "busy":
            per_prompt //= 2  # still working: no end of turn yet
        work = min(step * 0.6, rng.uniform(2, 12) * MINUTE)
        for j in range(per_prompt):
            sent += 1
            t_resp = t + work * (j + 1) / per_prompt
            done = j == per_prompt - 1 and not (last_turn and state in ("busy", "cut"))
            records.append({**base, "type": "assistant", "timestamp": iso(t_resp), "requestId": f"req_{uuid.UUID(int=rng.getrandbits(128))}",
                            "message": {"id": f"msg_{uuid.UUID(int=rng.getrandbits(128))}",
                                        "model": "claude-opus-5-5" if rng.random() < 0.8 else "claude-sonnet-5-5",
                                        "stop_reason": "end_turn" if done else "tool_use",
                                        "content": [{"type": "text", "text": "Done."} if done
                                                    else {"type": "tool_use", "name": "Bash"}],
                                        "usage": usage(rng, int(final * sent / total))}})
        if last_turn and state == "cut":
            records.append({**base, "type": "user", "timestamp": iso(t + work + 5),
                            "message": {"content": [{"type": "text", "text": "[Request interrupted by user]"}]}})
        elif not (last_turn and state == "busy"):
            records.append({**base, "type": "system", "subtype": "turn_duration", "durationMs": int(work * 1000),
                            "timestamp": iso(t + work)})
    return records


def usage(rng, context: int):
    """One response with `context` tokens in its window: almost all of it read from the prompt cache."""
    written, fresh = rng.randint(1_000, 8_000), rng.randint(2, 50)
    return {"input_tokens": fresh, "output_tokens": rng.randint(200, 2_500),
            "cache_creation_input_tokens": written, "cache_creation": {"ephemeral_1h_input_tokens": written},
            "cache_read_input_tokens": max(context - written - fresh, 15_000)}


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    main()
