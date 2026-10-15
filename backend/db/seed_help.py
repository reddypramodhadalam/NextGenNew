"""
db/seed_help.py

Loads the authored help articles from markdown files into the database.

Article text lives in backend/content/help/*.md, not in Python and not typed
into a screen. Three reasons:

  - It is reviewable. A change to help content shows up as a diff.
  - It is re-runnable. Seeding is idempotent: unchanged content produces no
    new version, so running this script does not invalidate training records.
  - It separates authoring from approval. Seeding creates drafts; a person
    approves them. That gate is the point, not an obstacle to route around.

Each file begins with YAML front matter between --- markers:

    ---
    slug: scheduling-runs
    title: Scheduling runs
    category: Running tests
    summary: Running suites unattended
    sort_order: 10
    ---

    # markdown body follows

Usage, from the backend directory:

    python db/seed_help.py                      # load as drafts
    python db/seed_help.py --approve ""         # load and approve
    python db/seed_help.py --dry-run            # report, change nothing
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

# Make the backend package importable when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.db.connection import init_pool  # noqa: E402
from core.settings import settings  # noqa: E402
from service import help_service  # noqa: E402


REQUIRED = ("slug", "title", "category")


def parse_article(path: Path) -> dict:
    """Read one markdown file and split its front matter from its body.

    Args:
        path: Path to the .md file.

    Returns:
        A dict of the front-matter fields plus 'body_md'.

    Raises:
        ValueError: If the front matter is missing, malformed, or lacks a
            required field. Failing loudly is deliberate — a silently skipped
            article is a gap nobody notices until a user cannot find it.
    """
    raw = path.read_text(encoding="utf-8")

    if not raw.lstrip().startswith("---"):
        raise ValueError(f"{path.name}: missing YAML front matter")

    stripped = raw.lstrip()
    parts = stripped.split("---", 2)
    if len(parts) < 3:
        raise ValueError(f"{path.name}: front matter is not closed with ---")

    meta = yaml.safe_load(parts[1]) or {}
    if not isinstance(meta, dict):
        raise ValueError(f"{path.name}: front matter is not a mapping")

    missing = [f for f in REQUIRED if not meta.get(f)]
    if missing:
        raise ValueError(f"{path.name}: front matter missing {', '.join(missing)}")

    body = parts[2].strip()
    if not body:
        raise ValueError(f"{path.name}: body is empty")

    meta["body_md"] = body
    return meta


def main() -> int:
    """Load every article file and report what changed.

    Returns:
        0 on success, 1 if any file could not be read.
    """
    approve_as = None
    if "--approve" in sys.argv:
        i = sys.argv.index("--approve")
        if i + 1 >= len(sys.argv):
            print('Usage: python db/seed_help.py --approve "Your Name"')
            return 1
        approve_as = sys.argv[i + 1]
    dry_run = "--dry-run" in sys.argv

    content_dir = Path(settings.help.content_dir)
    if not content_dir.is_absolute():
        content_dir = Path(__file__).resolve().parent.parent / content_dir

    if not content_dir.exists():
        print(f"No help content directory at {content_dir}")
        return 1

    files = sorted(content_dir.glob("*.md"))
    if not files:
        print(f"No .md files found in {content_dir}")
        return 1

    print(f"Content: {content_dir}")
    print(f"Files  : {len(files)}")
    print(f"Mode   : {'dry run' if dry_run else 'seeding'}"
          f"{f', approving as {approve_as}' if approve_as else ', as drafts'}\n")

    init_pool()

    created = versioned = unchanged = 0
    errors: list[str] = []

    for path in files:
        try:
            meta = parse_article(path)
        except ValueError as e:
            errors.append(str(e))
            print(f"  ERROR    {path.name}: {e}")
            continue

        if dry_run:
            print(f"  would load {meta['slug']}  ({meta['category']})")
            continue

        before = help_service.get_article(meta["slug"], include_drafts=True)
        article = help_service.upsert_article(
            slug=meta["slug"],
            title=meta["title"],
            category=meta["category"],
            body_md=meta["body_md"],
            summary=meta.get("summary"),
            author=meta.get("author", "NextGen2 team"),
            status=meta.get("status", "draft"),
            sort_order=int(meta.get("sort_order", 100)),
        )

        if before is None:
            created += 1
            state = f"created  v{article['version']}"
        elif before["id"] == article["id"]:
            unchanged += 1
            state = f"unchanged v{article['version']}"
        else:
            versioned += 1
            state = f"updated  v{article['version']} (was v{before['version']})"

        if approve_as and article["status"] != "approved":
            help_service.approve_article(article["id"], approved_by=approve_as)
            state += ", approved"

        print(f"  {state:28} {meta['slug']}")

    if dry_run:
        print("\nDry run — nothing was written.")
        return 0

    print(f"\n{created} created, {versioned} updated, {unchanged} unchanged.")

    if not approve_as:
        print(
            "\nArticles were loaded as DRAFTS. NextGen2 AI will not cite a draft "
            "and it does not count as training material.\n"
            'Approve them with: python db/seed_help.py --approve "Your Name"'
        )

    if errors:
        print(f"\n{len(errors)} file(s) could not be read.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
