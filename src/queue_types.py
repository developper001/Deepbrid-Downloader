from __future__ import annotations

from enum import Enum


class QueueSource(str, Enum):
    PREMIUM_LINK = "premium_link"
    USENET = "usenet"
    TORRENT_CLOUD = "torrent_cloud"


class QueueStatus:
    QUEUED = "queued"
    GENERATING = "generating"
    RETRYING = "retrying"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"
    BLOCKED = "blocked"

    ALL = frozenset(
        {
            QUEUED,
            GENERATING,
            RETRYING,
            DOWNLOADING,
            COMPLETED,
            SKIPPED,
            FAILED,
            BLOCKED,
        }
    )
    INTERRUPTED = frozenset({GENERATING, RETRYING, DOWNLOADING})
    ACTIVE = frozenset({QUEUED, GENERATING, RETRYING, DOWNLOADING})
    SETTLED = frozenset({COMPLETED, SKIPPED, FAILED, BLOCKED})
