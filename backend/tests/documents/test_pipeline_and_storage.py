"""End-to-end generation (render -> verify -> PDF -> validate) and the file store."""

from __future__ import annotations

import os
import stat
import time
from datetime import date

import pytest

from app.documents import (
    DocumentVerificationError,
    InvalidDocumentData,
    PdfInvalid,
    StorageError,
    TemplateFileError,
    generate_document,
    sha256_file,
)
from app.storage import FileStore
from fixtures import eopyy_builder as fx
from fixtures.eopyy_maps import production_field_map
from tests.conftest import REPO_ROOT
from tests.documents.conftest import make_data


# ---------------------------------------------------------------- pipeline
@pytest.mark.libreoffice
def test_generate_document_end_to_end(tmp_path, soffice):
    built = fx.build_eopyy_docx(tmp_path / "t.docx", header_footer_images=True, split_runs=True)
    fmap = production_field_map(built)
    store = FileStore(tmp_path / "data")
    stored = store.put_template(built.path)
    result = generate_document(
        stored.path,
        fmap,
        make_data(built),
        store.new_preview_dir(),
        soffice=soffice,
        expected_pages=1,
    )
    assert result.docx.is_file() and result.pdf.is_file()
    assert result.pdf_report.pages == 1
    assert result.libreoffice_version is None or "LibreOffice" in result.libreoffice_version
    assert sha256_file(stored.path) == stored.sha256  # stored template untouched


@pytest.mark.libreoffice
def test_generation_stops_on_unexpected_page_count(tmp_path, built, fmap, data, soffice):
    with pytest.raises(PdfInvalid, match="expected 2"):
        generate_document(built.path, fmap, data, tmp_path / "w", soffice=soffice, expected_pages=2)


def test_generation_stops_before_pdf_when_docx_verification_fails(
    tmp_path, built, fmap, data, monkeypatch
):
    import app.documents.pipeline as pipeline

    monkeypatch.setattr(pipeline, "verify_document", lambda *a: ["simulated problem"])

    def must_not_run(*_a, **_k):
        raise AssertionError("conversion must not start after a failed verification")

    monkeypatch.setattr(pipeline, "convert_docx_to_pdf", must_not_run)
    with pytest.raises(DocumentVerificationError, match="simulated problem"):
        generate_document(built.path, fmap, data, tmp_path / "w", soffice=tmp_path / "x")


def test_generation_refuses_bad_data_before_writing(tmp_path, built, fmap):
    data = make_data(
        built, sessions={c: [date(2027, 1, 1)] for c in ("speech", "occupational", "physiotherapy")}
    )
    with pytest.raises(InvalidDocumentData):
        generate_document(built.path, fmap, data, tmp_path / "w", soffice=tmp_path / "x")
    assert list((tmp_path / "w").iterdir()) == []


# ---------------------------------------------------------------- storage
@pytest.fixture()
def store(tmp_path):
    return FileStore(tmp_path / "data")


def test_store_layout_is_created(store):
    assert {p.name for p in store.root.iterdir()} == {"templates", "documents", "previews"}


def test_store_refuses_a_root_inside_the_repository():
    with pytest.raises(StorageError, match="inside a Git repository"):
        FileStore(REPO_ROOT / "storage")
    assert not (REPO_ROOT / "storage").exists()


def test_templates_are_content_addressed_read_only_and_never_overwritten(store, built):
    before = built.path.read_bytes()
    first = store.put_template(built.path)
    assert first.relative_path == f"templates/{first.sha256}.docx"
    assert first.sha256 == sha256_file(built.path)
    assert built.path.read_bytes() == before  # the upload is only read
    assert not os.access(first.path, os.W_OK)
    assert not (first.path.stat().st_mode & stat.S_IWRITE)
    again = store.put_template(built.path)  # identical upload reuses the stored file
    assert again == first
    assert len(list((store.root / "templates").iterdir())) == 1


def test_altered_stored_template_is_detected(store, built):
    stored = store.put_template(built.path)
    stored.path.chmod(stat.S_IWRITE | stat.S_IREAD)
    stored.path.write_bytes(stored.path.read_bytes() + b"\x00")
    with pytest.raises(StorageError, match="altered"):
        store.verify(stored.relative_path, stored.sha256)
    with pytest.raises(StorageError, match="altered"):
        store.put_template(built.path)  # re-upload notices the damaged copy


def test_invalid_templates_are_not_stored(store, tmp_path):
    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"not a docx")
    with pytest.raises(TemplateFileError):
        store.put_template(bad)
    assert list((store.root / "templates").iterdir()) == []


def test_documents_are_never_overwritten(store, tmp_path):
    src = tmp_path / "x.pdf"
    src.write_bytes(b"%PDF-1.4 test")
    stored = store.put_document(src, "period-12-v1.pdf")
    assert store.verify(stored.relative_path, stored.sha256) == stored.path
    with pytest.raises(StorageError, match="never overwritten"):
        store.put_document(src, "period-12-v1.pdf")
    assert not os.access(stored.path, os.W_OK)


@pytest.mark.parametrize(
    "name", ["../escape.pdf", "sub/dir.pdf", "", ".hidden", "x.partial", "ΔΟΚΙΜΑΣΤΙΚΟΣ.pdf"]
)
def test_document_names_are_restricted(store, tmp_path, name):
    src = tmp_path / "x.pdf"
    src.write_bytes(b"%PDF")
    with pytest.raises(StorageError, match="invalid document file name"):
        store.put_document(src, name)


@pytest.mark.parametrize("relative", ["../outside.txt", "templates/../../outside.txt", ""])
def test_paths_cannot_escape_the_store(store, relative):
    with pytest.raises(StorageError, match="outside the file store"):
        store.resolve(relative)


def test_missing_stored_file_is_reported(store):
    with pytest.raises(StorageError, match="missing"):
        store.verify("documents/nothing.pdf", "0" * 64)


def test_preview_folders_and_cleanup(store):
    old, new = store.new_preview_dir(), store.new_preview_dir()
    assert old != new and old.parent == store.root / "previews"
    (old / "document.docx").write_bytes(b"x")
    past = time.time() - 3 * 24 * 3600
    os.utime(old, (past, past))
    assert store.cleanup_previews(older_than_seconds=24 * 3600) == 1
    assert not old.exists() and new.exists()
