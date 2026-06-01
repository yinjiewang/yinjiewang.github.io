import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Set
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup


SCHOLAR_URL = "https://scholar.google.com/citations"
PAGE_SIZE = 100
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_MAX_PAGES = 5


def parse_int(value: str) -> int:
    match = re.search(r"\d[\d,]*", value or "")
    return int(match.group(0).replace(",", "")) if match else 0


def get_expected_publication_ids(repo_root: Path) -> Set[str]:
    pattern = re.compile(
        r"class=['\"][^'\"]*show_paper_citations[^'\"]*['\"][^>]*\bdata=['\"]([^'\"]+)['\"]"
    )
    publication_ids: Set[str] = set()

    includes_dir = repo_root / "_pages" / "includes"
    if not includes_dir.exists():
        return publication_ids

    for markdown_file in includes_dir.glob("*.md"):
        publication_ids.update(pattern.findall(markdown_file.read_text(encoding="utf-8")))

    return publication_ids


def get_author_ids(primary_author_id: str, publication_ids: Iterable[str]) -> List[str]:
    author_ids = []
    if primary_author_id:
        author_ids.append(primary_author_id)

    for publication_id in sorted(publication_ids):
        author_id = publication_id.split(":", 1)[0]
        if author_id and author_id not in author_ids:
            author_ids.append(author_id)

    if not author_ids:
        raise RuntimeError(
            "Set GOOGLE_SCHOLAR_ID or add show_paper_citations entries with Google Scholar publication IDs."
        )

    return author_ids


def get_publication_id(base_url: str, row) -> str:
    link = row.select_one("a.gsc_a_at")
    if not link:
        return ""

    href = link.get("href", "")
    query = parse_qs(urlparse(urljoin(base_url, href)).query)
    return query.get("citation_for_view", [""])[0]


def fetch_author_page(
    session: requests.Session,
    author_id: str,
    start: int,
    timeout_seconds: int,
) -> BeautifulSoup:
    response = session.get(
        SCHOLAR_URL,
        params={
            "user": author_id,
            "hl": "en",
            "cstart": start,
            "pagesize": PAGE_SIZE,
            "view_op": "list_works",
            "sortby": "pubdate",
        },
        timeout=timeout_seconds,
    )
    response.raise_for_status()

    if "unusual traffic" in response.text.lower() or "/sorry/" in response.url:
        raise RuntimeError("Google Scholar returned an anti-bot page. Retry later or use a proxy-backed data source.")

    return BeautifulSoup(response.text, "html.parser")


def parse_author_page(soup: BeautifulSoup) -> Dict:
    name_element = soup.select_one("#gsc_prf_in")
    name = name_element.get_text(strip=True) if name_element else ""
    stats = [parse_int(element.get_text(" ", strip=True)) for element in soup.select(".gsc_rsb_std")]
    publications = {}

    for row in soup.select("tr.gsc_a_tr"):
        publication_id = get_publication_id(SCHOLAR_URL, row)
        if not publication_id:
            continue

        title = row.select_one("a.gsc_a_at")
        citations = row.select_one("a.gsc_a_ac")
        year = row.select_one(".gsc_a_y span")
        authors = row.select_one(".gsc_a_at + .gs_gray")
        venue = row.select_one(".gs_gray:nth-of-type(2)")

        publications[publication_id] = {
            "author_pub_id": publication_id,
            "bib": {
                "title": title.get_text(" ", strip=True) if title else "",
                "author": authors.get_text(" ", strip=True) if authors else "",
                "venue": venue.get_text(" ", strip=True) if venue else "",
                "pub_year": year.get_text(" ", strip=True) if year else "",
            },
            "num_citations": parse_int(citations.get_text(" ", strip=True) if citations else ""),
        }

    return {
        "name": name,
        "citedby": stats[0] if len(stats) > 0 else 0,
        "citedby5y": stats[1] if len(stats) > 1 else 0,
        "hindex": stats[2] if len(stats) > 2 else 0,
        "hindex5y": stats[3] if len(stats) > 3 else 0,
        "i10index": stats[4] if len(stats) > 4 else 0,
        "i10index5y": stats[5] if len(stats) > 5 else 0,
        "publications": publications,
    }


def fetch_author(
    session: requests.Session,
    author_id: str,
    expected_publication_ids: Set[str],
    timeout_seconds: int,
    max_pages: int,
) -> Dict:
    author_data = {
        "name": "",
        "citedby": 0,
        "citedby5y": 0,
        "hindex": 0,
        "hindex5y": 0,
        "i10index": 0,
        "i10index5y": 0,
        "publications": {},
    }

    expected_for_author = {
        publication_id
        for publication_id in expected_publication_ids
        if publication_id.startswith(f"{author_id}:")
    }

    for page_index in range(max_pages):
        soup = fetch_author_page(session, author_id, page_index * PAGE_SIZE, timeout_seconds)
        page_data = parse_author_page(soup)

        if page_index == 0:
            for key in ("name", "citedby", "citedby5y", "hindex", "hindex5y", "i10index", "i10index5y"):
                author_data[key] = page_data[key]

        page_publications = page_data["publications"]
        author_data["publications"].update(page_publications)

        if expected_for_author and expected_for_author.issubset(author_data["publications"]):
            break
        if len(page_publications) < PAGE_SIZE:
            break

    return author_data


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent
    expected_publication_ids = get_expected_publication_ids(repo_root)
    primary_author_id = os.environ.get("GOOGLE_SCHOLAR_ID", "").strip()
    author_ids = get_author_ids(primary_author_id, expected_publication_ids)
    timeout_seconds = int(os.environ.get("GOOGLE_SCHOLAR_TIMEOUT", DEFAULT_TIMEOUT_SECONDS))
    max_pages = int(os.environ.get("GOOGLE_SCHOLAR_MAX_PAGES", DEFAULT_MAX_PAGES))

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }
    )

    merged_publications = {}
    primary_data = None
    fetched_authors = {}

    for author_id in author_ids:
        author_data = fetch_author(session, author_id, expected_publication_ids, timeout_seconds, max_pages)
        fetched_authors[author_id] = {
            key: author_data[key]
            for key in ("name", "citedby", "citedby5y", "hindex", "hindex5y", "i10index", "i10index5y")
        }
        merged_publications.update(author_data["publications"])
        if primary_data is None:
            primary_data = author_data

    if primary_data is None:
        raise RuntimeError("No Google Scholar author data was fetched.")

    missing_publications = sorted(expected_publication_ids - set(merged_publications))
    if missing_publications:
        print("Warning: missing expected publication IDs: " + ", ".join(missing_publications))

    output = {
        "name": primary_data["name"],
        "citedby": primary_data["citedby"],
        "citedby5y": primary_data["citedby5y"],
        "hindex": primary_data["hindex"],
        "hindex5y": primary_data["hindex5y"],
        "i10index": primary_data["i10index"],
        "i10index5y": primary_data["i10index5y"],
        "updated": datetime.now(timezone.utc).isoformat(),
        "authors": fetched_authors,
        "publications": merged_publications,
        "missing_publications": missing_publications,
    }

    results_dir = script_dir / "results"
    results_dir.mkdir(exist_ok=True)
    (results_dir / "gs_data.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (results_dir / "gs_data_shieldsio.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "label": "citations",
                "message": str(output["citedby"]),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
