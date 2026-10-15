"""
service/scheduler_jobs.py

The set of job types the scheduler can run — the ONE place a new kind of
scheduled work plugs in.

This file is deliberately NOT tied to any single source. It holds the small
strategy classes (one per job_type) and a registry the runner dispatches
through exactly once. The runner (service/scheduler.py) owns the lifecycle —
recording the run, notifying, computing the next fire time — and never needs
to know what a job actually does. Adding a job type is a new class here plus
one line in REGISTRY; the runner is not reopened.

Jira-specific logic does NOT live here — it stays in service/jira_sync.py and
service/jira_adapter.py. JiraSyncJob is only a thin wrapper that calls into
them and maps the result onto the common shape.
"""

from dataclasses import dataclass, field
from typing import Optional, Protocol


@dataclass
class JobResult:
    """
    The common outcome shape every job returns, so the runner can persist and
    report any job type identically.

    Attributes:
        status: Short outcome recorded on the schedule and its run.
            Suite runs: 'passed' | 'failed' | 'error'.
            Jira sync:  'changes_found' | 'current'.
        summary: The full result dict — written to schedule_runs.summary (JSONB)
            and sent in the webhook payload. Job-specific detail lives here so
            it is never forced into fixed columns.
        counters: Numeric roll-up for schedule_runs' typed columns
            {total, passed, failed, skipped}. Suite runs fill these; a Jira
            sync leaves them empty (those columns are suite-semantics and do
            not apply to a sync — its detail is carried in `summary`).
        event: Event name in the webhook payload, so n8n can route suite
            completions and sync completions differently.
    """

    status: str
    summary: dict
    counters: dict = field(default_factory=dict)
    event: str = "scheduled_run_complete"


class ScheduleJob(Protocol):
    """
    The contract every job type implements. Duck-typed via typing.Protocol —
    a job class does not need to inherit anything, it just needs these members.
    """

    job_type: str

    def validate(self, schedule: dict) -> Optional[str]:
        """Return an error string if this schedule cannot run, else None."""
        ...

    def run(self, schedule: dict, triggered_by: str) -> JobResult:
        """Do the work for this schedule and return a JobResult."""
        ...


class SuiteRunJob:
    """
    Runs a test suite — the original scheduled behaviour, unchanged.

    Wraps api.routes.suites.run_suite so a scheduled run and the "Run now"
    button share exactly the same suite-execution path.
    """

    job_type = "suite_run"

    def validate(self, schedule: dict) -> Optional[str]:
        """A suite schedule cannot run without a suite attached.

        Args:
            schedule: The schedule row fields the runner passes in.

        Returns:
            An error string if no suite is set, else None.
        """
        if not schedule.get("suite_id"):
            return "This schedule has no suite attached."
        return None

    def run(self, schedule: dict, triggered_by: str) -> JobResult:
        """Run the schedule's suite headless and map the outcome.

        Args:
            schedule: The schedule row fields (suite_id, environment, ...).
            triggered_by: 'scheduler' or 'manual', passed through to the run.

        Returns:
            A JobResult with pass/fail counters populated.
        """
        # Imported here (not at module load) to avoid a circular import with
        # the routes package — the same reason scheduler.py imported it lazily.
        from api.routes.suites import run_suite, RunSuiteRequest

        # Scheduled runs are always headless — nobody watches a 6am run, and a
        # visible browser would need a desktop session.
        summary = run_suite(schedule["suite_id"], RunSuiteRequest(
            headed=False,
            environment=schedule.get("environment"),
            triggered_by=triggered_by,
        ))

        if summary.get("error"):
            status = "error"
        elif summary.get("failed"):
            status = "failed"
        else:
            status = "passed"

        counters = {
            "total": summary.get("total", 0),
            "passed": summary.get("passed", 0),
            "failed": summary.get("failed", 0),
            "skipped": summary.get("skipped", 0),
        }
        return JobResult(
            status=status,
            summary=summary,
            counters=counters,
            event="scheduled_run_complete",
        )


class JiraSyncJob:
    """
    Runs one Jira change-detection pass.

    A thin wrapper over service.jira_sync.sync_application. It DETECTS and
    FLAGS only — it never generates or edits a test case. Regenerating tests
    from a flagged requirement is a separate, explicit human action (the GxP
    boundary: no unreviewed AI change to a validated artefact).
    """

    job_type = "jira_sync"

    def validate(self, schedule: dict) -> Optional[str]:
        """A Jira schedule cannot run without a target project.

        Args:
            schedule: The schedule row fields the runner passes in.

        Returns:
            An error string if no Jira project is set, else None.
        """
        if not schedule.get("jira_project_key"):
            return "This schedule has no Jira project set."
        return None

    def run(self, schedule: dict, triggered_by: str) -> JobResult:
        """Run one change-detection pass and map the outcome.

        Args:
            schedule: The schedule row fields (application_id, jira_project_key).
            triggered_by: 'scheduler' or 'manual' (unused by the sync itself,
                kept for signature symmetry with other jobs).

        Returns:
            A JobResult. Counters are empty — pass/fail is not meaningful for a
            sync; the counts a human cares about (changed / flagged / new) are
            in `summary`.
        """
        from service.jira_sync import sync_application

        result = sync_application(
            schedule["application_id"],
            project_key=schedule["jira_project_key"],
        )
        changed = result.get("changed_requirements") or []
        new = result.get("new_requirements") or []
        # A sync that found nothing is a healthy 'current', not a failure — do
        # not overload 'failed'. Finding changes is the expected good outcome.
        status = "changes_found" if (changed or new) else "current"

        return JobResult(
            status=status,
            summary=result,
            counters={},
            event="scheduled_jira_sync_complete",
        )


# The bounded set of job types the scheduler can run. Adding a type is one new
# class above + one entry here — the runner never changes. Keeping this list
# explicit is itself a control: nothing runs on a schedule that is not here.
REGISTRY: dict[str, ScheduleJob] = {
    job.job_type: job
    for job in (SuiteRunJob(), JiraSyncJob())
}


def get_job(job_type: str) -> Optional[ScheduleJob]:
    """Look up the job for a schedule's job_type.

    Args:
        job_type: The schedule's job_type column value.

    Returns:
        The matching job instance, or None if the type is not registered.
    """
    return REGISTRY.get(job_type)
