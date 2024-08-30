#
# Copyright (c) 2023 Airbyte, Inc., all rights reserved.
#

import json
import logging
import os
import re
import tempfile
import git
import requests
import yaml

from dataclasses import dataclass
from pathlib import Path
from typing import List, Literal, Optional, Tuple, NamedTuple
from metadata_service.helpers.files import compute_gcs_md5, compute_sha256, create_zip_and_get_sha256
from pydash import set_
from pydash.objects import get
from google.cloud import storage
from google.oauth2 import service_account

from metadata_service.constants import (
    DOC_FILE_NAME,
    DOC_INAPP_FILE_NAME,
    ICON_FILE_NAME,
    LATEST_GCS_FOLDER_NAME,
    METADATA_FILE_NAME,
    METADATA_FOLDER,
    RELEASE_CANDIDATE_GCS_FOLDER_NAME,
    COMPONENTS_PY_FILE_NAME,
    MANIFEST_FILE_NAME,
)
from metadata_service.models.generated.ConnectorMetadataDefinitionV0 import ConnectorMetadataDefinitionV0
from metadata_service.models.generated.GitInfo import GitInfo
from metadata_service.models.transform import to_json_sanitized_dict
from metadata_service.validators.metadata_validator import POST_UPLOAD_VALIDATORS, ValidatorOptions, validate_and_load

# 🧩 TYPES

@dataclass(frozen=True)
class UploadedFile:
    id: str
    uploaded: bool
    description: str
    blob_id: Optional[str]


@dataclass(frozen=True)
class MetadataUploadInfo:
    metadata_uploaded: bool
    metadata_file_path: str
    uploaded_files: List[UploadedFile]


class MaybeUpload(NamedTuple):
    uploaded: bool
    blob_id: str

@dataclass
class ManifestOnlyFilePaths:
    zip_file_path: Path
    sha256_file_path: Path
    sha256: str | None
    manifest_file_path: Path

# 🛣️ PATHS

def get_metadata_remote_file_path(dockerRepository: str, version: str) -> str:
    """Get the path to the metadata file for a specific version of a connector.

    Args:
        dockerRepository (str): Name of the connector docker image.
        version (str): Version of the connector.
    Returns:
        str: Path to the metadata file.
    """
    return f"{METADATA_FOLDER}/{dockerRepository}/{version}/{METADATA_FILE_NAME}"


def get_icon_remote_file_path(dockerRepository: str, version: str) -> str:
    """Get the path to the icon file for a specific version of a connector.

    Args:
        dockerRepository (str): Name of the connector docker image.
        version (str): Version of the connector.
    Returns:
        str: Path to the icon file.
    """
    return f"{METADATA_FOLDER}/{dockerRepository}/{version}/{ICON_FILE_NAME}"


def get_doc_remote_file_path(dockerRepository: str, version: str, inapp: bool) -> str:
    """Get the path to the icon file for a specific version of a connector.

    Args:
        dockerRepository (str): Name of the connector docker image.
        version (str): Version of the connector.
    Returns:
        str: Path to the icon file.
    """
    return f"{METADATA_FOLDER}/{dockerRepository}/{version}/{DOC_INAPP_FILE_NAME if inapp else DOC_FILE_NAME}"


def get_doc_local_file_path(metadata: ConnectorMetadataDefinitionV0, docs_path: Path, inapp: bool) -> Path:
    pattern = re.compile(r"^https://docs\.airbyte\.com/(.+)$")
    match = pattern.search(metadata.data.documentationUrl)
    if match:
        extension = ".inapp.md" if inapp else ".md"
        return (docs_path / match.group(1)).with_suffix(extension)
    return None

# 🛠️ HELPERS

def _commit_to_git_info(commit: git.Commit) -> GitInfo:
    return GitInfo(
        commit_sha=commit.hexsha,
        commit_timestamp=commit.authored_datetime,
        commit_author=commit.author.name,
        commit_author_email=commit.author.email,
    )


def _get_git_info_for_file(original_metadata_file_path: Path) -> Optional[GitInfo]:
    """
    Add additional information to the metadata file before uploading it to GCS.

    e.g. The git commit hash, the date of the commit, the author of the commit, etc.

    """
    try:
        repo = git.Repo(search_parent_directories=True)

        # get the commit hash for the last commit that modified the metadata file
        commit_sha = repo.git.log("-1", "--format=%H", str(original_metadata_file_path))

        commit = repo.commit(commit_sha)
        return _commit_to_git_info(commit)
    except git.exc.InvalidGitRepositoryError:
        logging.warning(f"Metadata file {original_metadata_file_path} is not in a git repository, skipping author info attachment.")
        return None
    except git.exc.GitCommandError as e:
        if "unknown revision or path not in the working tree" in str(e):
            logging.warning(f"Metadata file {original_metadata_file_path} is not tracked by git, skipping author info attachment.")
            return None
        else:
            raise e





def _safe_load_metadata_file(metadata_file_path: Path) -> dict:
    try:
        metadata = yaml.safe_load(metadata_file_path.read_text())
        if metadata is None or not isinstance(metadata, dict):
            raise ValueError(f"Validation error: Metadata file {metadata_file_path} is invalid yaml.")
        return metadata
    except Exception as e:
        raise ValueError(f"Validation error: Metadata file {metadata_file_path} is invalid yaml: {e}")

# 🚀 UPLOAD

def _save_blob_to_gcs(blob_to_save: storage.blob.Blob, file_path: str, disable_cache: bool = False) -> bool:
    """Uploads a file to the bucket."""
    print(f"Uploading {file_path} to {blob_to_save.name}...")

    # Set Cache-Control header to no-cache to avoid caching issues
    # This is IMPORTANT because if we don't set this header, the metadata file will be cached by GCS
    # and the next time we try to download it, we will get the stale version
    if disable_cache:
        blob_to_save.cache_control = "no-cache"

    blob_to_save.upload_from_filename(file_path)

    return True


def upload_file_if_changed(
    local_file_path: Path, bucket: storage.bucket.Bucket, blob_path: str, disable_cache: bool = False
) -> MaybeUpload:
    local_file_md5_hash = compute_gcs_md5(local_file_path)
    remote_blob = bucket.blob(blob_path)

    # reload the blob to get the md5_hash
    if remote_blob.exists():
        remote_blob.reload()

    remote_blob_md5_hash = remote_blob.md5_hash if remote_blob.exists() else None

    print(f"Local {local_file_path} md5_hash: {local_file_md5_hash}")
    print(f"Remote {blob_path} md5_hash: {remote_blob_md5_hash}")

    if local_file_md5_hash != remote_blob_md5_hash:
        uploaded = _save_blob_to_gcs(remote_blob, local_file_path, disable_cache=disable_cache)
        return MaybeUpload(uploaded, remote_blob.id)

    return MaybeUpload(False, remote_blob.id)


def _metadata_upload(
    metadata: ConnectorMetadataDefinitionV0, bucket: storage.bucket.Bucket, metadata_file_path: Path, version: str
) -> MaybeUpload:
    latest_path = get_metadata_remote_file_path(metadata.data.dockerRepository, version)
    return upload_file_if_changed(metadata_file_path, bucket, latest_path, disable_cache=True)


def _icon_upload(metadata: ConnectorMetadataDefinitionV0, bucket: storage.bucket.Bucket, icon_file_path: Path) -> MaybeUpload:
    latest_icon_path = get_icon_remote_file_path(metadata.data.dockerRepository, LATEST_GCS_FOLDER_NAME)
    if not icon_file_path.exists():
        return MaybeUpload(False, f"No Icon found at {icon_file_path}")
    return upload_file_if_changed(icon_file_path, bucket, latest_icon_path)


def _doc_upload(
    metadata: ConnectorMetadataDefinitionV0, bucket: storage.bucket.Bucket, docs_path: Path, latest: bool, inapp: bool
) -> MaybeUpload:
    local_doc_path = get_doc_local_file_path(metadata, docs_path, inapp)
    if not local_doc_path:
        return MaybeUpload(False, f"Metadata does not contain a valid Airbyte documentation url, skipping doc upload.")

    remote_doc_path = get_doc_remote_file_path(
        metadata.data.dockerRepository, LATEST_GCS_FOLDER_NAME if latest else metadata.data.dockerImageTag, inapp
    )

    if local_doc_path.exists():
        doc_uploaded, doc_blob_id = upload_file_if_changed(local_doc_path, bucket, remote_doc_path)
    else:
        if inapp:
            doc_uploaded, doc_blob_id = False, f"No inapp doc found at {local_doc_path}, skipping inapp doc upload."
        else:
            raise FileNotFoundError(f"Expected to find connector doc file at {local_doc_path}, but none was found.")

    return MaybeUpload(doc_uploaded, doc_blob_id)

def _file_upload(
    local_path: Path,
    gcp_connector_dir: str,
    bucket: storage.bucket.Bucket,
    *,
    upload_as_version: str | Literal[False],
    upload_as_latest: bool,
    skip_if_not_exists: bool = True,
) -> tuple[MaybeUpload, MaybeUpload]:
    """Upload a file to GCS.

    Optionally upload it as a versioned file and/or as the latest version.

    Args:
        local_path: Path to the file to upload.
        gcp_connector_dir: Path to the connector folder in GCS. This is the parent folder,
            containing the versioned and "latest" folders as its subdirectories.
        bucket: GCS bucket to upload the file to.
        upload_as_version: The version to upload the file as or 'False' to skip uploading
            the versioned copy.
        upload_as_latest: Whether to upload the file as the latest version.
        skip_if_not_exists: Whether to skip the upload if the file does not exist. Otherwise,
            an exception will be raised if the file does not exist.

    Returns: Tuple of two MaybeUpload objects, each containing a boolean indicating whether the file was
        uploaded and the blob id. The first tuple is for the versioned file, the second for the
        latest file.
    """
    if not local_path.exists():
        if skip_if_not_exists:
            return MaybeUpload(False, None), MaybeUpload(False, None)

        msg = f"Expected to find file at {local_path}, but none was found."
        raise FileNotFoundError(msg)

    versioned_file_uploaded, latest_file_uploaded = False, False
    versioned_blob_id, latest_blob_id = None, None
    if upload_as_version:
        remote_upload_path = gcp_connector_dir / upload_as_version
        versioned_file_uploaded, versioned_blob_id = upload_file_if_changed(
            local_file_path=local_path,
            bucket=bucket,
            blob_path=remote_upload_path / local_path.name,
        )

    if upload_as_latest:
        remote_upload_path = gcp_connector_dir / LATEST_GCS_FOLDER_NAME
        versioned_file_uploaded, versioned_blob_id = upload_file_if_changed(
            local_file_path=local_path,
            bucket=bucket,
            blob_path=remote_upload_path / local_path.name,
        )

    return MaybeUpload(versioned_file_uploaded, versioned_blob_id), MaybeUpload(latest_file_uploaded, latest_blob_id)

# 🔧 METADATA MODIFICATIONS

def _apply_prerelease_overrides(metadata_dict: dict, validator_opts: ValidatorOptions) -> dict:
    """Apply any prerelease overrides to the metadata file before uploading it to GCS."""
    if validator_opts.prerelease_tag is None:
        return metadata_dict

    # replace any dockerImageTag references with the actual tag
    # this includes metadata.data.dockerImageTag, metadata.data.registryOverrides[].dockerImageTag
    # where registries is a dictionary of registry name to registry object
    metadata_dict["data"]["dockerImageTag"] = validator_opts.prerelease_tag
    for registry in get(metadata_dict, "data.registryOverrides", {}).values():
        if "dockerImageTag" in registry:
            registry["dockerImageTag"] = validator_opts.prerelease_tag

    return metadata_dict


def _apply_author_info_to_metadata_file(metadata_dict: dict, original_metadata_file_path: Path) -> dict:
    """Apply author info to the metadata file before uploading it to GCS."""
    git_info = _get_git_info_for_file(original_metadata_file_path)
    if git_info:
        # Apply to the nested / optional field at metadata.data.generated.git
        git_info_dict = to_json_sanitized_dict(git_info, exclude_none=True)
        metadata_dict = set_(metadata_dict, "data.generated.git", git_info_dict)
    return metadata_dict


def _apply_python_components_sha_to_metadata_file(
    metadata_dict: dict,
    python_components_sha256: Optional[Path] | None = None,
) -> dict:
    """If a `components.py` file is required, store the necessary information in the metadata.

    This adds a `required=True` flag and the sha256 hash of the `python_components.zip` file.

    This is a no-op if `python_components_sha256` is not provided.
    """
    if python_components_sha256:
        metadata_dict = set_(metadata_dict, "data.generated.pythonComponents.required", True)
        metadata_dict = set_(
            metadata_dict,
            "data.generated.pythonComponents.sha256",
            python_components_sha256,
        )

    return metadata_dict


def _apply_sbom_url_to_metadata_file(metadata_dict: dict) -> dict:
    """Apply sbom url to the metadata file before uploading it to GCS."""
    try:
        sbom_url = f"https://connectors.airbyte.com/files/sbom/{metadata_dict['data']['dockerRepository']}/{metadata_dict['data']['dockerImageTag']}.spdx.json"
    except KeyError:
        return metadata_dict
    response = requests.head(sbom_url)
    if response.ok:
        metadata_dict = set_(metadata_dict, "data.generated.sbomUrl", sbom_url)
    return metadata_dict


def _apply_modifications_to_metadata_file(
    original_metadata_file_path: Path,
    validator_opts: ValidatorOptions,
    components_zip_sha256: str | None = None,
) -> Path:
    """Apply modifications to the metadata file before uploading it to GCS.

    e.g. The git commit hash, the date of the commit, the author of the commit, etc.

    Args:
        original_metadata_file_path (Path): Path to the original metadata file.
        validator_opts (ValidatorOptions): Options to use when validating the metadata file.
        components_zip_sha256 (str): The sha256 hash of the `python_components.zip` file. This is
            required if the `python_components.zip` file is present.
    """
    metadata = _safe_load_metadata_file(original_metadata_file_path)
    metadata = _apply_prerelease_overrides(metadata, validator_opts)
    metadata = _apply_author_info_to_metadata_file(metadata, original_metadata_file_path)
    metadata = _apply_python_components_sha_to_metadata_file(metadata, components_zip_sha256)

    metadata = _apply_sbom_url_to_metadata_file(metadata)
    return _write_metadata_to_tmp_file(metadata)

def _write_metadata_to_tmp_file(metadata_dict: dict) -> Path:
    """Write the metadata to a temporary file."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as tmp_file:
        yaml.dump(metadata_dict, tmp_file)
        return Path(tmp_file.name)


def maybe_create_components_zip(working_directory: Path) -> ManifestOnlyFilePaths:
    """Create a zip file for components if they exist and return its SHA256 hash."""
    yaml_manifest_file_path = working_directory / MANIFEST_FILE_NAME
    components_py_file_path = working_directory / COMPONENTS_PY_FILE_NAME

    with tempfile.NamedTemporaryFile(mode="wb", suffix=".zip", delete=False) as zip_tmp_file, \
         tempfile.NamedTemporaryFile(mode="w", suffix=".sha256", delete=False) as sha256_tmp_file:

        python_components_zip_file_path = Path(zip_tmp_file.name)
        python_components_zip_sha256_file_path = Path(sha256_tmp_file.name)

        components_zip_sha256 = None
        if components_py_file_path.exists():
            files_to_zip: List[Path] = [components_py_file_path, yaml_manifest_file_path]
            components_zip_sha256 = create_zip_and_get_sha256(files_to_zip, python_components_zip_file_path)
            sha256_tmp_file.write(components_zip_sha256)

        return ManifestOnlyFilePaths(
            zip_file_path=python_components_zip_file_path,
            sha256_file_path=python_components_zip_sha256_file_path,
            sha256=components_zip_sha256,
            manifest_file_path=yaml_manifest_file_path
        )

def upload_metadata_to_gcs(bucket_name: str, metadata_file_path: Path, validator_opts: ValidatorOptions) -> MetadataUploadInfo:
    """Upload a metadata file to a GCS bucket.

    If the per 'version' key already exists it won't be overwritten.
    Also updates the 'latest' key on each new version.

    Args:
        bucket_name (str): Name of the GCS bucket to which the metadata file will be uploade.
        metadata_file_path (Path): Path to the metadata file.
        service_account_file_path (Path): Path to the JSON file with the service account allowed to read and write on the bucket.
        prerelease_tag (Optional[str]): Whether the connector is a prerelease_tag or not.
    Returns:
        Tuple[bool, str]: Whether the metadata file was uploaded and its blob id.
    """
    # Get our working directory
    working_directory = metadata_file_path.parent
    manifest_only_file_info = maybe_create_components_zip(working_directory)

    metadata_file_path = _apply_modifications_to_metadata_file(
        original_metadata_file_path=metadata_file_path,
        validator_opts=validator_opts,
        components_zip_sha256=manifest_only_file_info.sha256,
    )

    metadata, error = validate_and_load(metadata_file_path, POST_UPLOAD_VALIDATORS, validator_opts)
    if metadata is None:
        raise ValueError(f"Metadata file {metadata_file_path} is invalid for uploading: {error}")

    is_pre_release = validator_opts.prerelease_tag is not None
    is_release_candidate = getattr(metadata.data.releases, "isReleaseCandidate", False)
    should_upload_release_candidate = is_release_candidate and not is_pre_release
    should_upload_latest = not is_release_candidate and not is_pre_release
    gcs_creds = os.environ.get("GCS_CREDENTIALS")
    if not gcs_creds:
        raise ValueError("Please set the GCS_CREDENTIALS env var.")

    service_account_info = json.loads(gcs_creds)
    credentials = service_account.Credentials.from_service_account_info(service_account_info)
    storage_client = storage.Client(credentials=credentials)
    bucket = storage_client.bucket(bucket_name)
    docs_path = Path(validator_opts.docs_path)
    gcp_connector_dir = f"{METADATA_FOLDER}/{metadata.data.dockerRepository}"
    upload_as_version = metadata.data.dockerImageTag

    icon_uploaded, icon_blob_id = _icon_upload(metadata, bucket, metadata_file_path.parent / ICON_FILE_NAME)

    # Upload version metadata and doc
    # If the connector is a pre-release, we use the pre-release tag as the version
    # Otherwise, we use the dockerImageTag from the metadata
    version = metadata.data.dockerImageTag if not is_pre_release else validator_opts.prerelease_tag
    version_uploaded, version_blob_id = _metadata_upload(metadata, bucket, metadata_file_path, version)
    doc_version_uploaded, doc_version_blob_id = _doc_upload(metadata, bucket, docs_path, False, False)
    doc_inapp_version_uploaded, doc_inapp_version_blob_id = _doc_upload(metadata, bucket, docs_path, False, True)

    (manifest_yml_uploaded, manifest_yml_blob_id), (manifest_yml_latest_uploaded, manifest_yml_latest_blob_id) = _file_upload(
        local_path=manifest_only_file_info.manifest_file_path,
        gcp_connector_dir=gcp_connector_dir,
        bucket=bucket,
        upload_as_version=upload_as_version,
        upload_as_latest=should_upload_latest,
    )
    (components_zip_sha256_uploaded, components_zip_sha256_blob_id), (
        components_zip_sha256_latest_uploaded,
        components_zip_sha256_latest_blob_id,
    ) = _file_upload(
        local_path=manifest_only_file_info.sha256_file_path,
        gcp_connector_dir=gcp_connector_dir,
        bucket=bucket,
        upload_as_version=upload_as_version,
        upload_as_latest=should_upload_latest,
    )
    (components_zip_uploaded, components_zip_blob_id), (components_zip_latest_uploaded, components_zip_latest_blob_id) = _file_upload(
        local_path=manifest_only_file_info.zip_file_path,
        gcp_connector_dir=gcp_connector_dir,
        bucket=bucket,
        upload_as_version=upload_as_version,
        upload_as_latest=should_upload_latest,
    )

    latest_uploaded, latest_blob_id = False, None
    doc_latest_uploaded, doc_latest_blob_id = doc_inapp_latest_uploaded, doc_inapp_latest_blob_id = False, None

    release_candidate_uploaded, release_candidate_blob_id = False, None

    # Latest upload
    # We upload
    # - the current metadata to the "latest" path
    # - the current doc to the "latest" path
    # - the current inapp doc to the "latest" path
    if should_upload_latest:
        latest_uploaded, latest_blob_id = _metadata_upload(metadata, bucket, metadata_file_path, LATEST_GCS_FOLDER_NAME)
        doc_latest_uploaded, doc_latest_blob_id = _doc_upload(metadata, bucket, docs_path, True, False)
        doc_inapp_latest_uploaded, doc_inapp_latest_blob_id = _doc_upload(metadata, bucket, docs_path, True, True)

    # Release candidate upload
    # We just upload the current metadata to the "release_candidate" path
    # The doc and inapp doc are not uploaded, which means that the release candidate will still point to the latest doc
    if should_upload_release_candidate:
        release_candidate_uploaded, release_candidate_blob_id = _metadata_upload(
            metadata, bucket, metadata_file_path, RELEASE_CANDIDATE_GCS_FOLDER_NAME
        )

    return MetadataUploadInfo(
        metadata_uploaded=version_uploaded or latest_uploaded or release_candidate_uploaded,
        metadata_file_path=str(metadata_file_path),
        uploaded_files=[
            UploadedFile(
                id="version_metadata",
                uploaded=version_uploaded,
                description="versioned metadata",
                blob_id=version_blob_id,
            ),
            UploadedFile(
                id="latest_metadata",
                uploaded=latest_uploaded,
                description="latest metadata",
                blob_id=latest_blob_id,
            ),
            UploadedFile(
                id="release_candidate_metadata",
                uploaded=release_candidate_uploaded,
                description="release candidate metadata",
                blob_id=release_candidate_blob_id,
            ),
            UploadedFile(
                id="icon",
                uploaded=icon_uploaded,
                description="icon",
                blob_id=icon_blob_id,
            ),
            UploadedFile(
                id="doc_version",
                uploaded=doc_version_uploaded,
                description="versioned doc",
                blob_id=doc_version_blob_id,
            ),
            UploadedFile(
                id="doc_latest",
                uploaded=doc_latest_uploaded,
                description="latest doc",
                blob_id=doc_latest_blob_id,
            ),
            UploadedFile(
                id="manifest_yml",
                description="versioned manifest.yml file",
                uploaded=manifest_yml_uploaded,
                blob_id=manifest_yml_blob_id,
            ),
            UploadedFile(
                id="components_zip",
                description="python_components.zip file",
                uploaded=components_zip_uploaded,
                blob_id=components_zip_blob_id,
            ),
            UploadedFile(
                id="components_zip_sha256",
                description="python_components.zip.sha256 file",
                uploaded=components_zip_sha256_uploaded,
                blob_id=components_zip_sha256_blob_id,
            ),
            UploadedFile(
                id="manifest_yml_latest",
                description="latest manifest.yml file",
                uploaded=manifest_yml_latest_uploaded,
                blob_id=manifest_yml_latest_blob_id,
            ),
            UploadedFile(
                id="components_zip_latest",
                description="latest python_components.zip file",
                uploaded=components_zip_latest_uploaded,
                blob_id=components_zip_latest_blob_id,
            ),
            UploadedFile(
                id="components_zip_sha256_latest",
                description="latest python_components.zip.sha256 file",
                uploaded=components_zip_sha256_latest_uploaded,
                blob_id=components_zip_sha256_latest_blob_id,
            ),
            UploadedFile(
                id="doc_inapp_version",
                uploaded=doc_inapp_version_uploaded,
                description="versioned inapp doc",
                blob_id=doc_inapp_version_blob_id,
            ),
            UploadedFile(
                id="doc_inapp_latest",
                uploaded=doc_inapp_latest_uploaded,
                description="latest inapp doc",
                blob_id=doc_inapp_latest_blob_id,
            ),
        ],
    )
