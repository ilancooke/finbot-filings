"""Shared validation and provenance for locally stored SEC XBRL inputs."""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


MAX_PACKAGE_MEMBERS = 1_000
MAX_TOTAL_UNCOMPRESSED_BYTES = 1_073_741_824


class XBRLExtractionError(ValueError):
    """Raised when downloaded XBRL inputs fail integrity or layout checks."""


@dataclass(frozen=True, slots=True)
class XBRLSource:
    filing_directory: Path
    package_path: Path
    instance_path: Path
    filing_metadata: dict[str, Any]
    acquisition_metadata: dict[str, Any]
    package_members: tuple[str, ...]
    package_sha256: str
    instance_sha256: str
    instance_bytes: bytes


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise XBRLExtractionError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise XBRLExtractionError(f"expected a JSON object in {path}")
    return value


def safe_package_members(package_path: Path) -> tuple[str, ...]:
    try:
        with zipfile.ZipFile(package_path) as archive:
            members = archive.infolist()
            names = [member.filename for member in members]
            if len(members) > MAX_PACKAGE_MEMBERS:
                raise XBRLExtractionError(
                    f"XBRL package exceeds {MAX_PACKAGE_MEMBERS} members"
                )
            uncompressed_bytes = sum(member.file_size for member in members)
            if uncompressed_bytes > MAX_TOTAL_UNCOMPRESSED_BYTES:
                raise XBRLExtractionError(
                    "XBRL package exceeds the uncompressed-size limit"
                )
            if len(names) != len(set(names)):
                raise XBRLExtractionError(
                    "XBRL package contains duplicate member names"
                )
            for name in names:
                member_path = PurePosixPath(name)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise XBRLExtractionError(f"unsafe archive member {name!r}")
            corrupt_member = archive.testzip()
            if corrupt_member is not None:
                raise XBRLExtractionError(
                    f"package contains corrupt member {corrupt_member!r}"
                )
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise XBRLExtractionError(f"invalid XBRL package {package_path}") from exc
    return tuple(names)


def inventory_xbrl_source(filing_directory: Path) -> XBRLSource:
    """Validate one raw package and its separately downloaded instance."""
    filing_metadata = read_json_object(filing_directory / "metadata.json")
    xbrl_directory = filing_directory / "xbrl"
    acquisition_metadata = read_json_object(xbrl_directory / "metadata.json")
    package_path = xbrl_directory / "package.zip"
    instance_path = xbrl_directory / "instance.xml"
    try:
        package_bytes = package_path.read_bytes()
        instance_bytes = instance_path.read_bytes()
    except OSError as exc:
        raise XBRLExtractionError(
            "XBRL package is incomplete; rerun download-xbrl to fetch instance.xml"
        ) from exc
    package_sha256 = sha256_bytes(package_bytes)
    instance_sha256 = sha256_bytes(instance_bytes)
    if package_sha256 != acquisition_metadata.get("sha256"):
        raise XBRLExtractionError("package.zip checksum does not match metadata")
    if instance_sha256 != acquisition_metadata.get("instance_sha256"):
        raise XBRLExtractionError("instance.xml checksum does not match metadata")
    return XBRLSource(
        filing_directory=filing_directory,
        package_path=package_path,
        instance_path=instance_path,
        filing_metadata=filing_metadata,
        acquisition_metadata=acquisition_metadata,
        package_members=safe_package_members(package_path),
        package_sha256=package_sha256,
        instance_sha256=instance_sha256,
        instance_bytes=instance_bytes,
    )
