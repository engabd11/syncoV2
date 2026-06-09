"""Music Assistant stream-URL variant building (squeezelite / flow-mode etc.)."""

from __future__ import annotations

from hue_music_sync.audio.ma_stream import ma_stream_variants

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
