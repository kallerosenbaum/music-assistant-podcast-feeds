"""Tests for the Podcast Feeds provider."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers.podcast_feeds import (
    CONF_FEED_URLS,
    SUPPORTED_FEATURES,
    PodcastFeedsProvider,
    normalize_feed_url,
    restore_search_url,
    split_episode_id,
)


def _as_server_sends(query: str) -> str:
    """Return the query as the music controller passes it to a provider search."""
    return query.replace("/", " ").replace("'", "")


FEED_A = "https://example.com/a.xml"
FEED_B = "https://example.org/b.xml"


def _episode(guid: str, published: int) -> dict[str, Any]:
    """Return a minimal podcastparser episode dict with a valid enclosure."""
    return {
        "guid": guid,
        "title": f"Episode {guid}",
        "published": published,
        "enclosures": [{"url": f"https://cdn.example.com/{guid}.mp3"}],
    }


FEEDS: dict[str, dict[str, Any]] = {
    FEED_A: {
        "title": "Show A",
        "episodes": [_episode("a2", 200), _episode("a3", 300), _episode("a1", 100)],
    },
    FEED_B: {"title": "Show B", "episodes": [_episode("b1", 100)]},
}


def _provider(feed_urls: list[str]) -> PodcastFeedsProvider:
    """Return a provider configured with the given feed urls, without a real mass."""
    mass = Mock()
    manifest = Mock(domain="podcast_feeds")
    config = Mock(instance_id="podcast_feeds--test", enabled=True)
    config.values = {}
    stored = {CONF_FEED_URLS: feed_urls}
    config.get_value.side_effect = lambda key, default=None: (
        "INFO" if key == "log_level" else stored.get(key, default)
    )
    provider = PodcastFeedsProvider(mass, manifest, config, SUPPORTED_FEATURES)
    provider._update_config_value = Mock(  # type: ignore[method-assign]
        side_effect=lambda key, value, **_: stored.__setitem__(key, value)
    )
    return provider


async def _fake_get_cached_podcast(*, feed_url: str, **_: Any) -> dict[str, Any]:
    if feed_url not in FEEDS:
        raise MediaNotFoundError(feed_url)
    return FEEDS[feed_url]


@pytest.fixture(autouse=True)
def _mock_feed_fetch() -> Any:
    """Serve FEEDS instead of fetching over the network."""
    with (
        patch(
            "music_assistant.providers.podcast_feeds.get_cached_podcast",
            side_effect=_fake_get_cached_podcast,
        ),
        patch(
            "music_assistant.providers.podcast_feeds.refresh_cached_podcast",
            side_effect=_fake_get_cached_podcast,
        ),
        patch(
            "music_assistant.providers.podcast_feeds.enrich_episode_chapters",
            new=AsyncMock(),
        ),
    ):
        yield


def test_normalize_feed_url() -> None:
    """Feed urls are normalized, anything else is rejected."""
    assert normalize_feed_url(f"  {FEED_A} ") == FEED_A
    assert normalize_feed_url("feed://example.com/a.xml") == "http://example.com/a.xml"
    assert normalize_feed_url("some podcast name") is None
    assert normalize_feed_url("") is None


def test_restore_search_url() -> None:
    """A url mangled by the server's search sanitizing is restored; other text is not."""
    url = "https://feeds.npr.org/510289/podcast.xml?auth=x&y=1"
    assert restore_search_url(_as_server_sends(url)) == url
    assert restore_search_url(" feed:  example.com a.xml ") == "feed://example.com/a.xml"
    assert restore_search_url("planet money") == "planet money"
    assert restore_search_url("https is great") == "https is great"


def test_split_episode_id() -> None:
    """The episode id splits into feed url and episode part."""
    assert split_episode_id(f"{FEED_A} a1") == (FEED_A, "a1")
    with pytest.raises(MediaNotFoundError):
        split_episode_id(FEED_A)


async def test_library_lists_every_feed() -> None:
    """Every configured feed becomes a podcast; duplicates and invalid urls are skipped."""
    provider = _provider([FEED_A, "not a url", FEED_B, f"{FEED_A} "])
    provider.mass.create_task = lambda coro, **_: asyncio.ensure_future(coro)
    podcasts = [p async for p in provider.get_library_podcasts()]
    assert sorted(p.item_id for p in podcasts) == [FEED_A, FEED_B]


async def test_broken_feed_does_not_fail_sync() -> None:
    """A feed that cannot be fetched is reported and skipped."""
    broken = "https://example.net/broken.xml"
    provider = _provider([broken, FEED_B])
    provider.mass.create_task = lambda coro, **_: asyncio.ensure_future(coro)
    provider.report_skipped_sync_item = Mock()  # type: ignore[method-assign]
    podcasts = [p async for p in provider.get_library_podcasts()]
    assert [p.item_id for p in podcasts] == [FEED_B]
    provider.report_skipped_sync_item.assert_called_once()


async def test_episodes_newest_first_with_positions() -> None:
    """Episodes are listed newest first, positions run oldest to newest."""
    provider = _provider([FEED_A])
    episodes = [e async for e in provider.get_podcast_episodes(FEED_A)]
    assert [e.item_id for e in episodes] == [f"{FEED_A} a3", f"{FEED_A} a2", f"{FEED_A} a1"]
    assert [e.position for e in episodes] == [3, 2, 1]
    assert {e.podcast.name for e in episodes} == {"Show A"}


async def test_get_episode_and_stream() -> None:
    """An episode id resolves to its details and stream url."""
    provider = _provider([FEED_A, FEED_B])
    episode = await provider.get_podcast_episode(f"{FEED_B} b1")
    assert episode.name == "Episode b1"
    details = await provider.get_stream_details(f"{FEED_B} b1", MediaType.PODCAST_EPISODE)
    assert details.path == "https://cdn.example.com/b1.mp3"
    with pytest.raises(MediaNotFoundError):
        await provider.get_podcast_episode(f"{FEED_B} nope")


async def test_search_by_feed_url() -> None:
    """Searching a feed url returns that podcast; other queries return nothing."""
    provider = _provider([])
    result = await provider.search(FEED_B, [MediaType.PODCAST])
    assert [p.name for p in result.podcasts] == ["Show B"]
    # the music controller replaces "/" with spaces before calling the provider
    result = await provider.search(_as_server_sends(FEED_B), [MediaType.PODCAST])
    assert [p.item_id for p in result.podcasts] == [FEED_B]
    assert not (await provider.search("show b", [MediaType.PODCAST])).podcasts


async def test_library_add_and_remove() -> None:
    """Adding a podcast stores its feed url; removing it drops the url again."""
    provider = _provider([FEED_A])
    podcast = await provider.get_podcast(FEED_B)
    assert await provider.library_add(podcast)
    assert not await provider.library_add(podcast)  # already there
    assert provider._get_feed_urls() == [FEED_A, FEED_B]
    assert await provider.library_remove(FEED_A, MediaType.PODCAST)
    assert provider._get_feed_urls() == [FEED_B]
