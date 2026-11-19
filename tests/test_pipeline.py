"""Processing jobs: resumption after failure, retry limits and error handling."""

import pytest

from doc_intelligence.models import Collection, Document, JobStatus, ProcessingJob, User
from doc_intelligence.pipeline import MAX_ATTEMPTS, StageError, run_job, stage_verify_file
from tests.conftest import make_pdf


@pytest.fixture
async def job_id(app):
    storage = app.state.storage
    async with app.state.db.sessionmaker() as s:
        user = User(email="p@example.com", password_hash="x")
        s.add(user)
        await s.flush()
        col = Collection(owner_id=user.id, name="c")
        s.add(col)
        await s.flush()
        doc = Document(
            owner_id=user.id, collection_id=col.id, title="a", original_filename="a.pdf", file_format="pdf",
            media_type="application/pdf", size_bytes=1, sha256="0" * 64, storage_key=storage.put(make_pdf()),
        )
        s.add(doc)
        await s.flush()
        job = ProcessingJob(document_id=doc.id, owner_id=user.id)
        s.add(job)
        await s.commit()
        return job.id


async def load(app, job_id) -> ProcessingJob:
    async with app.state.db.sessionmaker() as s:
        return await s.get(ProcessingJob, job_id)


def counting_stage(calls: list[str], name: str, fail_times: int = 0, retryable: bool = True):
    async def stage(ctx):
        calls.append(name)
        if calls.count(name) <= fail_times:
            raise StageError("provider timeout", retryable=retryable)

    return stage


async def test_resumes_from_failed_stage_without_repeating_done_stages(app, job_id):
    calls: list[str] = []
    stages = [
        ("a", counting_stage(calls, "a")),
        ("b", counting_stage(calls, "b", fail_times=1)),
        ("c", counting_stage(calls, "c")),
    ]
    run = lambda: run_job(job_id, app.state.db.sessionmaker, app.state.storage, stages)  # noqa: E731

    assert await run() == JobStatus.queued  # transient failure: queued for retry
    job = await load(app, job_id)
    assert job.completed_stages == ["a"]
    assert job.error == "b: provider timeout"

    assert await run() == JobStatus.completed
    assert calls == ["a", "b", "b", "c"]  # "a" was not run again
    job = await load(app, job_id)
    assert job.completed_stages == ["a", "b", "c"]
    assert job.attempts == 2
    assert job.error is None


async def test_permanent_failure_is_not_retried(app, job_id):
    calls: list[str] = []
    stages = [("a", counting_stage(calls, "a", fail_times=99, retryable=False))]
    status = await run_job(job_id, app.state.db.sessionmaker, app.state.storage, stages)
    assert status == JobStatus.failed


async def test_gives_up_after_max_attempts(app, job_id):
    calls: list[str] = []
    stages = [("a", counting_stage(calls, "a", fail_times=99))]
    statuses = [
        await run_job(job_id, app.state.db.sessionmaker, app.state.storage, stages)
        for _ in range(MAX_ATTEMPTS)
    ]
    assert statuses[-1] == JobStatus.failed
    assert statuses[:-1] == [JobStatus.queued] * (MAX_ATTEMPTS - 1)


async def test_unexpected_crash_is_recorded_not_left_running(app, job_id):
    async def boom(ctx):
        raise RuntimeError("bug")

    await run_job(job_id, app.state.db.sessionmaker, app.state.storage, [("a", boom)])
    job = await load(app, job_id)
    assert job.status in (JobStatus.queued, JobStatus.failed)
    assert job.error == "a: Internal error: RuntimeError"


async def test_completed_job_is_not_rerun(app, job_id):
    calls: list[str] = []
    stages = [("a", counting_stage(calls, "a"))]
    await run_job(job_id, app.state.db.sessionmaker, app.state.storage, stages)
    await run_job(job_id, app.state.db.sessionmaker, app.state.storage, stages)
    assert calls == ["a"]


async def test_verify_file_fails_permanently_when_file_missing(app, job_id):
    async with app.state.db.sessionmaker() as s:
        job = await s.get(ProcessingJob, job_id)
        doc = await s.get(Document, job.document_id)
        app.state.storage.delete(doc.storage_key)
    status = await run_job(
        job_id, app.state.db.sessionmaker, app.state.storage, [("verify_file", stage_verify_file)]
    )
    assert status == JobStatus.failed
    assert (await load(app, job_id)).error == "verify_file: Stored file is missing"


async def test_unfinished_jobs_are_resumed_after_restart(app):
    from doc_intelligence.queue import resume_unfinished_jobs

    async with app.state.db.sessionmaker() as s:
        user = User(email="r@example.com", password_hash="x")
        s.add(user)
        await s.flush()
        col = Collection(owner_id=user.id, name="c")
        s.add(col)
        await s.flush()
        doc = Document(owner_id=user.id, collection_id=col.id, title="notes", original_filename="notes.md",
                       file_format="md", media_type="text/markdown", size_bytes=10, sha256="1" * 64,
                       storage_key=app.state.storage.put(b"# Notes\nThe office moves to Baner on 15 December 2026."))
        s.add(doc)
        await s.flush()
        job = ProcessingJob(document_id=doc.id, owner_id=user.id, status=JobStatus.running)  # interrupted
        s.add(job)
        await s.commit()
    assert await resume_unfinished_jobs(app) == 1
    assert (await load(app, job.id)).status == JobStatus.completed
    assert await resume_unfinished_jobs(app) == 0
