"""Local, allowlisted help-pack reader and deterministic bundled FAQ export.

Nothing here reads the team's docs, contacts a model or writes to a database.
Draft content is exportable for the matching unreleased mobile build, not RAG.
"""

import hashlib
import json
import textwrap
from dataclasses import dataclass
from pathlib import Path

from tourism_backend.modules.support.application.help_content import HelpArticleSpec, HelpManifest


@dataclass(frozen=True, slots=True)
class HelpArticle:
    spec: HelpArticleSpec
    markdown: str
    plain_text: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class HelpCatalog:
    manifest: HelpManifest
    articles: tuple[HelpArticle, ...]
    content_hash: str


def _read_inside(root: Path, relative: str) -> str:
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Help file escapes the source pack")
    if path.stat().st_size > 64_000:
        raise ValueError("Help file exceeds source-pack size limit")
    return path.read_text(encoding="utf-8")


def _plain_text(markdown: str, title: str) -> str:
    paragraphs = markdown.strip().split("\n\n")
    if not paragraphs or paragraphs[0] != f"# {title}":
        raise ValueError("Article heading must match its manifest title")
    body = paragraphs[1:]
    if len(body) < 2:
        raise ValueError("Help article needs an answer and supporting detail")
    for paragraph in body:
        if any(
            line.startswith("#") and not line.startswith("## ") for line in paragraph.splitlines()
        ):
            raise ValueError("Only second-level section headings are supported")
    # This first content format deliberately has no executable HTML, remote
    # links or embedded media. Add a sanitized renderer before extending it.
    if any(marker in markdown for marker in ("<", ">", "](", "```")):
        raise ValueError("Help articles support plain paragraphs and headings only")
    return "\n\n".join(" ".join(part.removeprefix("## ").split()) for part in body)


def load_help_catalog(root: Path) -> HelpCatalog:
    root = root.resolve()
    manifest = HelpManifest.model_validate_json(_read_inside(root, "manifest.json"))
    listed = {item.body_file for item in manifest.articles}
    actual = {f"articles/{path.name}" for path in (root / "articles").glob("*.md")}
    if actual != listed:
        raise ValueError("Manifest and article files differ")
    articles: list[HelpArticle] = []
    for spec in manifest.articles:
        markdown = _read_inside(root, spec.body_file)
        articles.append(
            HelpArticle(
                spec=spec,
                markdown=markdown,
                plain_text=_plain_text(markdown, spec.title),
                content_hash=hashlib.sha256(markdown.encode()).hexdigest(),
            )
        )
    digest_input = manifest.model_dump_json() + "".join(a.content_hash for a in articles)
    return HelpCatalog(
        manifest=manifest,
        articles=tuple(articles),
        content_hash=hashlib.sha256(digest_input.encode()).hexdigest(),
    )


def _dart_string(value: str) -> str:
    # JSON escapes controls/backslashes; Dart also needs interpolation escaped.
    return json.dumps(value, ensure_ascii=False).replace("$", r"\$")


def render_mobile_faq(catalog: HelpCatalog) -> str:
    """Pure export; callers decide where/how to apply the generated source."""
    if catalog.manifest.status == "withdrawn":
        raise ValueError("Withdrawn help cannot be bundled")
    lines = [
        "// Generated from tourism-backend/data/support_help; do not edit by hand.",
        f"// Source SHA-256: {catalog.content_hash}",
        f"// Target build: {catalog.manifest.target_app_version}; not a live RAG publication.",
        "",
        "import 'package:tourism_mobile/features/settings/domain/support_faq_item.dart';",
        "",
    ]
    names = {
        "routes": "kRoutesNavigationFaq",
        "app": "kAppQuestionsFaq",
        "travel_points": "kTravelPointsFaq",
    }
    for category, name in names.items():
        lines.append(f"const {name} = <SupportFaqItem>[")
        for article in catalog.articles:
            spec = article.spec
            if spec.category != category:
                continue
            lines.extend(
                [
                    "  SupportFaqItem(",
                    f"    id: {_dart_string(spec.faq_id)},",
                    f"    articleId: {_dart_string(spec.id)},",
                    f"    revision: {spec.revision},",
                    f"    title: {_dart_string(spec.title)},",
                    f"    subtitle: {_dart_string(spec.question)},",
                    "    answer:",
                ]
            )
            chunks: list[str] = []
            paragraphs = article.plain_text.split("\n\n")
            for index, paragraph in enumerate(paragraphs):
                parts = textwrap.wrap(paragraph, width=58, break_long_words=False)
                chunks.extend(part + " " for part in parts[:-1])
                chunks.append(parts[-1] + ("\n\n" if index < len(paragraphs) - 1 else ""))
            for index, chunk in enumerate(chunks):
                suffix = "," if index == len(chunks) - 1 else ""
                lines.append(f"        {_dart_string(chunk)}{suffix}")
            lines.append("  ),")
        lines.extend(["];", ""])
    return "\n".join(lines)
