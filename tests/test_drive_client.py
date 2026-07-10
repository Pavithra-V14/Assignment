from __future__ import annotations

import pytest

from project_health_agent.core.exceptions import DriveSyncError
from project_health_agent.ingestion.drive_client import parse_folder_id


@pytest.mark.parametrize(
    "value,expected",
    [
        ("https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrSt", "1AbCdEfGhIjKlMnOpQrSt"),
        ("https://drive.google.com/drive/u/0/folders/1AbCdEfGhIjKlMnOpQrSt", "1AbCdEfGhIjKlMnOpQrSt"),
        ("https://drive.google.com/open?id=1AbCdEfGhIjKlMnOpQrSt", "1AbCdEfGhIjKlMnOpQrSt"),
        ("1AbCdEfGhIjKlMnOpQrSt", "1AbCdEfGhIjKlMnOpQrSt"),  # bare ID passthrough
    ],
)
def test_parse_folder_id_accepts_links_and_bare_ids(value: str, expected: str) -> None:
    assert parse_folder_id(value) == expected


def test_parse_folder_id_rejects_unrecognized_url() -> None:
    with pytest.raises(DriveSyncError):
        parse_folder_id("https://drive.google.com/drive/my-drive")


def test_sync_folder_downloads_new_and_skips_unchanged(tmp_path, mocker) -> None:
    from project_health_agent.ingestion import drive_client

    remote_files = [
        {
            "id": "file-1",
            "name": "Project_A.xlsx",
            "mimeType": drive_client._XLSX_MIME,
            "modifiedTime": "2026-07-01T00:00:00.000Z",
        }
    ]
    mocker.patch.object(drive_client, "_build_service", return_value=object())
    mocker.patch.object(drive_client, "_list_folder_files", return_value=remote_files)
    download_mock = mocker.patch.object(
        drive_client, "_download_file",
        side_effect=lambda service, meta, dest: dest.write_bytes(b"fake-xlsx-bytes"),
    )

    cache_dir = tmp_path / "drive_cache"

    # First sync: file is new -> must download.
    paths = drive_client.sync_folder("1FolderId", cache_dir)
    assert len(paths) == 1
    assert paths[0].exists()
    assert download_mock.call_count == 1

    # Second sync with identical modifiedTime: must NOT re-download.
    drive_client.sync_folder("1FolderId", cache_dir)
    assert download_mock.call_count == 1


def test_sync_folder_removes_files_deleted_from_drive(tmp_path, mocker) -> None:
    from project_health_agent.ingestion import drive_client

    file_v1 = {
        "id": "file-1",
        "name": "Project_A.xlsx",
        "mimeType": drive_client._XLSX_MIME,
        "modifiedTime": "2026-07-01T00:00:00.000Z",
    }
    mocker.patch.object(drive_client, "_build_service", return_value=object())
    mocker.patch.object(
        drive_client, "_download_file",
        side_effect=lambda service, meta, dest: dest.write_bytes(b"fake-xlsx-bytes"),
    )

    cache_dir = tmp_path / "drive_cache"

    mocker.patch.object(drive_client, "_list_folder_files", return_value=[file_v1])
    paths = drive_client.sync_folder("1FolderId", cache_dir)
    assert paths[0].exists()

    # File removed from Drive on the next run -> local copy must disappear.
    mocker.patch.object(drive_client, "_list_folder_files", return_value=[])
    paths_after = drive_client.sync_folder("1FolderId", cache_dir)
    assert paths_after == []
    assert not paths[0].exists()
