"""SQLAlchemy tables of modulehub. Prefix `mh_`; foreign keys only point to other `mh_` tables."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text

from app.db.database import Base


class MhRelease(Base):
    __tablename__ = "mh_release"

    id = Column(Integer, primary_key=True, autoincrement=True)
    module = Column(String(64), nullable=False, index=True)
    repo = Column(String(128), nullable=False, default="")   # module repo, from modules.versions.toml
    platforms = Column(String(32), nullable=False)        # "android,ios" | "android" | "ios"
    branch = Column(String(128), nullable=False)
    major = Column(Boolean, default=False)
    kind = Column(String(16), default="release")           # release | preview
    state = Column(String(24), default="pending", index=True)
    failed_from = Column(String(24), default="")
    version = Column(String(32), default="")
    git_sha = Column(String(40), default="")
    artifacts_json = Column(Text, default="{}")            # platform -> {coordinate, sha256, apiChanges}
    previous_versions_json = Column(Text, default="{}")    # platform -> version the shell pinned before
    bump_prs_json = Column(Text, default="{}")             # platform -> bump PR url
    build_ref = Column(String(512), default="")
    build_url = Column(String(512), default="")
    backport_pr_url = Column(String(512), default="")
    error = Column(Text, default="")
    requested_by = Column(String(128), default="")
    resume_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow)



class MhReleaseEvent(Base):
    __tablename__ = "mh_release_event"

    id = Column(Integer, primary_key=True, autoincrement=True)
    release_id = Column(Integer, ForeignKey("mh_release.id"), nullable=False, index=True)
    at = Column(DateTime, default=datetime.utcnow)
    state = Column(String(24), default="")
    message = Column(Text, default="")


class MhMirrorLog(Base):
    __tablename__ = "mh_mirror_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    module = Column(String(64), nullable=False)
    branch = Column(String(128), nullable=False)
    action = Column(String(16), default="")      # create | verify | no_pin
    outcome = Column(String(256), default="")
    at = Column(DateTime, default=datetime.utcnow, index=True)
