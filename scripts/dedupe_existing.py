"""
One-off: consolidate pre-existing duplicate Score + Application rows so they
match the new cluster-replication invariant:

  - Every Job in a cluster (>1 siblings) ends up with one Score per sibling,
    all carrying the canonical's content.
  - Every Application in a cluster points at a single shared materials_path;
    orphaned materials directories on disk are deleted.

Safe to re-run. Backs up the DB first.

Usage:
  .venv/bin/python scripts/dedupe_existing.py            # dry-run by default
  .venv/bin/python scripts/dedupe_existing.py --apply    # actually mutate
"""
import argparse
import shutil
from datetime import datetime
from pathlib import Path

from sqlmodel import select

from job_agent import clustering
from job_agent.config import load_profile
from job_agent.db import init_db, get_session, Job, Score, Application


def _replicate_score(original: Score, target_job_id: str) -> Score:
    return Score(
        job_id=target_job_id,
        user_id=original.user_id,
        tier=original.tier,
        skill_fit=original.skill_fit,
        preference_alignment=original.preference_alignment,
        recommended_resume_version=original.recommended_resume_version,
        gap_assessment=original.gap_assessment,
        positioning_notes=original.positioning_notes,
        rationale=original.rationale,
        raw_response=original.raw_response,
        scored_at=original.scored_at,
    )


def main(apply: bool) -> None:
    profile = load_profile("profile/theo.yaml")
    init_db()

    # Backup DB before any mutation
    if apply:
        db_path = Path("data/jobs.sqlite")
        stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        backup = db_path.with_name(f"jobs.sqlite.bak-pre-dedupe-{stamp}")
        shutil.copy2(db_path, backup)
        print(f"DB backup → {backup}")

    stats = {
        "clusters_inspected": 0,
        "clusters_with_siblings": 0,
        "scores_deleted": 0,
        "scores_inserted": 0,
        "apps_deleted": 0,
        "apps_inserted": 0,
        "dirs_deleted": 0,
        "clusters_skipped_no_score": 0,
        "clusters_skipped_no_app": 0,
    }

    with get_session() as s:
        jobs = s.exec(select(Job)).all()
        groups = clustering.group_by_cluster(jobs)
        stats["clusters_inspected"] = len(groups)

        for key, cluster_jobs in groups.items():
            if len(cluster_jobs) < 2:
                continue
            stats["clusters_with_siblings"] += 1
            canonical = clustering.pick_canonical(cluster_jobs, profile)
            sibling_ids = [j.id for j in cluster_jobs]

            # -- Score consolidation -------------------------------------------------
            cluster_scores = s.exec(
                select(Score).where(Score.job_id.in_(sibling_ids)).order_by(Score.scored_at.desc())
            ).all()

            best_score = None
            # Prefer canonical's most-recent score
            for sc in cluster_scores:
                if sc.job_id == canonical.id:
                    best_score = sc
                    break
            # Fall back to the most recent sibling score
            if best_score is None and cluster_scores:
                best_score = cluster_scores[0]

            if best_score is None:
                stats["clusters_skipped_no_score"] += 1
            else:
                # Delete every existing score in this cluster
                for sc in cluster_scores:
                    if apply:
                        s.delete(sc)
                    stats["scores_deleted"] += 1
                # Insert one replicated score per sibling job_id
                for jid in sibling_ids:
                    new_sc = _replicate_score(best_score, jid)
                    if apply:
                        s.add(new_sc)
                    stats["scores_inserted"] += 1

            # -- Application consolidation -----------------------------------------
            cluster_apps = s.exec(
                select(Application).where(Application.job_id.in_(sibling_ids))
            ).all()

            best_path = None
            # Prefer canonical's app (whose dir exists)
            for app in cluster_apps:
                if app.job_id == canonical.id and Path(app.materials_path).exists():
                    best_path = app.materials_path
                    user_id = app.user_id
                    break
            # Fall back: any sibling's app whose dir exists
            if best_path is None:
                for app in cluster_apps:
                    if Path(app.materials_path).exists():
                        best_path = app.materials_path
                        user_id = app.user_id
                        break

            if best_path is None:
                stats["clusters_skipped_no_app"] += 1
                continue

            # Collect dirs to delete (every sibling's app dir that isn't the kept one)
            dirs_to_delete: set[Path] = set()
            for app in cluster_apps:
                if app.materials_path != best_path:
                    p = Path(app.materials_path)
                    if p.exists():
                        dirs_to_delete.add(p)
                if apply:
                    s.delete(app)
                stats["apps_deleted"] += 1

            # Insert one Application per sibling, all pointing at best_path
            for jid in sibling_ids:
                new_app = Application(job_id=jid, user_id=user_id, materials_path=best_path)
                if apply:
                    s.add(new_app)
                stats["apps_inserted"] += 1

            # Delete orphaned directories on disk
            for d in dirs_to_delete:
                if apply:
                    try:
                        shutil.rmtree(d)
                    except OSError as e:
                        print(f"  WARN couldn't delete {d}: {e}")
                stats["dirs_deleted"] += 1

        if apply:
            s.commit()

    print()
    print(f"{'APPLIED' if apply else 'DRY-RUN'} summary:")
    for k, v in stats.items():
        print(f"  {k:<30} {v}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Actually mutate the DB and disk (default: dry-run)")
    args = parser.parse_args()
    main(apply=args.apply)
