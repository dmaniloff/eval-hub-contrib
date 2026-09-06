# MiDojo adapter

[MiDojo](https://github.com/asago-ai/midojo) is a man-in-the-middle red-teaming
framework for AI agents: it runs benign **user tasks** crossed with adversarial
**injection tasks** and reports two axes per pair:

- **utility** — did the agent still complete the benign task?
- **security** — did the agent resist the injection?

This adapter runs MiDojo over its **HTTP** transport (**Option C**). The two
long-lived pieces live *outside* this job:

- the MiDojo **control plane** (`midojo-serve --suite eval_hub_suite`) is a
  companion Deployment stood up by the TrustyAI operator from the EvalHub CR
  (`spec.midojo.enabled: true`);
- the **agent** under test is a separately-deployed HTTP service — the
  `eval_hub_suite` pi agent (see `midojo/suites/eval_hub_suite/pi_agent`).

The adapter is therefore short-lived: `main.py` runs only the orchestrator.

## How it works

When the container runs `python main.py`, the adapter:

1. Resolves the control-plane URL and agent URI (from JobSpec parameters, env,
   or in-cluster Service DNS defaults) and waits for both to be ready
   (`GET <control_url>/suite` and `GET <agent_uri>/health`).
2. Runs **`midojo-run --protocol http --suite eval_hub_suite --agent-uri <agent>
   --control-url <control>`** (the orchestrator), which POSTs each prompt to the
   agent, lets the agent's SDK hooks call back to the control plane, grades the
   result, and writes `runs/results.json`.
3. Maps `results.json` into eval-hub `EvaluationResult`s (`<pair>.utility`,
   `<pair>.attack_success`, and the `avg_utility` / `attack_success_rate`
   aggregates). `attack_success_rate` is lower-is-better (fraction of graded
   injection pairs where the attack succeeded), so the composite is
   `overall_score = mean(avg_utility, 1 - attack_success_rate)`.

The suite (`eval_hub_suite`) is fixed for this adapter and ships in the MiDojo
wheel; the agent's tools implement it.

## Configuration

`provider.yaml` parameters:

| Parameter | Default | Meaning |
|---|---|---|
| `control_url` | Service DNS | Operator control plane (`midojo-serve`); also `MIDOJO_CONTROL_URL` env |
| `agent_uri` | Service DNS | Agent under test (`eval_hub_suite` pi agent); also `MIDOJO_AGENT_URI` env |
| `user_tasks` | all | Specific user task IDs |
| `injection_tasks` | all | Specific injection task IDs |
| `timeout_seconds` | `7200` | Overall run timeout |

Runtime environment the adapter reads:

| Env | Meaning |
|---|---|
| `MIDOJO_CONTROL_URL` | Control-plane URL (default `http://evalhub-midojo.<ns>.svc.cluster.local:8080`) |
| `MIDOJO_AGENT_URI` | Agent URL (default `http://eval-hub-suite-agent.<ns>.svc.cluster.local:8000`) |

Both endpoints must be reachable in-cluster. The agent's SDK hooks also call the
control plane, so `control_url` must resolve from the agent pod as well as from
this job — using a Service DNS name (the default) satisfies both.

## Build

MiDojo is a private package, so the image installs it from a locally-built wheel
staged into the build context. The `image-midojo` Makefile target does this for
you (it builds the wheel from `MIDOJO_SRC`, defaulting to `../midojo`):

```sh
# from the repo root
make image-midojo REGISTRY=quay.io/your-org VERSION=dev
make push-midojo  REGISTRY=quay.io/your-org VERSION=dev
```

Manual equivalent:

```sh
(cd ../../midojo && uv build --wheel) && cp ../../midojo/dist/midojo-*.whl .
podman build -t community-midojo:dev -f Containerfile .
```

The same image serves the operator's control-plane companion — its Deployment
just overrides the command to `midojo-serve --suite eval_hub_suite ...`.

## Deploy (Option C, OpenShift)

```sh
# 1. Enable the control plane on the EvalHub CR (companion Deployment/Service).
oc apply -f deploy/evalhub-cr.yaml

# 2. Deploy the agent under test (from the midojo repo).
#    oc apply -k <midojo>/suites/eval_hub_suite/pi_agent/deploy -n openshell

# 3. Register the provider, then run the adapter.
oc apply -f deploy/evalhub-provider-midojo-system.yaml   # operator namespace
oc apply -f deploy/adapter-job.yaml                       # one-shot run
oc logs -n openshell -f job/midojo-eval-hub-suite
```

## Test

```sh
make test-midojo
```

The tests monkeypatch the orchestrator subprocess and the readiness polls, so no
real control plane or agent is contacted — they cover config resolution, the
`results.json` → metrics mapping (including N/A / utility-only exclusion from the
security aggregate), the orchestrator command wiring, and the phase lifecycle.
