from modules import movies as mv


def test_results_keep_only_movies_and_series():
    data = {"results": [
        {"id": 1, "media_type": "movie", "title": "A", "release_date": "2020-05-01", "vote_average": 7.84},
        {"id": 2, "media_type": "person", "name": "Someone"},
        {"id": 3, "media_type": "tv", "name": "B", "first_air_date": "2019-01-01", "vote_average": 8},
        {"media_type": "movie", "title": "no id"},
    ]}
    r = mv.parse_results(data)
    assert [x["id"] for x in r] == [1, 3]
    assert r[0]["year"] == "2020" and r[0]["rating"] == 7.8 and r[1]["type"] == "tv"


def test_details_trailer_cast_and_regional_providers():
    data = {"id": 9, "title": "T", "release_date": "2021-02-02", "overview": "x" * 900, "vote_average": 7.0, "runtime": 120,
            "genres": [{"name": "Drama"}], "poster_path": "/p.jpg",
            "credits": {"cast": [{"name": f"C{i}"} for i in range(8)]},
            "videos": {"results": [{"site": "YouTube", "type": "Teaser", "key": "no"}, {"site": "YouTube", "type": "Trailer", "key": "yes"}]},
            "watch/providers": {"results": {"GH": {"flatrate": [{"provider_name": "Netflix"}], "link": "https://jw/x"}}}}
    d = mv.parse_details(data, "movie", "gh")
    assert d["trailer"].endswith("yes") and len(d["cast"]) == 5 and len(d["overview"]) == 700
    assert d["stream"] == ["Netflix"] and d["watch_link"] == "https://jw/x" and d["poster"].endswith("/p.jpg")
    assert mv.parse_details(data, "movie", "US")["stream"] == []      # another country has no listing


def test_region_falls_back_to_default():
    assert mv.valid_region("ng") == "NG" and mv.valid_region("zz") == "US" and mv.valid_region(None) == "US"


def test_archive_results_link_to_the_archive_only():
    data = {"response": {"docs": [{"identifier": "nosferatu 1922", "title": "Nosferatu", "year": 1922}, {"title": "no id"}]}}
    r = mv.parse_archive(data)
    assert len(r) == 1 and r[0]["url"] == "https://archive.org/details/nosferatu%201922"


def test_movies_and_meme_items_registered():
    from discord_bot.cogs import _views_join_dm as jd
    from discord_bot.cogs import memes, movies
    assert jd._MovieButton in jd.DYNAMIC_ITEMS
    assert len([i for i in jd.DYNAMIC_ITEMS if i.__discord_ui_compiled_template__.match("join_dm_movie:7:-")]) == 1
    ids = [i.__discord_ui_compiled_template__.pattern for i in movies.DYNAMIC_ITEMS + memes.DYNAMIC_ITEMS]
    assert len(ids) == len(set(ids))                                   # no two persistent items share a custom id


def test_setups_are_owner_locked_wizards_like_the_anime_spawn_one():
    from discord_bot.cogs import memes, movies
    for mod, view in ((memes, memes.MemeSetupView), (movies, movies.MovieSetupView)):
        assert callable(mod.open_setup) and view.__mro__[1].__name__ == "LayoutView"
        assert "interaction_check" in view.__dict__ and "_on_quick" in view.__dict__ and "on_timeout" in view.__dict__
    # only things that live in the server stay persistent; the setup itself is no longer a standing panel
    assert [i.__name__ for i in memes.DYNAMIC_ITEMS] == ["MemeRoleToggle"]
    assert {"SearchButton", "TrendingButton", "ClassicsButton", "RoleToggleButton"} == {i.__name__ for i in movies.DYNAMIC_ITEMS}
