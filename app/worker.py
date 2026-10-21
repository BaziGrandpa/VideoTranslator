from __future__ import annotations

import argparse
import logging
import time

from app.config import settings
from app.db import init_db
from app.pipeline import NoUsableSubtitlesError, Pipeline
from app.repository import Repository
from app.services.renderer import RenderCancelled
from app.services.subtitle_planner import TranslationQuotaError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("video-translator-worker")


def run_worker(once: bool = False, poll_seconds: float = 1.0) -> None:
    settings.ensure_directories()
    init_db()
    repository = Repository()
    recovered = repository.recover_running_jobs()
    if recovered:
        logger.warning("Reconciled %s interrupted job(s)", recovered)
    pipeline = Pipeline(repository)

    logger.info("Worker started")
    while True:
        job = repository.claim_next_job()
        if job is None:
            if once:
                return
            time.sleep(poll_seconds)
            continue
        logger.info("Running %s job %s for project %s", job["kind"], job["id"], job["project_id"])
        try:
            if job["kind"] == "prepare":
                pipeline.prepare(job["project_id"])
            elif job["kind"] == "translate":
                pipeline.translate(job["project_id"])
            elif job["kind"] == "rebuild_subtitles":
                pipeline.rebuild_subtitles(job["project_id"])
            elif job["kind"] == "rebuild_subtitles_skip_pass0":
                pipeline.rebuild_subtitles(job["project_id"], skip_pass0=True)
            elif job["kind"] == "part_rebuild":
                pipeline.part_rebuild(job["project_id"])
            elif job["kind"] == "render":
                pipeline.render(job["project_id"])
            elif job["kind"] == "generate_cards":
                pipeline.generate_cards(job["project_id"])
            elif job["kind"] == "refresh_card_candidates":
                pipeline.refresh_card_candidates(job["project_id"])
            else:
                raise RuntimeError(f"Unknown job kind: {job['kind']}")
            repository.complete_job(job["id"])
        except RenderCancelled:
            logger.info("Render cancelled for project %s", job["project_id"])
            repository.cancel_render_job(job["id"])
        except NoUsableSubtitlesError as exc:
            logger.warning("Project has no usable subtitles: %s", exc)
            repository.fail_job(job["id"], str(exc))
            repository.update_project(
                job["project_id"], status="failed", stage="no_subtitles", error=str(exc)
            )
        except TranslationQuotaError as exc:
            logger.warning("Translation paused for quota: %s", exc)
            if job["kind"] == "part_rebuild":
                repository.fail_interactive_job(job["id"], str(exc))
            elif job["kind"] == "refresh_card_candidates":
                repository.fail_job(job["id"], str(exc))
                repository.update_project(
                    job["project_id"], error=f"Card candidate refresh paused: {exc}"
                )
            else:
                repository.fail_job(job["id"], str(exc))
                repository.update_project(
                    job["project_id"],
                    status="waiting_for_quota",
                    stage="waiting_for_quota",
                    error=str(exc),
                )
        except Exception as exc:
            logger.exception("Job failed")
            error = str(exc)
            if job["kind"] == "generate_cards":
                repository.fail_job(job["id"], error)
                repository.update_project(
                    job["project_id"], error=f"Card generation failed: {error}"
                )
                if once:
                    return
                continue
            if job["kind"] == "refresh_card_candidates":
                repository.fail_job(job["id"], error)
                repository.update_project(
                    job["project_id"], error=f"Card candidate refresh failed: {error}"
                )
                if once:
                    return
                continue
            if job["kind"] in {"render", "part_rebuild"}:
                repository.fail_interactive_job(job["id"], error)
            else:
                repository.fail_job(job["id"], error)
                repository.update_project(
                    job["project_id"], status="failed", stage="failed", error=error
                )
        if once:
            return


def main() -> None:
    parser = argparse.ArgumentParser(description="Video Translator pipeline worker")
    parser.add_argument("--once", action="store_true", help="Run at most one queued job")
    args = parser.parse_args()
    run_worker(once=args.once)


if __name__ == "__main__":
    main()
