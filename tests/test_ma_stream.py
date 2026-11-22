"""Music Assistant stream-URL variant building (squeezelite / flow-mode etc.)."""

from __future__ import annotations

from hue_music_sync.audio.ma_stream import (
    as_track_list,
    attr_summary,
    iter_http_urls,
    library_track_url,
    ma_stream_variants,
    mapping_stream_urls,
    radiobrowser_stream_urls,
    radiobrowser_uuid,
)


class _Obj:
    """Minimal stand-in for an MA object exposing public attributes."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


# --- library pre-warm: per-track URL resolution ----------------------------

def test_library_track_url_builds_subsonic_stream():
    # A Navidrome/OpenSubsonic provider mapping resolves to a /rest/stream URL
    # built from the provider track id (the reliable pre-warm path).
    track = _Obj(provider_mappings=[
        _Obj(provider_domain="opensubsonic", item_id="track-42", url=None),
    ])
    url = library_track_url(track, ("http://nav:4533", "u", "p"))
    assert url is not None
    assert url.startswith("http://nav:4533/rest/stream.view?")
    assert "id=track-42" in url


def test_library_track_url_falls_back_to_http_mapping():
    track = _Obj(provider_mappings=[
        _Obj(provider_domain="filesystem", item_id="x", url="http://host/song.flac"),
    ])
    assert library_track_url(track, None) == "http://host/song.flac"


def test_library_track_url_none_when_unresolvable():
    track = _Obj(provider_mappings=[_Obj(provider_domain="spotify", item_id="x", url=None)])
    assert library_track_url(track, None) is None


def test_as_track_list_coerces_paged_results():
    assert as_track_list(None) == []
    assert as_track_list([1, 2]) == [1, 2]
    assert as_track_list(_Obj(items=[3, 4])) == [3, 4]


_BASE = "http://ma:8095"
_IDS = ("sess1", "queue1", "item1", "player1")


def _urls(variants):
    return [url for _k, _f, url in variants]


def test_flow_mode_player_builds_flow_url_first():
    # A squeezelite/flow-mode player must hit /flow/, not /single/.
    variants = ma_stream_variants(_BASE, *_IDS, flow_mode=True, codec="flac")
    assert _urls(variants)[0] == (
        "http://ma:8095/flow/sess1/queue1/item1/player1.flac"
    )
    assert variants[0][0] == "flow"


def test_single_mode_player_builds_single_url_first():
    variants = ma_stream_variants(_BASE, *_IDS, flow_mode=False, codec="flac")
    assert _urls(variants)[0] == (
        "http://ma:8095/single/sess1/queue1/item1/player1.flac"
    )


def test_non_flac_codec_is_used_in_the_extension():
    variants = ma_stream_variants(_BASE, *_IDS, flow_mode=True, codec="mp3")
    first = _urls(variants)[0]
    assert first.endswith("/flow/sess1/queue1/item1/player1.mp3")
    # ...and flac is still offered as a fallback variant.
    assert any(u.endswith(".flac") for u in _urls(variants))


def test_both_kinds_are_offered_as_fallbacks():
    kinds = {k for k, _f, _u in ma_stream_variants(_BASE, *_IDS, flow_mode=True, codec="flac")}
    assert kinds == {"flow", "single"}


def test_prefer_puts_a_known_working_variant_first():
    variants = ma_stream_variants(
        _BASE, *_IDS, flow_mode=True, codec="flac", prefer=("single", "wav")
    )
    assert variants[0] == (
        "single", "wav", "http://ma:8095/single/sess1/queue1/item1/player1.wav"
    )


def test_missing_ids_yield_no_variants():
    assert ma_stream_variants(_BASE, None, "q", "i", "p", flow_mode=False, codec="flac") == []
    assert ma_stream_variants(None, "s", "q", "i", "p", flow_mode=False, codec="flac") == []


def test_variants_are_unique():
    urls = _urls(ma_stream_variants(_BASE, *_IDS, flow_mode=True, codec="flac"))
    assert len(urls) == len(set(urls))


# --- HTTP-URL auto-discovery (the Sendspin / OpenSubsonic resolution path) ----

def test_iter_http_urls_finds_a_stream_url_field():
    # OpenSubsonic-style: .path is None, but the resolved stream URL lives in
    # another attribute - we must find it without knowing the field name.
    sd = _Obj(path=None, provider="opensubsonic--x", item_id="abc",
              url="http://nas:4533/rest/stream?id=abc&u=u&t=t")
    assert "http://nas:4533/rest/stream?id=abc&u=u&t=t" in list(iter_http_urls(sd))


def test_iter_http_urls_scans_nested_dicts():
    sd = _Obj(path=None, data={"stream_url": "https://host/track.mp3"})
    assert "https://host/track.mp3" in list(iter_http_urls(sd))


def test_mapping_stream_urls_finds_saved_tunein_station():
    # Issue #14: a saved TuneIn station has streamdetails.path=None and nothing
    # on the queue item; its stream URL is only on the provider mapping details.
    station = _Obj(provider_mappings=[
        _Obj(provider_domain="tunein", item_id="s88004",
             url="https://tunein.com/radio/s88004/",  # web page, not audio
             details="http://live.radiolumina.ro:8000/stream"),
    ])
    sd = _Obj(path=None, provider="tunein--GvJoKoeS", item_id="s88004")
    assert list(iter_http_urls(sd)) == []  # why it fell back before
    assert mapping_stream_urls(station) == ["http://live.radiolumina.ro:8000/stream"]


def test_mapping_stream_urls_skips_non_url_details():
    # TuneIn stores the bare preset id when it has no URL; Radio Browser
    # mappings carry no details at all.
    station = _Obj(provider_mappings=[
        _Obj(provider_domain="tunein", details="s88004"),
        _Obj(provider_domain="radiobrowser", details=None),
    ])
    assert mapping_stream_urls(station) == []
    assert mapping_stream_urls(None) == []


_ASCOR = "2f1fc325-c42c-4184-8d96-bfb5df2e9c95"


def test_radiobrowser_uuid_from_streamdetails_or_mapping():
    # Issue #14's Radio ASCOR log: sd_provider='radiobrowser', sd_path=None.
    sd = _Obj(path=None, provider="radiobrowser", item_id=_ASCOR)
    assert radiobrowser_uuid(sd, None) == _ASCOR
    station = _Obj(provider_mappings=[
        _Obj(provider_domain="radiobrowser", item_id=_ASCOR.upper(), details=None),
    ])
    assert radiobrowser_uuid(None, station) == _ASCOR


def test_radiobrowser_uuid_rejects_other_providers_and_bad_ids():
    assert radiobrowser_uuid(_Obj(provider="tunein--x", item_id=_ASCOR), None) is None
    bad = _Obj(provider="radiobrowser", item_id="../../json/stats")  # goes in a URL
    assert radiobrowser_uuid(bad, None) is None
    assert radiobrowser_uuid(None, None) is None


def test_radiobrowser_stream_urls_prefers_url_resolved():
    reply = [{"url": "http://a/playlist.pls", "url_resolved": "http://a:8000/live"}]
    assert radiobrowser_stream_urls(reply) == ["http://a:8000/live", "http://a/playlist.pls"]
    same = [{"url": "https://s/lisen", "url_resolved": "https://s/lisen"}]
    assert radiobrowser_stream_urls(same) == ["https://s/lisen"]
    assert radiobrowser_stream_urls([]) == []
    assert radiobrowser_stream_urls({"error": "x"}) == []
    assert radiobrowser_stream_urls([{"url": "", "url_resolved": None}]) == []


def test_iter_http_urls_empty_when_no_url():
    assert list(iter_http_urls(None)) == []
    assert list(iter_http_urls(_Obj(path=None, provider="opensubsonic"))) == []
    assert list(iter_http_urls(_Obj(uri="library://track/1097"))) == []  # not http


def test_attr_summary_is_compact_repr():
    s = attr_summary(_Obj(path=None, provider="opensubsonic--2tqKHCzo"))
    assert s == {"path": "None", "provider": "'opensubsonic--2tqKHCzo'"}
    assert attr_summary(None) == {}
