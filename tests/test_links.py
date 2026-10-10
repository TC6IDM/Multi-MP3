from src import links

TEXT = """#https://open.spotify.com/playlist/AAA?si=1 metal?

# --- soundcloud ---
https://soundcloud.com/me/sets/gym-bangers?si=x gym bangers
[My Mix](https://www.youtube.com/playlist?list=PL1)
not a link at all
"""


def test_parse_reads_labels_toggles_and_markdown():
    entries = links.entries(TEXT)
    assert [(e.provider, e.enabled, e.label, e.markdown) for e in entries] == [
        ("spotify", False, "metal?", False),
        ("soundcloud", True, "gym bangers", False),
        ("youtube", True, "My Mix", True),
    ]
    assert entries[1].line == 3


def test_round_trip_without_edits_is_lossless():
    edited = [e.to_dict() for e in links.entries(TEXT)]
    assert links.apply_edits(TEXT, edited) == TEXT


def test_toggle_relabel_remove_and_add_keep_comments_in_place():
    entries = [e.to_dict() for e in links.entries(TEXT)]
    entries[0]["enabled"] = True                      # switch metal? on
    entries[1]["label"] = "GYM"                       # relabel
    del entries[2]                                    # remove the YouTube mix
    entries.append({"url": "https://soundcloud.com/x/sets/new", "label": "new one", "enabled": True})
    out = links.apply_edits(TEXT, entries)
    assert out.splitlines() == [
        "https://open.spotify.com/playlist/AAA?si=1 metal?",
        "",
        "# --- soundcloud ---",
        "https://soundcloud.com/me/sets/gym-bangers?si=x GYM",
        "not a link at all",
        "https://soundcloud.com/x/sets/new new one",
    ]


def test_invalid_urls_are_ignored():
    out = links.apply_edits("", [{"url": "https://example.com/nope", "label": "x"}])
    assert out == ""
