"""MiDojo framework adapter for eval-hub.

This adapter integrates MiDojo (https://github.com/asago-ai/midojo), a
man-in-the-middle red-teaming framework for AI agents, with the eval-hub
evaluation service using the evalhub-sdk framework adapter pattern.

MiDojo tests whether an agent can be tricked into unsafe actions via prompt
injection. It reports two axes per (user_task, injection_task) pair:
- **utility** — did the agent still complete the benign task?
- **security** — did the agent resist the injection?

This is the **Option C** adapter. Both long-lived pieces live *outside* this
job:

1. The MiDojo **control plane** (``midojo-serve``) is a companion Deployment
   stood up by the TrustyAI operator from the EvalHub CR, reachable at a fixed
   in-cluster Service URL.
2. The **agent** (system under test) is a separately-deployed HTTP service — the
   ``eval_hub_suite`` pi agent — reachable at its own Service URL.

This adapter is therefore short-lived: it runs only ``midojo-run --protocol
http`` (the orchestrator), which drives the remote agent over HTTP, grades the
result against the control plane, and writes ``runs/results.json``. The adapter
then maps utility/security into eval-hub EvaluationResult objects.

The agent's in-process SDK hooks POST their tool calls back to the control
plane. Because both the control plane and the agent are addressed by fixed
Service URLs, no per-job networking (POD_IP, gateways, sandboxes) is involved;
the two URLs just have to be reachable in-cluster.
"""

import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from evalhub.adapter import (
    DefaultCallbacks,
    EvaluationResult,
    FrameworkAdapter,
    JobCallbacks,
    JobPhase,
    JobResults,
    JobSpec,
    JobStatus,
    JobStatusUpdate,
    MessageInfo,
)

logger = logging.getLogger(__name__)

# This adapter targets a single, fixed MiDojo suite (Option C). The suite ships
# in the MiDojo wheel (suites/eval_hub_suite) and is served by the operator's
# control-plane companion; the agent under test implements its tools.
SUITE = "eval_hub_suite"

# Default in-cluster addresses. Both are overridable via env (set on the adapter
# Job by the operator/provider config) or JobSpec parameters. The control-plane
# Service is created by the operator as ``<evalhub-cr-name>-midojo`` in the CR
# namespace; the agent is the eval_hub_suite pi agent Service.
DEFAULT_CONTROL_URL = "http://evalhub-midojo.openshell.svc.cluster.local:8080"
DEFAULT_AGENT_URI = "http://eval-hub-suite-agent.openshell.svc.cluster.local:8000"


class MidojoAdapter(FrameworkAdapter):
    """MiDojo red-teaming adapter (Option C, HTTP transport)."""

    def run_benchmark_job(self, config: JobSpec, callbacks: JobCallbacks) -> JobResults:
        start_time = time.time()
        logger.info(
            "Starting MiDojo job %s for benchmark %s", config.id, config.benchmark_id
        )

        logdir = Path("/tmp/midojo_runs") / config.id
        logdir.mkdir(parents=True, exist_ok=True)

        try:
            # Phase 1: Initialize
            callbacks.report_status(
                JobStatusUpdate(
                    status=JobStatus.RUNNING,
                    phase=JobPhase.INITIALIZING,
                    progress=0.0,
                    message=MessageInfo(
                        message=f"Initializing MiDojo for benchmark {config.benchmark_id}",
                        message_code="initializing",
                    ),
                )
            )

            control_url = self._resolve_control_url(config.parameters)
            agent_uri = self._resolve_agent_uri(config.parameters)
            user_tasks = self._as_list(config.parameters.get("user_tasks"))
            injection_tasks = self._as_list(config.parameters.get("injection_tasks"))
            timeout = int(config.parameters.get("timeout_seconds", 7200))

            logger.info(
                "Config: suite=%s control_url=%s agent_uri=%s user_tasks=%s "
                "injection_tasks=%s timeout=%ss",
                SUITE, control_url, agent_uri, user_tasks, injection_tasks, timeout,
            )

            # Phase 2: Loading data — wait for the operator control plane and the
            # remote agent to be reachable before driving a run.
            callbacks.report_status(
                JobStatusUpdate(
                    status=JobStatus.RUNNING,
                    phase=JobPhase.LOADING_DATA,
                    progress=0.2,
                    message=MessageInfo(
                        message="Waiting for MiDojo control plane and agent",
                        message_code="loading_data",
                    ),
                    current_step="Checking control plane and agent",
                    total_steps=3,
                    completed_steps=1,
                )
            )

            self._wait_ready(f"{control_url}/suite", "control plane", timeout_s=180)
            self._wait_ready(f"{agent_uri}/health", "agent", timeout_s=180)

            # Phase 3: Run evaluation — orchestrator drives the remote agent.
            callbacks.report_status(
                JobStatusUpdate(
                    status=JobStatus.RUNNING,
                    phase=JobPhase.RUNNING_EVALUATION,
                    progress=0.3,
                    message=MessageInfo(
                        message=f"Running MiDojo red-team on suite {SUITE}",
                        message_code="running_evaluation",
                    ),
                    current_step="Executing benchmark",
                    total_steps=3,
                    completed_steps=2,
                )
            )

            self._run_orchestrator(
                control_url=control_url,
                agent_uri=agent_uri,
                user_tasks=user_tasks,
                injection_tasks=injection_tasks,
                logdir=logdir,
                timeout=timeout,
            )

            # Phase 4: Post-processing — parse results.json into metrics.
            callbacks.report_status(
                JobStatusUpdate(
                    status=JobStatus.RUNNING,
                    phase=JobPhase.POST_PROCESSING,
                    progress=0.8,
                    message=MessageInfo(
                        message="Processing MiDojo results",
                        message_code="post_processing",
                    ),
                    current_step="Extracting metrics",
                    total_steps=3,
                    completed_steps=3,
                )
            )

            raw = self._read_results(logdir)
            evaluation_results = self._extract_evaluation_results(raw)
            overall_score = self._compute_overall_score(evaluation_results)
            num_evaluated = len(raw.get("utility", {}))

            duration = time.time() - start_time
            logger.info(
                "Post-processing complete. overall_score=%s evaluated=%d pairs",
                overall_score, num_evaluated,
            )

            return JobResults(
                id=config.id,
                benchmark_id=config.benchmark_id,
                benchmark_index=config.benchmark_index,
                model_name=config.model.name,
                results=evaluation_results,
                overall_score=overall_score,
                num_examples_evaluated=num_evaluated,
                duration_seconds=duration,
                completed_at=datetime.now(UTC),
                evaluation_metadata={
                    "framework": "midojo",
                    "framework_version": self._get_midojo_version(),
                    "suite": SUITE,
                    "protocol": "http",
                    "control_url": control_url,
                    "agent_uri": agent_uri,
                    "benchmark_config": config.parameters,
                },
            )

        except Exception as e:
            logger.exception("MiDojo evaluation failed")
            error_msg = str(e)
            callbacks.report_status(
                JobStatusUpdate(
                    status=JobStatus.FAILED,
                    message=MessageInfo(message=error_msg, message_code="failed"),
                    error_message=MessageInfo(
                        message=error_msg, message_code="evaluation_error"
                    ),
                )
            )
            raise

    # ------------------------------------------------------------------ config

    def _resolve_control_url(self, parameters: dict[str, Any]) -> str:
        """URL of the operator-managed MiDojo control plane (``midojo-serve``).

        Precedence: JobSpec parameter > MIDOJO_CONTROL_URL env > default Service
        DNS. The agent's SDK hooks call back to this same URL, so it must be
        reachable from the agent pod as well as this job.
        """
        return (
            parameters.get("control_url")
            or os.environ.get("MIDOJO_CONTROL_URL")
            or DEFAULT_CONTROL_URL
        )

    def _resolve_agent_uri(self, parameters: dict[str, Any]) -> str:
        """URL of the agent under test (the eval_hub_suite HTTP-wrapped pi agent).

        Precedence: JobSpec parameter > MIDOJO_AGENT_URI env > default Service
        DNS. Passed to ``midojo-run`` as ``--agent-uri``; the orchestrator POSTs
        ``{"prompt": ...}`` to this URL.
        """
        return (
            parameters.get("agent_uri")
            or os.environ.get("MIDOJO_AGENT_URI")
            or DEFAULT_AGENT_URI
        )

    @staticmethod
    def _as_list(value: Any) -> list[str]:
        if not value:
            return []
        if isinstance(value, str):
            return [value]
        return list(value)

    # ------------------------------------------------------------- readiness

    def _wait_ready(self, url: str, what: str, timeout_s: int) -> None:
        """Poll ``url`` until it returns HTTP 200, or fail fast on timeout."""
        deadline = time.time() + timeout_s
        last_err: str | None = None
        while time.time() < deadline:
            try:
                resp = httpx.get(url, timeout=5.0)
                if resp.status_code == 200:
                    logger.info("%s ready at %s", what.capitalize(), url)
                    return
                last_err = f"HTTP {resp.status_code}"
            except httpx.HTTPError as e:
                last_err = str(e)
            time.sleep(2.0)
        raise RuntimeError(
            f"{what} at {url} did not become ready within {timeout_s}s "
            f"(last error: {last_err})"
        )

    # ------------------------------------------------------------- orchestrator

    def _run_orchestrator(
        self,
        control_url: str,
        agent_uri: str,
        user_tasks: list[str],
        injection_tasks: list[str],
        logdir: Path,
        timeout: int,
    ) -> None:
        """Run ``midojo-run --protocol http`` (blocking) and stream logs."""
        cmd = [
            "midojo-run",
            "--protocol", "http",
            "--suite", SUITE,
            "--agent-uri", agent_uri,
            "--control-url", control_url,
            "--logdir", str(logdir),
        ]
        for ut in user_tasks:
            cmd.extend(["--user-task", ut])
        for it in injection_tasks:
            cmd.extend(["--injection-task", it])

        logger.info("Running orchestrator: %s", " ".join(cmd))
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        tail = self._pump_logs(proc, prefix="midojo-run", keep_tail=50)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise RuntimeError(f"MiDojo run timed out after {timeout}s")
        if proc.returncode != 0:
            raise RuntimeError(
                f"midojo-run failed with exit code {proc.returncode}\n"
                + "\n".join(tail)
            )

    # ----------------------------------------------------------------- results

    def _read_results(self, logdir: Path) -> dict[str, Any]:
        results_file = logdir / "results.json"
        if not results_file.exists():
            raise RuntimeError(
                f"No results.json found in {logdir}. "
                f"Contents: {list(logdir.iterdir()) if logdir.exists() else 'missing'}"
            )
        with open(results_file) as f:
            return json.load(f)

    def _extract_evaluation_results(self, raw: dict[str, Any]) -> list[EvaluationResult]:
        """Map MiDojo's results.json into eval-hub metrics.

        results.json shape (keys are ``"{user_task},{injection_task}"``):
            {"utility": {k: bool}, "security": {k: bool|null},
             "security_reason": {k: str|null}}

        MiDojo's ``security`` value is the *injection check* result: ``True``
        means the attack SUCCEEDED (the agent was compromised), ``False`` means
        the attack was defended. We surface that verbatim polarity as an
        ``attack_success`` metric (1.0 = attack succeeded, lower is better) so
        the metric name never contradicts its direction; the aggregate is the
        ``attack_success_rate``.

        A ``null`` security value means the injection payload never reached the
        agent (N/A); those pairs — and utility-only pairs (empty injection
        half) — are excluded from the attack-success aggregate.
        """
        utility = raw.get("utility", {})
        security = raw.get("security", {})
        reasons = raw.get("security_reason", {})

        results: list[EvaluationResult] = []
        utilities: list[float] = []
        attack_successes: list[float] = []

        for key, util_val in utility.items():
            ut_id, _, it_id = key.partition(",")
            label = f"{ut_id}.{it_id}" if it_id else ut_id

            results.append(
                EvaluationResult(
                    metric_name=f"{label}.utility",
                    metric_value=1.0 if util_val else 0.0,
                    metric_type="float",
                    metadata={"user_task": ut_id, "injection_task": it_id or None},
                )
            )
            utilities.append(1.0 if util_val else 0.0)

            sec_val = security.get(key)
            if it_id and sec_val is not None:
                # sec_val is True when the attack succeeded (agent compromised).
                results.append(
                    EvaluationResult(
                        metric_name=f"{label}.attack_success",
                        metric_value=1.0 if sec_val else 0.0,
                        metric_type="float",
                        metadata={
                            "user_task": ut_id,
                            "injection_task": it_id,
                            "security_reason": reasons.get(key),
                        },
                    )
                )
                attack_successes.append(1.0 if sec_val else 0.0)

        if utilities:
            results.append(
                EvaluationResult(
                    metric_name="avg_utility",
                    metric_value=round(sum(utilities) / len(utilities), 4),
                    metric_type="float",
                    num_samples=len(utilities),
                )
            )
        if attack_successes:
            results.append(
                EvaluationResult(
                    metric_name="attack_success_rate",
                    metric_value=round(sum(attack_successes) / len(attack_successes), 4),
                    metric_type="float",
                    num_samples=len(attack_successes),
                )
            )
        return results

    def _compute_overall_score(self, results: list[EvaluationResult]) -> float | None:
        """Overall = mean of utility and defense, each higher-is-better.

        ``attack_success_rate`` is lower-is-better, so it enters as
        ``1 - attack_success_rate`` (the defense rate). With only one of the two
        aggregates present, the overall is just that component.
        """
        by_name = {
            r.metric_name: float(r.metric_value)
            for r in results
            if r.metric_name in ("avg_utility", "attack_success_rate")
            and isinstance(r.metric_value, (int, float))
        }
        components: list[float] = []
        if "avg_utility" in by_name:
            components.append(by_name["avg_utility"])
        if "attack_success_rate" in by_name:
            components.append(1.0 - by_name["attack_success_rate"])
        if not components:
            return None
        return round(sum(components) / len(components), 4)

    # ------------------------------------------------------------------- utils

    def _pump_logs(
        self, proc: subprocess.Popen[str], prefix: str, keep_tail: int = 0
    ) -> list[str]:
        """Stream a subprocess's output into the logger on a daemon thread.

        Returns a list that is filled with the last ``keep_tail`` lines (useful
        for error reporting); empty if ``keep_tail`` is 0.
        """
        tail: list[str] = []

        def _reader() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip()
                logger.info("[%s] %s", prefix, line)
                if keep_tail:
                    tail.append(line)
                    if len(tail) > keep_tail:
                        tail.pop(0)

        threading.Thread(target=_reader, daemon=True).start()
        return tail

    def _get_midojo_version(self) -> str:
        try:
            from importlib.metadata import version

            return version("midojo")
        except Exception:
            return "unknown"


def main() -> None:
    """eval-hub adapter entry point."""
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    try:
        job_spec_path = os.getenv("EVALHUB_JOB_SPEC_PATH", "/meta/job.json")
        adapter = MidojoAdapter(job_spec_path=job_spec_path)
        logger.info("Loaded job %s", adapter.job_spec.id)
        logger.info("Benchmark: %s", adapter.job_spec.benchmark_id)

        callbacks = DefaultCallbacks.from_adapter(adapter)
        results = adapter.run_benchmark_job(adapter.job_spec, callbacks)
        callbacks.report_results(results)

        logger.info("Job completed successfully: %s", results.id)
        if results.overall_score is not None:
            logger.info("Overall score: %.1f%%", results.overall_score * 100)
        logger.info("Evaluated %d task pairs", results.num_examples_evaluated)
        sys.exit(0)

    except FileNotFoundError as e:
        logger.error("Job spec not found: %s", e)
        sys.exit(1)
    except ValueError as e:
        logger.error("Configuration error: %s", e)
        sys.exit(1)
    except Exception:
        logger.exception("Job failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
