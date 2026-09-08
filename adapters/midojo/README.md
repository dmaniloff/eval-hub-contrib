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
```

Deploy the example agent:

```bash
oc create secret generic eval-hub-suite-agent-llm-creds -n $NS \
  --from-literal=LITELLM_API_KEY=... \
  --from-literal=LITELLM_API_URL=https://<maas-endpoint>/v1 \
  --from-literal=LITELLM_MODEL=<model-id>
oc apply -k midojo/suites/eval_hub_suite/pi_agent/deploy -n $NS
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
# build the operator
cd trustyai-service-operator
oc start-build trustyai-operator -n $OPNS --from-dir=. -F
oc apply --server-side --force-conflicts \
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
oc rollout status deploy/evalhub-midojo -n $NS
oc get evalhub evalhub -n $NS -o jsonpath='{.status.midojo}{"\n"}'     # ready: true
```

Make sure EvalHub and the control plane are both up:

```bash
# Kubernetes: Deployments available
oc wait --for=condition=Available deploy/evalhub deploy/evalhub-midojo -n $NS --timeout=300s
oc get deploy evalhub evalhub-midojo -n $NS

# EvalHub CR: MiDojo companion status
oc get evalhub evalhub -n $NS -o jsonpath='midojo phase={.status.midojo.phase} ready={.status.midojo.ready}{"\n"}'

# HTTP: EvalHub API (unauthenticated; app listens on :8444 in the pod)
EH_POD=$(oc get pods -n $NS -l app=eval-hub,instance=evalhub -o jsonpath='{.items[0].metadata.name}')
oc port-forward -n $NS "pod/$EH_POD" 18444:8444 &
sleep 2
curl -s http://localhost:18444/api/v1/health   # expect "status":"healthy"

# HTTP: MiDojo control plane (/suite returns 200 once eval_hub_suite is loaded)
MJ_POD=$(oc get pods -n $NS -l component=midojo,instance=evalhub -o jsonpath='{.items[0].metadata.name}')
oc port-forward -n $NS "pod/$MJ_POD" 18080:8080 &
sleep 2
curl -sf http://localhost:18080/suite && echo "control plane ready"
```

### 4. Register the provider as type=tenant

Checkout the `dmaniloff:feat/midojo-adapter` fork of eval-hub-contrib, then:

The operator auto-discovers provider ConfigMaps in the **EvalHub instance
namespace** (`$NS`) labeled
`trustyai.opendatahub.io/evalhub-provider-type=tenant`, mounts them at
`/etc/evalhub/config/providers/tenant/`, and hot-reloads them — no entry in
`spec.providers` on the EvalHub CR is required. Create/patch the CM in `$NS` so
its benchmark is `eval_hub_suite` and its image points at `community-midojo:dev`:

```bash
cd eval-hub-contrib/adapters/midojo

# Defaults for this setup (override any of these if your Services or image differ)
export MIDOJO_IMAGE="${MIDOJO_IMAGE:-image-registry.openshift-image-registry.svc:5000/${NS}/community-midojo:dev}"
export MIDOJO_CONTROL_URL="${MIDOJO_CONTROL_URL:-http://evalhub-midojo.${NS}.svc.cluster.local:8080}"
export MIDOJO_AGENT_URI="${MIDOJO_AGENT_URI:-http://eval-hub-suite-agent.${NS}.svc.cluster.local:8000}"

envsubst '${MIDOJO_IMAGE} ${MIDOJO_CONTROL_URL} ${MIDOJO_AGENT_URI}' \
  < provider.yaml > /tmp/midojo-provider.yaml

oc create configmap evalhub-provider-midojo -n $NS \
  --from-file=midojo.yaml=/tmp/midojo-provider.yaml \
  --dry-run=client -o yaml | \
  oc label -f - --local -o yaml \
    trustyai.opendatahub.io/evalhub-provider-type=tenant \
    trustyai.opendatahub.io/evalhub-provider-name=midojo | \
  oc apply -f -
```


### 5. Run the evaluation

EvalHub requires a `model` block on every job. For MiDojo that does **not**
configure the LLM — that stays on the agent Deployment (step 1). Use it to
identify the **agent under test** in EvalHub:

- `model.url` — agent HTTP base URL (not the MaaS endpoint)
- `model.name` — agent name (stored on results; e.g. `eval-hub-suite-agent`)

The adapter actually calls the agent using `MIDOJO_AGENT_URI` from the tenant
provider (or `parameters.agent_uri` on the job). The POST body is written to
`/meta/job.json` in the adapter pod — same shape as `meta/job.json`.

EvalHub sits behind kube-rbac-proxy on `:8443`; the app listens on `:8444`.
With `disable_auth: true` you can port-forward the app port directly and pass
`X-Tenant`/`X-User` (tenant = namespace):

```bash
POD=$(oc get pods -n $NS -o name | grep -E 'pod/evalhub-[0-9a-f]' | head -1)
oc port-forward -n $NS $POD 18444:8444 &

curl -sX POST http://localhost:18444/api/v1/evaluations/jobs \
  -H "X-Tenant: $NS" -H "X-User: $(oc whoami)" -H "Content-Type: application/json" \
  -d '{"name":"midojo-run-1",
       "model":{"url":"<agent-url>","name":"<agent-name>"},
       "benchmarks":[{"id":"eval_hub_suite","provider_id":"midojo"}]}'
```

Poll `GET /api/v1/evaluations/jobs/<id>` until `status.state=completed`; results
carry `avg_utility` and `attack_success_rate` (plus per-pair breakdown).

## Reading results

- **`attack_success_rate`** — fraction of pairs where the injection landed.
  **Lower is better** (0.0 = resisted everything).
- **`avg_utility`** — fraction where the agent still completed the benign task.
  Higher is better.

## Notes

- Everything is fixed to `eval_hub_suite` and open HTTP by design — a starting
  point to evolve (multi-suite, TLS) later.
