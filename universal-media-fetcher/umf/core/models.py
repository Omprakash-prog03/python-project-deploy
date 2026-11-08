"""Value objects shared by every adapter and by the API layer.

These are the contract. An adapter may return less; it may not return something
shaped differently, because the capability engine and the UI both depend on
being able to compare like with like across 21 platforms.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class PlatformCategory(str, Enum):
    VIDEO_HOSTING = "video_hosting"
    SOCIAL = "social"
    OTT = "ott"
    DIRECT_MEDIA = "direct_media"


class UrlKind(str, Enum):
    """Whether the URL addresses content, or a file."""

    SHARING = "sharing"
    DIRECT = "direct"
    EMBED = "embed"
    UNKNOWN = "unknown"


class MediaKind(str, Enum):
    VIDEO = "video"
    AUDIO = "audio"
    PLAYLIST = "playlist"     # HLS
    MANIFEST = "manifest"     # MPEG-DASH
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ContentRef:
    """A identified piece of content: what to ask an adapter about."""

    platform: str
    url: str
    content_id: str | None = None
    url_kind: UrlKind = UrlKind.UNKNOWN
    media_kind: MediaKind = MediaKind.UNKNOWN
    confidence: float = 1.0  # <1 when identification came from URL shape alone


@dataclass(frozen=True, slots=True)
class Capabilities:
    """The §5 capability profile.

    Booleans are the API contract; ``reasons`` and ``notes`` carry the honesty.
    ``available`` is deliberately absent: a capability being *possible* and it
    being *possible for this URL* are different questions, and the engine
    returns both.
    """

    metadata: bool = False
    preview: bool = False
    official_playback: bool = False
    authorized_download: bool = False
    subtitle_access: bool = False
    authentication_required: bool = False
    drm_protected: bool = False
    #: Why an operation is unavailable, keyed by capability name.
    reasons: dict[str, str] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "metadata": self.metadata,
            "preview": self.preview,
            "official_playback": self.official_playback,
            "authorized_download": self.authorized_download,
            "subtitle_access": self.subtitle_access,
            "authentication_required": self.authentication_required,
            "drm_protected": self.drm_protected,
            "reasons": dict(self.reasons),
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class DownloadOption:
    """One downloadable rendition, with real numbers rather than guesses."""

    format: str                  # mp4, webm, mkv, mov, mp3
    url: str
    resolution: str | None = None
    size_bytes: int | None = None
    bitrate_kbps: int | None = None
    codec: str | None = None
    #: True only when the server advertises Accept-Ranges: bytes.
    resumable: bool = False
    label: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass(frozen=True, slots=True)
class SubtitleTrack:
    language: str
    label: str
    url: str | None = None
    format: str = "vtt"          # vtt | srt
    auto_generated: bool = False


@dataclass(frozen=True, slots=True)
class Metadata:
    """Content information. Every field is optional because few platforms give
    all of it, and inventing a value is worse than returning ``None``."""

    title: str | None = None
    description: str | None = None
    thumbnail: str | None = None
    creator: str | None = None
    duration_seconds: float | None = None
    release_year: int | None = None
    category: str | None = None
    languages: tuple[str, ...] = ()
    source_url: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "description": self.description,
            "thumbnail": self.thumbnail,
            "creator": self.creator,
            "duration_seconds": self.duration_seconds,
            "release_year": self.release_year,
            "category": self.category,
            "languages": list(self.languages),
            "source_url": self.source_url,
            "extra": dict(self.extra),
        }
