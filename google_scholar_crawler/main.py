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
TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"
SERPAPI_URL = "https://serpapi.com/search.json"
PAGE_SIZE = 100
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_MAX_PAGES = 5
METRIC_KEYS = ("citedby", "citedby5y", "hindex", "hindex5y", "i10index", "i10index5y")


def parse_int(value) -> int:
    if isinstance(value, (int, float)):
        return int(value)

    match = re.search(r"\d[\d,]*", str(value or ""))
    return int(match.group(0).replace(",", "")) if match else 0


def parse_bool(value: str, default: bool = False) -> bool:
    if value == "":
        return default

    return value.strip().lower() in {"1", "true", "yes", "on"}


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.lower())).strip()


def get_expected_publications(repo_root: Path) -> Dict[str, Dict]:
    publications: Dict[str, Dict] = {}
    includes_dir = repo_root / "_pages" / "includes"
    if not includes_dir.exists():
        return publications

    for markdown_file in includes_dir.glob("*.md"):
        for line in markdown_file.read_text(encoding="utf-8").splitlines():
            if "show_paper_citations" not in line:
                continue

            id_match = re.search(r"\bdata=['\"]([^'\"]+)['\"]", line)
            if not id_match:
                continue

            publication_id = id_match.group(1)
            title_match = re.search(r"\[([^\]]+)\]\([^)]+\)", line)
            citations_match = re.search(r"\bdata-citations=['\"](\d+)['\"]", line)

            publication = publications.setdefault(publication_id, {"author_pub_id": publication_id})
            if title_match:
                publication["title"] = title_match.group(1).strip()
            if citations_match:
                publication["fallback_citations"] = max(
                    parse_int(citations_match.group(1)),
                    parse_int(publication.get("fallback_citations")),
                )

    return publications


def get_expected_publication_ids(repo_root: Path) -> Set[str]:
    return set(get_expected_publications(repo_root))


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
            for key in ("name", *METRIC_KEYS):
                author_data[key] = page_data[key]

        page_publications = page_data["publications"]
        author_data["publications"].update(page_publications)

        if expected_for_author and expected_for_author.issubset(author_data["publications"]):
            break
        if len(page_publications) < PAGE_SIZE:
            break

    return author_data


def get_scholar_author_url(author_id: str) -> str:
    return f"{SCHOLAR_URL}?hl=en&user={author_id}"


def fetch_tavily_extract_content(
    session: requests.Session,
    url: str,
    timeout_seconds: int,
    api_key: str,
) -> str:
    response = session.post(
        TAVILY_EXTRACT_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "urls": url,
            "extract_depth": "advanced",
            "format": "markdown",
            "timeout": min(max(timeout_seconds, 1), 60),
        },
        timeout=max(timeout_seconds + 10, 30),
    )
    response.raise_for_status()
    payload = response.json()

    results = payload.get("results", [])
    if not results:
        raise RuntimeError(f"Tavily did not return extracted content for {url}: {payload.get('failed_results', [])}")

    raw_content = "\n".join(result.get("raw_content") or "" for result in results)
    if not raw_content.strip():
        raise RuntimeError(f"Tavily returned empty extracted content for {url}.")

    return raw_content


def get_text_window(raw_content: str, title: str, max_lines: int = 12) -> str:
    normalized_title = normalize_text(title)
    lines = [line.strip() for line in raw_content.splitlines() if line.strip()]

    for index, line in enumerate(lines):
        normalized_line = normalize_text(line)
        if normalized_title and (
            normalized_title in normalized_line
            or (len(normalized_line) > 25 and normalized_line in normalized_title)
        ):
            return "\n".join(lines[index : index + max_lines])

    normalized_content = normalize_text(raw_content)
    position = normalized_content.find(normalized_title)
    if position == -1:
        return ""

    return raw_content[max(0, position - 200) : position + len(title) + 800]


def parse_citation_count_from_window(window: str) -> Optional[int]:
    if not window:
        return None

    labeled_match = re.search(r"(?:cited\s+by|citations?)\D{0,12}(\d[\d,]*)", window, re.IGNORECASE)
    if labeled_match:
        return parse_int(labeled_match.group(1))

    for line in window.splitlines()[1:8]:
        cleaned_line = line.strip()
        if re.search(r"\b(19|20)\d{2}\b", cleaned_line):
            continue

        number_match = re.fullmatch(r"(?:\[[^\]]*\]\([^)]*\)|\D)*(\d[\d,]*)(?:\D*)", cleaned_line)
        if number_match:
            value = parse_int(number_match.group(1))
            if value < 1900 or value > datetime.now(timezone.utc).year + 1:
                return value

    return None


def parse_tavily_publications(raw_content: str, expected_publications: Dict[str, Dict]) -> Dict:
    publications = {}

    for publication_id, expected_publication in expected_publications.items():
        title = expected_publication.get("title", "")
        windows = []

        id_position = raw_content.find(publication_id)
        if id_position != -1:
            windows.append(raw_content[max(0, id_position - 500) : id_position + 800])

        if title:
            windows.append(get_text_window(raw_content, title))

        for window in windows:
            citation_count = parse_citation_count_from_window(window)
            if citation_count is None:
                continue

            publications[publication_id] = {
                "author_pub_id": publication_id,
                "bib": {"title": title},
                "num_citations": citation_count,
                "source": "tavily",
            }
            break

    return publications


def parse_tavily_metrics(raw_content: str) -> Dict:
    metrics = {key: 0 for key in METRIC_KEYS}

    patterns = {
        "citedby": r"\bCitations?\b\D{0,80}(\d[\d,]*)",
        "hindex": r"\bh[- ]?index\b\D{0,80}(\d[\d,]*)",
        "i10index": r"\bi10[- ]?index\b\D{0,80}(\d[\d,]*)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, raw_content, re.IGNORECASE)
        if match:
            metrics[key] = parse_int(match.group(1))

    return metrics


def fetch_author_with_tavily(
    session: requests.Session,
    author_id: str,
    expected_publications: Dict[str, Dict],
    timeout_seconds: int,
    api_key: str,
) -> Dict:
    author_url = get_scholar_author_url(author_id)
    raw_content = fetch_tavily_extract_content(session, author_url, timeout_seconds, api_key)
    metrics = parse_tavily_metrics(raw_content)

    return {
        "name": "",
        **metrics,
        "publications": parse_tavily_publications(raw_content, expected_publications),
    }


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
            for key in ("name", *METRIC_KEYS):
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
            for key in ("name", *METRIC_KEYS)
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
        **{key: primary_data[key] for key in METRIC_KEYS},
        "updated": datetime.now(timezone.utc).isoformat(),
        "authors": fetched_authors,
        "publications": merged_publications,
        "missing_publications": missing_publications,
    }


def build_empty_output(author_ids: List[str], expected_publication_ids: Set[str]) -> Dict:
    return {
        "name": "",
        **{key: 0 for key in METRIC_KEYS},
        "updated": datetime.now(timezone.utc).isoformat(),
        "authors": {
            author_id: {
                "name": "",
                **{key: 0 for key in METRIC_KEYS},
            }
            for author_id in author_ids
        },
        "publications": {},
        "missing_publications": sorted(expected_publication_ids),
    }


def load_fallback_data(path_value: str, error: Optional[Exception] = None) -> Optional[Dict]:
    if not path_value:
        return None

    fallback_path = Path(path_value)
    if not fallback_path.exists():
        return None

    if error is not None:
        print(f"Warning: live citation fetch failed: {error}")
    print(f"Warning: using previous citation data from {fallback_path}.")
    return json.loads(fallback_path.read_text(encoding="utf-8"))


def load_previous_data(path_value: str) -> Optional[Dict]:
    if not path_value:
        return None

    previous_path = Path(path_value)
    if not previous_path.exists():
        return None

    return json.loads(previous_path.read_text(encoding="utf-8"))


def merge_previous_data(output: Dict, previous_output: Optional[Dict]) -> Dict:
    if not previous_output:
        return output

    merged = json.loads(json.dumps(previous_output))

    if output.get("name"):
        merged["name"] = output["name"]
    for key in METRIC_KEYS:
        if parse_int(output.get(key)) > 0:
            merged[key] = output[key]

    merged.setdefault("authors", {}).update(output.get("authors", {}))
    merged.setdefault("publications", {})
    for publication_id, publication in output.get("publications", {}).items():
        merged["publications"][publication_id] = publication

    merged["missing_publications"] = output.get("missing_publications", [])
    merged["updated"] = output.get("updated", datetime.now(timezone.utc).isoformat())
    return merged


def load_citation_overrides(script_dir: Path) -> Dict:
    overrides_path = script_dir / "citation_overrides.json"
    if not overrides_path.exists():
        return {}

    return json.loads(overrides_path.read_text(encoding="utf-8"))


def apply_citation_overrides(output: Dict, overrides: Dict) -> Dict:
    if not overrides:
        return output

    for key in METRIC_KEYS:
        if key in overrides.get("metrics", {}):
            output[key] = parse_int(overrides["metrics"][key])

    output.setdefault("publications", {})
    for publication_id, override in overrides.get("publications", {}).items():
        publication = output["publications"].setdefault(
            publication_id,
            {
                "author_pub_id": publication_id,
                "bib": {},
                "num_citations": 0,
            },
        )

        if "num_citations" in override:
            publication["num_citations"] = max(
                parse_int(publication.get("num_citations")),
                parse_int(override["num_citations"]),
            )
        if override.get("bib"):
            publication.setdefault("bib", {}).update(override["bib"])
        publication["manual_override"] = True

    overridden_ids = set(overrides.get("publications", {}))
    output["missing_publications"] = [
        publication_id
        for publication_id in output.get("missing_publications", [])
        if publication_id not in overridden_ids
    ]
    output["updated"] = datetime.now(timezone.utc).isoformat()
    output["citation_overrides_updated"] = overrides.get("updated", "")
    return output


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
    expected_publications = get_expected_publications(repo_root)
    expected_publication_ids = set(expected_publications)
    primary_author_id = os.environ.get("GOOGLE_SCHOLAR_ID", "").strip()
    author_ids = get_author_ids(primary_author_id, expected_publication_ids)
    timeout_seconds = int(os.environ.get("GOOGLE_SCHOLAR_TIMEOUT", DEFAULT_TIMEOUT_SECONDS))
    max_pages = int(os.environ.get("GOOGLE_SCHOLAR_MAX_PAGES", DEFAULT_MAX_PAGES))
    tavily_key = os.environ.get("TAVILY_API_KEY", "").strip()
    serpapi_key = os.environ.get("SERPAPI_API_KEY", "").strip()
    live_fetch_enabled = parse_bool(os.environ.get("GOOGLE_SCHOLAR_LIVE_FETCH", ""), default=False)
    previous_output = load_previous_data(os.environ.get("GOOGLE_SCHOLAR_FALLBACK_JSON", ""))

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
        if tavily_key:
            print("Fetching citation data from Google Scholar via Tavily Extract.")
            output = collect_author_data(
                author_ids,
                expected_publication_ids,
                lambda author_id: fetch_author_with_tavily(
                    session,
                    author_id,
                    expected_publications,
                    timeout_seconds,
                    tavily_key,
                ),
            )
            output = merge_previous_data(output, previous_output)
        elif serpapi_key:
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
        elif not live_fetch_enabled:
            print("Skipping direct Google Scholar fetch on GitHub Actions.")
            output = previous_output
            if output is None:
                output = build_empty_output(author_ids, expected_publication_ids)
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
                "GitHub-hosted runners are often blocked by Google Scholar with HTTP 403; add TAVILY_API_KEY "
                "or make sure the google-scholar-stats branch exists."
            ) from error

    output = apply_citation_overrides(output, load_citation_overrides(script_dir))
    write_results(script_dir, output)

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
