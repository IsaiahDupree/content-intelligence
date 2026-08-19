from pathlib import Path

from scripts.research_youtube_queries import classify_query_state, load_queries, slugify


def test_slugify_preserves_query_identity():
    assert slugify("GTA 6 leaks") == "gta-6-leaks"
    assert slugify("America's Got Talent: Quarterfinals 1") == (
        "america-s-got-talent-quarterfinals-1"
    )


def test_load_queries_deduplicates_without_reordering(tmp_path: Path):
    query_file = tmp_path / "queries.txt"
    query_file.write_text("# current demand\nweather tornado warning\nDune 3\n", encoding="utf-8")

    assert load_queries(["Dune 3", "2K27"], query_file) == [
        "Dune 3",
        "2K27",
        "weather tornado warning",
    ]


def test_nonzero_query_with_rows_is_partial_not_failed():
    assert classify_query_state(0, 5) == "completed"
    assert classify_query_state(0, 0) == "empty"
    assert classify_query_state(1, 5) == "partial"
    assert classify_query_state(1, 0) == "failed"
