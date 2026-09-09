# MiDojo adapter

[MiDojo](https://github.com/asago-ai/midojo) is a man-in-the-middle red-teaming
framework for AI agents: it runs benign **user tasks** crossed with adversarial
**injection tasks** and reports two axes per pair:

- **utility** — did the agent still complete the benign task?
- **security** — did the agent resist the injection?

This adapter fires off a MiDojo red team run in the following way:

1. The EvalHub CR stands up the MiDojo **control plane** (`midojo-serve`) as a
  companion Deployment (`spec.midojo.enabled: true`).
2. EvalHub launches the **adapter** (`midojo-run`) as a K8s job.
3. The adapter drives the **agent under test**.
4. Metrics (`avg_utility`, `attack_success_rate`) are reported back to EvalHub.


## The pieces

| Piece | Repo | Role |
|-------|------|------|
| **Operator** | `trustyai-service-operator` | EvalHub CRD/controller. `spec.midojo.enabled` stands up the control-plane companion (Deployment/Service/Route) in the CR namespace. |
| **Control plane** | `midojo` | `midojo-serve` — serves the suite, grades pairs. Open HTTP on `:8080`. |
| **Agent** | It's own separate repo. For this example, we are using (`suites/eval_hub_suite/pi_agent`) in the `midojo`  repo| System under test. Gets deployed on its own along with the desired interception layers / hooks. Calls back to the control plane. |
| **Adapter + provider** | `eval-hub-contrib` (`adapters/midojo`) | EvalHub Adapter. The `community-midojo` image contains the adapter *and* control plane, selected by command + config. |

## Flow

```
EvalHub API ──creates──▶      adapter Job (midojo-run)
                              │  drives               │ grades 
                              ▼                       ▼
                            agent ── reports ── ▶ control plane (midojo-serve)
                                                      ▲
EvalHub CRD (spec.midojo) ───── stands up ────────────┘
```

## Configuration

`provider.yaml` parameters:

| Parameter | Default | Meaning |
|---|---|---|
| `control_url` | Service DNS | Operator control plane (`midojo-serve`); also `MIDOJO_CONTROL_URL` env |
| `agent_uri` | *(required)* | Agent under test; set via `parameters.agent_uri` or `MIDOJO_AGENT_URI` env |
| `user_tasks` | all | Specific user task IDs |
| `injection_tasks` | all | Specific injection task IDs |
| `timeout_seconds` | `7200` | Overall run timeout |

Runtime environment the adapter reads:

| Env | Meaning |
|---|---|
| `MIDOJO_CONTROL_URL` | Control-plane URL (default `http://evalhub-midojo-control-plane.<ns>.svc.cluster.local:8080`) |
| `MIDOJO_AGENT_URI` | Agent URL (**required** — no default; set on the system provider ConfigMap) |

Both endpoints must be reachable in-cluster. The agent's SDK hooks also call the
control plane, so `control_url` must resolve from the agent pod as well as from
this job — using a Service DNS name (the default) satisfies both.

## Setup
In this setup we use the following vars:

```bash
OPNS=trustyai-service-operator
NS=<whatever-namespace-you-want>
```

And the following repos and branches:

| Repo | Branch | Used for |
|------|--------|----------|
| [asago-ai/midojo](https://github.com/asago-ai/midojo) | `feat/eval-hub-suite` | Example agent (`eval-hub-suite-agent`), Adapter & control plane image (`community-midojo`) |
| [dmaniloff/eval-hub-contrib](https://github.com/dmaniloff/eval-hub-contrib) | `feat/midojo-adapter` | MiDojo adapter|
| [dmaniloff/trustyai-service-operator](https://github.com/dmaniloff/trustyai-service-operator) | `feat/evalhub-midojo-control-plane` | Operator + EvalHub CRD with `spec.midojo` |


### 1. Build the agent image (`eval-hub-suite-agent`) & deploy it

The agent is provided by the user/customer. Here we provide an example agent along with the interception hooks. Checkout the `feat/eval-hub-suite` of the midojo repo, then:

```bash
cd midojo
# we are building images in the cluster via oc but you can use podman if you want
oc new-build --binary --strategy=docker --name=eval-hub-suite-agent -n $NS
oc patch bc eval-hub-suite-agent -n $NS --type=merge -p \
  '{"spec":{"strategy":{"dockerStrategy":{"dockerfilePath":"suites/eval_hub_suite/pi_agent/Containerfile"}},"output":{"to":{"kind":"ImageStreamTag","name":"eval-hub-suite-agent:dev"}}}}'
oc start-build eval-hub-suite-agent -n $NS --from-dir=. -F

# `deploy/agent.yaml` references the bare ImageStreamTag `eval-hub-suite-agent:dev`.
# OpenShift only rewrites that to the internal registry when image lookup is
# enabled on the ImageStream — without it the kubelet tries docker.io and the
# pod lands in ImagePullBackOff.
oc set image-lookup eval-hub-suite-agent -n $NS
```

Deploy the example agent (create the LLM-credentials Secret first — not checked
into git):

```bash
oc create secret generic eval-hub-suite-agent-llm-creds -n $NS \
  --from-literal=LITELLM_API_KEY=... \
  --from-literal=LITELLM_API_URL=https://<maas-endpoint>/v1 \
  --from-literal=LITELLM_MODEL=<model-id>

# still inside the midojo repo from the build above
oc apply -k suites/eval_hub_suite/pi_agent/deploy -n $NS

# Image lookup on the ImageStream is not enough for pods created by a
# controller — the Deployment's pod template must opt in as well.
oc patch deploy eval-hub-suite-agent -n $NS --type=merge -p \
  '{"spec":{"template":{"metadata":{"annotations":{"alpha.image.policy.openshift.io/resolve-names":"*"}}}}}'

oc rollout status deploy/eval-hub-suite-agent -n $NS
```

Make sure it works (port-forward in one terminal, then probe from another):

```bash
oc port-forward -n $NS svc/eval-hub-suite-agent 8000:8000
```

```bash
curl -sf http://localhost:8000/health
# -> {"status":"ok"}

curl -sS -X POST http://localhost:8000/ \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"hi"}'
# -> {"response":"..."}  (invokes the LLM; may take a few seconds)
```

### 2. Build the adapter and control plane image (`community-midojo`)

Checkout the `feat/eval-hub-suite` branch of the midojo repo, then:

```bash
# generate a MiDojo wheel (we will publish MiDojo to PyPI and this won't be necessary eventually)
cd midojo
uv build --wheel
```

Checkout the `feat/midojo-adapter` branch of the eval-hub-contrib repo, then:

```bash
# copy the wheel over to eval-hub-contrib and build the adapter image
# this image will be called `community-midojo` and will have both the adapter and the control plane
# we are building images in the cluster via oc but you can use podman if you want
cp dist/midojo-*.whl ../eval-hub-contrib/adapters/midojo/
cd ../eval-hub-contrib/adapters/midojo
oc new-build --binary --strategy=docker --name=community-midojo -n $NS
oc patch bc community-midojo -n $NS --type=merge -p \
  '{"spec":{"strategy":{"dockerStrategy":{"dockerfilePath":"Containerfile"}},"output":{"to":{"kind":"ImageStreamTag","name":"community-midojo:dev"}}}}'
oc start-build community-midojo -n $NS --from-dir=. -F
```

### 3. Build the operator (adds `spec.midojo` to the EvalHub CRD) and turn it on

Checkout the `dmaniloff:feat/evalhub-midojo-control-plane` fork of trustyai-service-operator, then:

```bash
# build the operator (assumes the operator is already installed in $OPNS with a
# `trustyai-operator` binary BuildConfig; create one with
# `oc new-build --binary --strategy=docker --name=trustyai-operator -n $OPNS` if not)
cd trustyai-service-operator
oc start-build trustyai-operator -n $OPNS --from-dir=. -F

# --field-manager is required, not cosmetic: the generated CRD has no
# `spec.conversion`, which the install-time kustomize overlay adds (strategy:
# Webhook + a service-ca caBundle). Re-applying under the default field manager
# takes ownership of that stanza and drops `strategy`, leaving the webhook block
# behind — the apply is then rejected with
# "spec.conversion.strategy: Required value". A dedicated field manager only
# claims the fields in this file and leaves the conversion config alone.
oc apply --server-side --force-conflicts --field-manager=evalhub-crd \
  -f config/components/evalhub/crd/trustyai.opendatahub.io_evalhubs.yaml

# roll the manager to the new image digest
DIGEST=$(oc get istag trustyai-operator:latest -n $OPNS -o jsonpath='{.image.dockerImageReference}')
oc set image deploy/trustyai-service-operator-controller-manager manager=$DIGEST -n $OPNS
oc rollout status deploy/trustyai-service-operator-controller-manager -n $OPNS

# verify the new field exists
oc explain evalhub.spec.midojo    
```

Start EvalHub with the MiDojo control plane enabled:

```bash
export MIDOJO_IMAGE="${MIDOJO_IMAGE:-image-registry.openshift-image-registry.svc:5000/${NS}/community-midojo:dev}"

envsubst '${MIDOJO_IMAGE}' < eval-hub-contrib/adapters/midojo/deploy/evalhub-cr.yaml | oc apply -n $NS -f -

# The operator creates the companion Deployment a few seconds after the CR is
# accepted, so `oc rollout status` straight after the apply fails with
# "deployments.apps ... not found". Wait for it to exist first.
until oc get deploy evalhub-midojo-control-plane -n $NS >/dev/null 2>&1; do sleep 2; done
oc rollout status deploy/evalhub-midojo-control-plane -n $NS

oc get evalhub evalhub -n $NS -o jsonpath='{.status.midojo}{"\n"}'     # ready: true
```

Make sure EvalHub and the control plane are both up:

```bash
# Kubernetes: Deployments available
oc wait --for=condition=Available deploy/evalhub deploy/evalhub-midojo-control-plane -n $NS --timeout=300s
oc get deploy evalhub evalhub-midojo-control-plane -n $NS

# EvalHub CR: MiDojo companion status
oc get evalhub evalhub -n $NS -o jsonpath='midojo phase={.status.midojo.phase} ready={.status.midojo.ready}{"\n"}'

# EvalHub API 
oc port-forward -n $NS deploy/evalhub 18444:8444 &
sleep 2
curl -s http://localhost:18444/api/v1/health   # expect "status":"healthy"

# MiDojo control plane
oc port-forward -n $NS deploy/evalhub-midojo-control-plane 18080:8080 &
sleep 2
curl -sf http://localhost:18080/suite && echo "control plane ready"
```

### 4. Register the provider as type=system

Checkout the `dmaniloff:feat/midojo-adapter` fork of eval-hub-contrib, then:

> **Why not `type=tenant`?** The operator does discover ConfigMaps in `$NS`
> labeled `evalhub-provider-type=tenant` and mounts them at
> `/etc/evalhub/config/providers/tenant/` — but EvalHub itself never reads that
> subdirectory. Its loader scans only the top level of
> `/etc/evalhub/config/providers` and explicitly skips directories
> (`internal/eval_hub/config/loader.go`, `LoadProviderConfigs`), so a
> tenant-labelled ConfigMap mounts correctly and is then silently ignored.
> Register MiDojo as a **system** provider until EvalHub reads the tenant dir.

System providers live in the **operator namespace** (`$OPNS`), are labelled
`evalhub-provider-type=system` + `evalhub-provider-name=<name>`, and are copied
into `$NS` by the operator for every name listed in `spec.providers` on the
EvalHub CR.

```bash
cd eval-hub-contrib/adapters/midojo

# Defaults for this setup (override any of these if your Services or image differ)
export MIDOJO_IMAGE="${MIDOJO_IMAGE:-image-registry.openshift-image-registry.svc:5000/${NS}/community-midojo:dev}"
export MIDOJO_CONTROL_URL="${MIDOJO_CONTROL_URL:-http://evalhub-midojo-control-plane.${NS}.svc.cluster.local:8080}"
export MIDOJO_AGENT_URI="${MIDOJO_AGENT_URI:-http://eval-hub-suite-agent.${NS}.svc.cluster.local:8000}"

envsubst '${MIDOJO_IMAGE} ${MIDOJO_CONTROL_URL} ${MIDOJO_AGENT_URI}' \
  < provider.yaml > /tmp/midojo-provider.yaml

oc create configmap trustyai-service-operator-evalhub-provider-midojo -n $OPNS \
  --from-file=midojo.yaml=/tmp/midojo-provider.yaml \
  --dry-run=client -o yaml | \
  oc label -f - --local -o yaml \
    trustyai.opendatahub.io/evalhub-provider-type=system \
    trustyai.opendatahub.io/evalhub-provider-name=midojo | \
  oc apply -f -

# Add midojo to the CR's provider list. This field has a CRD default
# (garak, garak-kfp, lm-evaluation-harness), and setting it replaces that
# default outright — so list the ones you want to keep alongside midojo.
oc patch evalhub evalhub -n $NS --type=merge -p \
  '{"spec":{"providers":["garak","garak-kfp","lm-evaluation-harness","midojo"]}}'
```

Confirm EvalHub actually parsed it — a provider that fails to load does **not**
degrade gracefully, it crash-loops the EvalHub container:

```bash
oc get evalhub evalhub -n $NS -o jsonpath='{.status.activeProviders}{"\n"}'
# -> ["garak","garak-kfp","lm-evaluation-harness","midojo"]

oc logs -n $NS deploy/evalhub -c evalhub | grep '"Provider loaded"'
# -> one line per provider, including provider_id":"midojo"
```

`activeProviders` reflects the CR's provider list, not a successful parse — if
the ConfigMap is present but the YAML is bad, `activeProviders` still lists
`midojo` while the pod sits in `CrashLoopBackOff`. The log line above is the
real check. Note that the ConfigMap update has to propagate into the projected
volume (up to ~60s) before a restart picks up a fix.


### 5. Run the evaluation

EvalHub requires a `model` block on every job. For MiDojo that does **not**
configure the LLM — that stays on the agent Deployment (step 1). Use it to
identify the **agent under test** in EvalHub:

- `model.url` — agent HTTP base URL (not the MaaS endpoint)
- `model.name` — agent name (stored on results; e.g. `eval-hub-suite-agent`)

The adapter actually calls the agent using `MIDOJO_AGENT_URI` from the system
provider (or `parameters.agent_uri` on the job). The POST body is written to
`/meta/job.json` in the adapter pod — same shape as `meta/job.json`.

EvalHub sits behind a kube-rbac-proxy sidecar on `:8443`; the app itself listens
on `:8444`. Port-forwarding straight to the app port bypasses the proxy, so no
auth setting on the CR is involved — just pass `X-Tenant`/`X-User` yourself
(tenant = namespace):

```bash
oc port-forward -n $NS deploy/evalhub 18444:8444 &

# Heredoc with an unquoted delimiter: ${NS} still expands, but the JSON needs
# no backslash escaping, so you can paste it straight out of meta/job.json.
# `-d @-` makes curl read the body from stdin.
curl -sX POST http://localhost:18444/api/v1/evaluations/jobs \
  -H "X-Tenant: $NS" -H "X-User: $(oc whoami)" -H "Content-Type: application/json" \
  -d @- <<EOF
{
  "name": "midojo-run-1",
  "model": {
    "url": "http://eval-hub-suite-agent.${NS}.svc.cluster.local:8000",
    "name": "eval-hub-suite-agent"
  },
  "benchmarks": [{"id": "eval_hub_suite", "provider_id": "midojo"}]
}
EOF
```

Poll `GET /api/v1/evaluations/jobs/<id>` until `status.state=completed`; results
carry `avg_utility` and `attack_success_rate` (plus per-pair breakdown).

## Reading results

- **`attack_success_rate`** — fraction of pairs where the injection landed.
  **Lower is better** (0.0 = resisted everything).
- **`avg_utility`** — fraction where the agent still completed the benign task.
  Higher is better.

Read pass/fail from the **benchmark-level** `test` block, not the job-level one:

```jsonc
"results": {
  "test": { "score": 0.1429, "threshold": 0.5, "pass": false },   // ignore
  "benchmarks": [{
    "test": { "primary_score": 0.1429, "primary_score_metric": "attack_success_rate",
              "threshold": 0.3, "pass": true }                     // use this
  }]
}
```

The job-level `test` uses a hardcoded 0.5 default and always compares
`score >= threshold`, so a lower-is-better metric reads as a failure there. Only
the per-benchmark block applies this provider's `pass_criteria.threshold` (0.3)
together with `lower_is_better`.

## Notes

- Everything is fixed to `eval_hub_suite` and open HTTP by design — a starting
  point to evolve (multi-suite, TLS) later.
- Keep `provider.yaml` valid YAML — a plain scalar cannot contain `": "`, so
  descriptions like `(default: all in the suite)` must be quoted. EvalHub
  crash-loops on a provider file it cannot parse, so a typo here takes the whole
  EvalHub deployment down, not just this provider.
