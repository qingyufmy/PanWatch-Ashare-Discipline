"""Read-only upstream change inventory. It never installs or promotes updates."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from urllib.request import Request, urlopen

from sqlalchemy.orm import Session

from src.platform.persistence.models import AIModel, ModelRun

REPOS = ("TNT-Likely/PanWatch", "TauricResearch/TradingAgents")
PACKAGES = ("fastapi", "sqlalchemy", "pydantic", "apscheduler", "httpx")


def _github_head(repo: str) -> dict:
    request = Request(f"https://api.github.com/repos/{repo}/commits?per_page=1",
                      headers={"Accept": "application/vnd.github+json",
                               "User-Agent": "PanWatch-portfolio-upstream-watch"})
    with urlopen(request, timeout=8) as response:
        rows = json.load(response)
    if not rows:
        raise ValueError("upstream_commit_missing")
    return {"repo": repo, "sha": rows[0]["sha"], "url": rows[0]["html_url"]}


def collect(db: Session, *, fetch_head=_github_head, previous: dict | None = None) -> dict:
    repositories = []
    for repo in REPOS:
        try:
            repositories.append({**fetch_head(repo), "status": "OK"})
        except Exception as exc:
            repositories.append({"repo": repo, "status": "UNAVAILABLE",
                                 "error": type(exc).__name__})
    dependencies = {}
    for package in PACKAGES:
        try:
            dependencies[package] = version(package)
        except PackageNotFoundError:
            dependencies[package] = None
    configured = [{"id": m.id, "model": m.model} for m in db.query(AIModel).order_by(AIModel.id)]
    observed = (db.query(ModelRun).filter(ModelRun.reported_model.isnot(None))
                .order_by(ModelRun.started_at.desc()).first())
    report = {"checked_at": datetime.now(timezone.utc).isoformat(),
              "repositories": repositories, "dependencies": dependencies,
              "configured_models": configured,
              "last_reported_model": observed.reported_model if observed else None,
              "action": "REVIEW_ONLY_NO_UPGRADE"}
    previous_heads = {r["repo"]: r.get("sha") for r in (previous or {}).get("repositories", [])}
    report["changed_repositories"] = [r["repo"] for r in repositories
                                      if r.get("sha") and previous_heads.get(r["repo"])
                                      and r["sha"] != previous_heads[r["repo"]]]
    return report
