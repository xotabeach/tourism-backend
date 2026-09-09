"""Help source pack is explicit, versioned and not silently published."""

import json
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from tourism_backend.modules.support.application.help_content import HelpManifest
from tourism_backend.modules.support.infrastructure.help_catalog import (
    _dart_string,
    load_help_catalog,
    render_mobile_faq,
)

PACK = Path(__file__).resolve().parents[2] / "data" / "support_help"


def test_pack_has_fifteen_versioned_articles_but_is_not_published() -> None:
    catalog = load_help_catalog(PACK)
    assert len(catalog.articles) == 15
    assert catalog.manifest.status == "draft"
    assert not catalog.manifest.release_verified
    assert catalog.manifest.approved_by is None
    # Bump with every mobile release. Help search filters by exact app
    # version, so a corpus left on the previous one answers "no articles for
    # this build" to everybody — this assertion is the tripwire for that.
    assert catalog.manifest.target_app_version == "0.2.4"
    assert len({a.spec.id for a in catalog.articles}) == 15
    assert all(a.spec.evidence and a.spec.revision >= 1 for a in catalog.articles)
    assert all(a.plain_text and "##" not in a.plain_text for a in catalog.articles)


def test_dart_export_is_deterministic_and_retains_all_source_ids() -> None:
    catalog = load_help_catalog(PACK)
    rendered = render_mobile_faq(catalog)
    assert rendered == render_mobile_faq(load_help_catalog(PACK))
    assert catalog.content_hash in rendered
    assert all(f'articleId: "{a.spec.id}"' in rendered for a in catalog.articles)
    assert rendered.count("  SupportFaqItem(") == 15


def test_dart_strings_escape_interpolation_quotes_and_control_characters() -> None:
    assert _dart_string('$x "quoted"\n\\path') == '"\\$x \\"quoted\\"\\n\\\\path"'


@pytest.mark.parametrize("approved_by", [None, "   "])
def test_publication_without_editorial_approval_is_rejected(approved_by: str | None) -> None:
    payload = json.loads((PACK / "manifest.json").read_text())
    payload.update(status="published", release_verified=True, approved_by=approved_by)
    with pytest.raises(ValidationError, match="Publication needs"):
        HelpManifest.model_validate(payload)


def test_code_review_is_not_release_verification() -> None:
    payload = json.loads((PACK / "manifest.json").read_text())
    payload.update(status="published", approved_by="editor")
    with pytest.raises(ValidationError, match="Publication needs"):
        HelpManifest.model_validate(payload)


@pytest.mark.parametrize("key", ["id", "body_file", "faq_id"])
def test_duplicate_identity_or_faq_route_is_rejected(key: str) -> None:
    payload = json.loads((PACK / "manifest.json").read_text())
    payload["articles"][1][key] = payload["articles"][0][key]
    with pytest.raises(ValidationError, match="Duplicate"):
        HelpManifest.model_validate(payload)


@pytest.mark.parametrize("path", ["../secret.md", "/etc/passwd", "articles/../../secret.md"])
def test_manifest_cannot_reference_arbitrary_files(path: str) -> None:
    payload = json.loads((PACK / "manifest.json").read_text())
    payload["articles"][0]["body_file"] = path
    with pytest.raises(ValidationError):
        HelpManifest.model_validate(payload)


def test_symlink_cannot_escape_pack(tmp_path: Path) -> None:
    root = tmp_path / "help"
    shutil.copytree(PACK, root)
    article = root / "articles/routes-difficulty.md"
    outside = tmp_path / "private.md"
    outside.write_text("private fixture")
    article.unlink()
    article.symlink_to(outside)
    with pytest.raises(ValueError, match="escapes"):
        load_help_catalog(root)


def test_unlisted_article_is_not_silently_indexable(tmp_path: Path) -> None:
    root = tmp_path / "help"
    shutil.copytree(PACK, root)
    (root / "articles/internal-runbook.md").write_text("not approved")
    with pytest.raises(ValueError, match="differ"):
        load_help_catalog(root)


def test_withdrawn_pack_cannot_be_bundled(tmp_path: Path) -> None:
    root = tmp_path / "help"
    shutil.copytree(PACK, root)
    manifest_path = root / "manifest.json"
    payload = json.loads(manifest_path.read_text())
    payload["status"] = "withdrawn"
    manifest_path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="Withdrawn"):
        render_mobile_faq(load_help_catalog(root))


@pytest.mark.parametrize(
    "body", ["<script>alert(1)</script>", "[link](file:///private)", "### Hidden"]
)
def test_unsupported_markup_is_rejected(tmp_path: Path, body: str) -> None:
    root = tmp_path / "help"
    shutil.copytree(PACK, root)
    article = root / "articles/routes-difficulty.md"
    article.write_text(f"# Уровень сложности\n\nAnswer.\n\n{body}\n")
    with pytest.raises(ValueError, match="Only second-level|plain paragraphs"):
        load_help_catalog(root)


def test_article_change_invalidates_source_fingerprint(tmp_path: Path) -> None:
    root = tmp_path / "help"
    shutil.copytree(PACK, root)
    before = load_help_catalog(root)
    article = root / "articles/routes-difficulty.md"
    article.write_text(article.read_text() + "\nНовое пояснение.\n")
    after = load_help_catalog(root)
    assert before.content_hash != after.content_hash
    assert before.articles[0].content_hash != after.articles[0].content_hash


def test_faq_does_not_keep_the_known_wrong_product_claims() -> None:
    articles = {a.spec.id: a.plain_text for a in load_help_catalog(PACK).articles}
    assert "все обязательные" in articles["routes-order"]
    assert "автор маршрута" in articles["points-earn"]
    assert "владелец этого профиля" in articles["points-earn"]
    assert "появится позже" not in articles["points-earn"]
    assert "сборку своего маршрута" in articles["app-ai-chat"]
    assert "не является бронированием" in articles["app-ai-chat"]
    assert "не установле" in articles["app-support"]
