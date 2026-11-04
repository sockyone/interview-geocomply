# AI usage

## Tools

- **Claude Code** (Anthropic), in the Claude desktop app, as an agentic coding assistant with
  shell access to this workstation: it read and edited files, ran tests, drove the local kind
  cluster and Docker registry, and read logs.
- No other AI tools were used for code. <!-- Edit this line if you used others. -->

I made the decisions on scope, design and developer experience, and I reviewed the results.
The assistant wrote most of the code, tests and first drafts of the documents.

## Agentic workflow and configuration

**A living plan as shared memory.** `docs/design/implementation-plan.md` was the contract
between sessions. It holds the goal and constraints, seven checkpoints (ApplicationDefinition →
database/seed → TestSuite → load profile → published definition → LoadTestRun → results/CI),
"decisions to preserve", and a progress record with evidence for every slice. Every new
session started with: *"Continue working from this plan."*. Every slice ended with *"Update the
implementation plan."*. The plan's handoff procedure tells the agent to:

1. read the entity definitions, the plan and the relevant code first;
2. recheck tools and infrastructure instead of assuming they exist;
3. implement only the first unfinished checkpoint, as a working slice;
4. separate mock/render checks from real integration checks in the record;
5. never mark a checkpoint complete just because the code exists.

**Global instructions** (`~/.claude/CLAUDE.md`) set the working rules for the agent:

- the toolchain: Homebrew, uv/pyenv, Docker through Colima;
- ask before pushing, deploying or deleting data;
- never read or print secrets (`.env`, SSH keys, cloud credentials, kubeconfig contents).

The code follows the same rules: credentials are redacted from logs, and the kubeconfig stays
private under `.runtime/`.

**Safety boundaries for live work.** The agent used one explicit kubeconfig/context
(`kind-performance-test`) and never the default kubeconfig. It deleted only namespaces
carrying its own run label, and never deleted the shared cluster or registry.

## Representative prompts

Steering the design (developer experience):

> "Why is it `checkout-flows`? Why do users have to set that? … What does `journeys: {browse: 3, purchase: 1}` mean?"

> "1. … Remove the ConfigMap from the settings; it should be handled behind the scenes. 2. Why do we need weights? Just make a simple case: users browse 3 items, wait a little (2 seconds), then purchase. Simulate 10 users doing this."

> "The load profile doesn't mention the name of the test suite, so how does it know where to find functions like 'browse' or 'purchase'? … It makes a very implicit connection. … We might remove the load profile and move everything into the load test definition."

These turned an engine-centric design (separate LoadProfile files, journey weights, a
user-named ConfigMap) into the current model: one definition file with an ordered per-user
flow, and the ConfigMap named automatically from the content hash.

Execution and evidence:

> "Run a load test and show me the logs." · "Show me the test definition you wrote and the results you captured." · "What output do we get from Prometheus? Can we have it as a PDF or an image?"


## How AI output was validated

1. **Automated checks for every change.** The default suite runs without a cluster on every PR
   (96 checks). It covers definition/schema validation, Job manifests, chart rendering,
   lifecycle state handling with a fake cluster, cleanup safety, redaction, the report, and
   guardrail rules.
2. **Opt-in checks against real systems**, kept separate so a mocked check is never taken
   as proof:
   - `SUITE_LOCAL_TEST`: real HTTP and Locust;
   - `RUN_KIND_TEST`: full lifecycle on kind, including threshold failure, cancellation and
     seed deadline;
   - `GUARDRAILS_KIND_TEST`: NetworkPolicy, LimitRange and quota enforced by the cluster.
3. **Golden end-to-end runs** after each slice. The plan records the run ID, thresholds, sample
   counts, peak resources and the evidence path. I read the report and logs myself, not
   only the agent's summary.
4. **Negative tests on purpose.** Each guardrail and failure path was triggered, and the
   result checked: oversized test rejected before deployment; failed threshold → exit 1;
   Ctrl-C → exit 130 with cleanup; seed timeout → exit 2.
5. **Skepticism about "it passed".** Example: the first NetworkPolicy probe showed every
   destination as reachable. Instead of accepting or "fixing" the policy, we read kindnet's
   nftables rules. The probe had connected before kindnet had programmed the new Pod (about
   1 s). After adding a wait, the probe showed the expected allow/deny pattern, and that
   finding is now documented. Similarly, a passing golden run was not accepted as proof of
   isolation; a dedicated probe was required.
6. **Human review of contracts and docs.** I reviewed every user-facing file (definition
   format, README, design docs) for clarity. Several rounds of redesign came from that
   review, not from failing tests.
