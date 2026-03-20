"""
Scheduled code repository updater.

Runs daily between 2:00-6:00 AM to pull the latest main branch
for all configured code repositories (app, web, desktop).
"""

from __future__ import annotations

import asyncio
import logging
import random
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict

from app.config import get_all_code_repos

logger = logging.getLogger("jarvis.repo_updater")


def _update_repo(name: str, repo_path: str) -> bool:
    """Git fetch + checkout main + pull for a single repository."""
    path = Path(repo_path)
    if not path.exists():
        logger.warning("[repo:%s] Path does not exist: %s", name, repo_path)
        return False

    git_dir = path / ".git"
    if not git_dir.exists():
        logger.warning("[repo:%s] Not a git repository: %s", name, repo_path)
        return False

    try:
        # Fetch latest
        subprocess.run(
            ["git", "fetch", "origin"],
            cwd=str(path), capture_output=True, text=True, timeout=120,
        )

        # Checkout main (try main, then master)
        result = subprocess.run(
            ["git", "checkout", "main"],
            cwd=str(path), capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            result = subprocess.run(
                ["git", "checkout", "master"],
                cwd=str(path), capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                logger.warning("[repo:%s] Failed to checkout main/master: %s", name, result.stderr.strip())
                return False

        # Pull
        result = subprocess.run(
            ["git", "pull", "origin"],
            cwd=str(path), capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            logger.warning("[repo:%s] git pull failed: %s", name, result.stderr.strip())
            return False

        logger.info("[repo:%s] Updated successfully at %s", name, repo_path)
        return True

    except subprocess.TimeoutExpired:
        logger.warning("[repo:%s] git operation timed out", name)
        return False
    except Exception as e:
        logger.error("[repo:%s] Update failed: %s", name, e)
        return False


def update_all_repos() -> Dict[str, bool]:
    """Update all configured code repositories. Returns {name: success}."""
    repos = get_all_code_repos()
    if not repos:
        logger.debug("No code repositories configured, skipping update")
        return {}

    results = {}
    for name, path in repos.items():
        results[name] = _update_repo(name, path)

    succeeded = sum(1 for v in results.values() if v)
    logger.info(
        "Repository update complete: %d/%d succeeded (%s)",
        succeeded, len(results),
        ", ".join(f"{k}={'ok' if v else 'FAIL'}" for k, v in results.items()),
    )
    return results


async def repo_update_loop():
    """Background loop: update repos once daily between 2:00-6:00 AM.

    On first run, waits until the next 2:00 AM window. Then runs once per day.
    A random jitter (0-30 min) is added so multiple instances don't hit git
    servers simultaneously.
    """
    while True:
        try:
            now = datetime.now()
            # Calculate seconds until next 2:00 AM
            target_hour = 2
            if now.hour >= target_hour and now.hour < 6:
                # We're in the window — run soon (with small jitter)
                wait_seconds = random.randint(0, 60)
            elif now.hour >= 6:
                # Past today's window — wait until tomorrow 2:00 AM
                tomorrow_2am = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
                tomorrow_2am = tomorrow_2am.replace(day=now.day + 1) if now.hour >= target_hour else tomorrow_2am
                from datetime import timedelta
                tomorrow_2am = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1, hours=target_hour)
                wait_seconds = (tomorrow_2am - now).total_seconds()
            else:
                # Before 2:00 AM today — wait until 2:00 AM
                target = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
                wait_seconds = (target - now).total_seconds()

            # Add jitter (0-30 min)
            wait_seconds += random.randint(0, 1800)
            logger.info(
                "Next repo update in %.1f hours (at ~%s)",
                wait_seconds / 3600,
                (now.replace(second=0, microsecond=0).__class__.fromtimestamp(
                    now.timestamp() + wait_seconds
                )).strftime("%H:%M"),
            )

            await asyncio.sleep(wait_seconds)

            # Run the update in a thread pool to avoid blocking the event loop
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, update_all_repos)

            # After updating, sleep until tomorrow (at least 20 hours)
            await asyncio.sleep(20 * 3600)

        except asyncio.CancelledError:
            logger.info("Repo update loop cancelled")
            return
        except Exception as e:
            logger.error("Repo update loop error (will retry in 1h): %s", e)
            await asyncio.sleep(3600)
