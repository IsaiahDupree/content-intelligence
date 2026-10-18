from inspect import signature

from services.platform_adapters.tiktok import TikTokAdapter


def test_tiktok_adapter_fetch_comments_matches_platform_interface():
    parameter = signature(TikTokAdapter.fetch_comments).parameters[
        "platform_post_id"
    ]

    assert parameter.annotation is str
