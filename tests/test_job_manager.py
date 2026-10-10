from src.job_manager import JobManager


def test_second_job_is_queued_then_promoted():
    mgr = JobManager()
    first = mgr.create_job(["a"], ["soundcloud"])
    second = mgr.create_job(["b"], ["soundcloud"])
    assert (first.status, second.status) == ("running", "queued")
    assert mgr.queue_position(second.job_id) == 1
    assert mgr.start_next() is None             # still busy

    mgr.complete_job(0)
    nxt = mgr.start_next()
    assert nxt is not None and nxt.job_id == second.job_id and nxt.status == "running"


def test_cancelling_a_queued_job_removes_it():
    mgr = JobManager()
    mgr.create_job(["a"], ["soundcloud"])
    queued = mgr.create_job(["b"], ["soundcloud"])
    assert mgr.cancel_job(queued.job_id)
    assert queued.status == "cancelled" and mgr.queued() == []


def test_cancelling_the_running_job_keeps_it_current_until_it_stops():
    mgr = JobManager()
    job = mgr.create_job(["a"], ["soundcloud"])
    mgr.cancel_job(job.job_id)
    assert job.status == "cancelling" and mgr.is_running()
    mgr.complete_job(3, {"done": 1, "pending": 2, "total": 3})
    assert job.status == "cancelled" and not mgr.is_running()
    assert (job.done, job.pending, job.missing_tracks) == (1, 2, 2)


def test_partial_status_and_history_reload(tmp_path):
    mgr = JobManager()
    mgr.set_history_path(tmp_path / "jobs.json")
    job = mgr.create_job(["a"], ["spotify"])
    mgr.complete_job(2, {"done": 5, "failed": 1, "total": 6})
    assert job.status == "partial"

    JobManager._instance = None
    reloaded = JobManager()
    reloaded.set_history_path(tmp_path / "jobs.json")
    [saved] = reloaded.get_jobs()
    assert saved["status"] == "partial" and saved["failed"] == 1


def test_jobs_left_running_by_a_dead_server_load_as_cancelled(tmp_path):
    mgr = JobManager()
    mgr.set_history_path(tmp_path / "jobs.json")
    mgr.create_job(["a"], ["spotify"])
    mgr._save_history()

    JobManager._instance = None
    reloaded = JobManager()
    reloaded.set_history_path(tmp_path / "jobs.json")
    assert reloaded.get_jobs()[0]["status"] == "cancelled"
