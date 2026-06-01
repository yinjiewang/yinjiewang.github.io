from pathlib import Path


if __name__ == "__main__":
    header = "## Hi there 👋"
    script_dir = Path(__file__).resolve().parent
    base_dir = script_dir.parent / "_pages" / "includes"

    intro = (base_dir / "intro.md").read_text(encoding="utf-8").strip()
    homepage = (base_dir / "homepage.md").read_text(encoding="utf-8").strip()
    pub = (base_dir / "pub_short.md").read_text(encoding="utf-8").strip()
    news = (base_dir / "news.md").read_text(encoding="utf-8").strip()

    (script_dir / "README.md").write_text(
        "\n\n".join(
            [
                header,
                intro,
                f"##{homepage}",
                f"##{news}",
                f"##{pub}",
            ]
        ),
        encoding="utf-8",
    )
