"""Validate that all relative links in doch/ documentation resolve."""

import re
from pathlib import Path

LINK_PATTERN = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _collect_md_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.md"))


def _extract_relative_links(content: str) -> list[str]:
    links = []
    for match in LINK_PATTERN.finditer(content):
        target = match.group(2)
        if not target.startswith(("http://", "https://", "#")):
            links.append(target.split("#")[0])  # strip anchor
    return links


def test_all_doch_links_resolve(doch_dir):
    """Every relative link in doch/ must point to an existing file or directory."""
    md_files = _collect_md_files(doch_dir)
    assert len(md_files) > 0, "No markdown files found in doch/"

    broken = []
    for md_file in md_files:
        content = md_file.read_text()
        for link in _extract_relative_links(content):
            # Links without extension may be directories containing README.md
            target = (md_file.parent / link).resolve()
            if target.is_file():
                continue
            # Try with README.md appended (directory links)
            target_readme = target / "README.md"
            if target_readme.is_file():
                continue
            broken.append((str(md_file.relative_to(doch_dir.parent)), link))

    assert not broken, (
        f"Found {len(broken)} broken link(s) in documentation:\n"
        + "\n".join(f"  {file} → {link}" for file, link in broken)
    )


def test_index_covers_all_docs(doch_dir):
    """doch/README.md must link to every top-level doc directory."""
    index = doch_dir / "README.md"
    assert index.is_file(), "doch/README.md missing"

    content = index.read_text()
    linked = set()
    for match in LINK_PATTERN.finditer(content):
        target = match.group(2)
        if target.startswith("http"):
            continue
        linked.add(target.split("/")[0])

    top_dirs = {
        d.name
        for d in doch_dir.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    }

    missing = top_dirs - linked
    assert not missing, (
        f"doch/README.md missing links to: {missing}"
    )
