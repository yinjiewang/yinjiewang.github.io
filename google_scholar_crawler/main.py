import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup


SCHOLAR_URL = "https://scholar.google.com/citations"
SERPAPI_URL = "https://serpapi.com/search.json"
PAGE_SIZE = 100
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_MAX_PAGES = 5


def parse_int(value) -> int:
    if isinstance(value, (int, float)):
        return int(value)

    match = re.search(r"\d[\d,]*", str(value or ""))
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


def get_recent_metric(metric: Dict) -> int:
    for key, value in metric.items():
        if key != "all":
            return parse_int(value)
    return 0


def parse_serpapi_cited_by(cited_by: Dict) -> Dict:
    metrics = {
        "citedby": 0,
        "citedby5y": 0,
        "hindex": 0,
        "hindex5y": 0,
        "i10index": 0,
        "i10index5y": 0,
    }

    for row in cited_by.get("table", []):
        for metric_name, metric_value in row.items():
            if not isinstance(metric_value, dict):
                continue

            normalized_name = metric_name.lower().replace("-", "_")
            if "citation" in normalized_name:
                metrics["citedby"] = parse_int(metric_value.get("all"))
                metrics["citedby5y"] = get_recent_metric(metric_value)
            elif "h_index" in normalized_name or normalized_name in {"hindex", "indice_h"}:
                metrics["hindex"] = parse_int(metric_value.get("all"))
                metrics["hindex5y"] = get_recent_metric(metric_value)
            elif "i10" in normalized_name:
                metrics["i10index"] = parse_int(metric_value.get("all"))
                metrics["i10index5y"] = get_recent_metric(metric_value)

    return metrics


def parse_serpapi_author_response(payload: Dict) -> Dict:
    metrics = parse_serpapi_cited_by(payload.get("cited_by", {}))
    publications = {}

    for article in payload.get("articles", []):
        publication_id = article.get("citation_id") or ""
        if not publication_id and article.get("link"):
            query = parse_qs(urlparse(article["link"]).query)
            publication_id = query.get("citation_for_view", [""])[0]
        if not publication_id:
            continue

        publications[publication_id] = {
            "author_pub_id": publication_id,
            "bib": {
                "title": article.get("title", ""),
                "author": article.get("authors", ""),
                "venue": article.get("publication", ""),
                "pub_year": str(article.get("year", "")),
            },
            "num_citations": parse_int((article.get("cited_by") or {}).get("value")),
        }

    return {
        "name": (payload.get("author") or {}).get("name", ""),
        "citedby": metrics["citedby"],
        "citedby5y": metrics["citedby5y"],
        "hindex": metrics["hindex"],
        "hindex5y": metrics["hindex5y"],
        "i10index": metrics["i10index"],
        "i10index5y": metrics["i10index5y"],
        "publications": publications,
    }


def fetch_author_with_serpapi(
    session: requests.Session,
    author_id: str,
    expected_publication_ids: Set[str],
    timeout_seconds: int,
    max_pages: int,
    api_key: str,
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
        response = session.get(
            SERPAPI_URL,
            params={
                "engine": "google_scholar_author",
                "author_id": author_id,
                "hl": "en",
                "num": PAGE_SIZE,
                "start": page_index * PAGE_SIZE,
                "sort": "pubdate",
                "api_key": api_key,
            },
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise RuntimeError(f"SerpApi returned an error: {payload['error']}")

        page_data = parse_serpapi_author_response(payload)
        if page_index == 0:
            for key in ("name", "citedby", "citedby5y", "hindex", "hindex5y", "i10index", "i10index5y"):
                author_data[key] = page_data[key]

        page_publications = page_data["publications"]
        author_data["publications"].update(page_publications)

        if expected_for_author and expected_for_author.issubset(author_data["publications"]):
            break
        if len(payload.get("articles", [])) < PAGE_SIZE:
            break

    return author_data


def collect_author_data(
    author_ids: List[str],
    expected_publication_ids: Set[str],
    fetch_author_data: Callable[[str], Dict],
) -> Dict:
    merged_publications = {}
    primary_data = None
    fetched_authors = {}

    for author_id in author_ids:
        author_data = fetch_author_data(author_id)
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

    return {
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


def load_fallback_data(path_value: str, error: Exception) -> Optional[Dict]:
    if not path_value:
        return None

    fallback_path = Path(path_value)
    if not fallback_path.exists():
        return None

    print(f"Warning: live citation fetch failed: {error}")
    print(f"Warning: using previous citation data from {fallback_path}.")
    return json.loads(fallback_path.read_text(encoding="utf-8"))


def write_results(script_dir: Path, output: Dict) -> None:
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


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent
    expected_publication_ids = get_expected_publication_ids(repo_root)
    primary_author_id = os.environ.get("GOOGLE_SCHOLAR_ID", "").strip()
    author_ids = get_author_ids(primary_author_id, expected_publication_ids)
    timeout_seconds = int(os.environ.get("GOOGLE_SCHOLAR_TIMEOUT", DEFAULT_TIMEOUT_SECONDS))
    max_pages = int(os.environ.get("GOOGLE_SCHOLAR_MAX_PAGES", DEFAULT_MAX_PAGES))
    serpapi_key = os.environ.get("SERPAPI_API_KEY", "").strip()

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://scholar.google.com/",
        }
    )

    try:
        if serpapi_key:
            print("Fetching citation data via SerpApi.")
            output = collect_author_data(
                author_ids,
                expected_publication_ids,
                lambda author_id: fetch_author_with_serpapi(
                    session,
                    author_id,
                    expected_publication_ids,
                    timeout_seconds,
                    max_pages,
                    serpapi_key,
                ),
            )
        else:
            print("Fetching citation data directly from Google Scholar.")
            output = collect_author_data(
                author_ids,
                expected_publication_ids,
                lambda author_id: fetch_author(
                    session,
                    author_id,
                    expected_publication_ids,
                    timeout_seconds,
                    max_pages,
                ),
            )
    except (requests.RequestException, RuntimeError) as error:
        output = load_fallback_data(os.environ.get("GOOGLE_SCHOLAR_FALLBACK_JSON", ""), error)
        if output is None:
            raise RuntimeError(
                "Failed to fetch live citation data and no previous gs_data.json fallback is available. "
                "GitHub-hosted runners are often blocked by Google Scholar with HTTP 403; add SERPAPI_API_KEY "
                "or make sure the google-scholar-stats branch exists."
            ) from error

    write_results(script_dir, output)

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
