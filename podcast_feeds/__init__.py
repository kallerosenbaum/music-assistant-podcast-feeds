"""
Podcast Feeds Music Provider for Music Assistant.

One provider instance holds any number of podcast RSS feeds. The feed URLs are kept in
the provider's options (an editable list), and every feed becomes a podcast in the
library with all its episodes browsable and playable.

Feeds can be managed in three ways:
- edit the "Feed URLs" list in the provider settings,
- find a podcast with the search (by name in the Apple Podcasts directory, or by pasting
  its feed URL) and add it to the library,
- remove a podcast from the library, which drops its feed from the list.

Item ids:
- podcast: the (normalized) feed URL
- episode: "{feed_url} {guid_or_stream_url}", the default of parse_podcast_episode
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Iterator
from itertools import islice
from typing import TYPE_CHECKING, Any

import aiohttp
import podcastparser
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    AudioFormat,
    MediaItemImage,
    MediaItemTranscriptCue,
    Podcast,
    PodcastEpisode,
    ProviderMapping,
    SearchResults,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.countries import get_country_codes
from music_assistant.helpers.podcast_parsers import (
    enrich_episode_chapters,
    find_episode_stream_url,
    get_cached_podcast,
    get_episode_positions,
    get_stream_url_and_guid_from_episode,
    parse_podcast,
    parse_podcast_episode,
    refresh_cached_podcast,
)
from music_assistant.helpers.throttle_retry import ThrottlerManager, throttle_with_retries
from music_assistant.models.music_provider import MusicProvider

try:
    from music_assistant.helpers.podcast_parsers import (
        find_episode_transcripts,
        get_episode_transcript,
    )

    HAS_TRANSCRIPTS = True
except ImportError:
    # transcripts arrived in Music Assistant 2.11; older servers never ask for them
    HAS_TRANSCRIPTS = False

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.media_items import MediaItemType
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CONF_FEED_URLS = "feed_urls"
CONF_DIRECTORY_SEARCH = "directory_search"
CONF_COUNTRY = "country"

# Apple Podcasts directory (iTunes Search API), needs no account or key
ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
# Apple allows about 20 requests a minute, stay below like the itunes_podcasts provider
ITUNES_RATE_LIMIT = 18
ITUNES_RATE_PERIOD = 60
ITUNES_TIMEOUT = 10
# store to search when the server's region has no matching store
DEFAULT_COUNTRY = "us"
# the directory changes slowly; Music Assistant caches whole searches for minutes on top
DIRECTORY_CACHE_EXPIRATION = 60 * 60 * 24

# number of feeds fetched in parallel during a library sync
FEED_REFRESH_CONCURRENCY = 5

# url schemes podcast apps use for feed links; podcastparser rewrites them to http(s)
_FEED_URL_PREFIXES = ("http://", "https://", "feed://", "itpc://", "pcast://", "podcast://")

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.LIBRARY_PODCASTS,
    ProviderFeature.LIBRARY_PODCASTS_EDIT,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return PodcastFeedsProvider(mass, manifest, config, SUPPORTED_FEATURES)


def normalize_feed_url(url: str) -> str | None:
    """
    Return the canonical form of a podcast feed url, or None if it is not a feed url.

    :param url: A feed url as entered by the user.
    """
    url = url.strip()
    if not url.lower().startswith(_FEED_URL_PREFIXES):
        return None
    normalized: str | None = podcastparser.normalize_feed_url(url)
    # the episode item id separates feed url and episode part by a space
    if normalized is None or " " in normalized:
        return None
    return normalized


def restore_search_url(search_query: str) -> str:
    """
    Undo the server's search sanitizing for a query that is a url.

    The music controller replaces every "/" in a search query with a space before it
    reaches a provider, so a pasted "https://host/feed.xml" arrives as
    "https:  host feed.xml". A url never contains spaces, so turning them back into
    slashes restores it exactly. Any other query is returned unchanged.

    :param search_query: The search query as received by the provider.
    """
    query = search_query.strip()
    if query.lower().startswith(tuple(f"{p[:-2]}  " for p in _FEED_URL_PREFIXES)):
        return query.replace(" ", "/")
    return query


def split_episode_id(prov_episode_id: str) -> tuple[str, str]:
    """
    Split an episode item id into its feed url and its guid_or_stream_url part.

    :param prov_episode_id: The episode's item id, see the module docstring.
    """
    feed_url, sep, guid_or_stream_url = prov_episode_id.partition(" ")
    if not sep or not guid_or_stream_url:
        raise MediaNotFoundError(f"Invalid episode id: {prov_episode_id}")
    return feed_url, guid_or_stream_url


def iter_episodes(
    parsed_feed: dict[str, Any],
) -> Iterator[tuple[int, dict[str, Any], str]]:
    """
    Yield position, raw episode and episode id part of every playable episode, newest first.

    :param parsed_feed: A feed as parsed by podcastparser.
    """
    episodes: list[dict[str, Any]] = parsed_feed.get("episodes", [])
    # positions run oldest to newest and are derived from the feed's own listing order
    ranked = list(zip(get_episode_positions(episodes), episodes, strict=True))
    # list newest first, so callers after the latest episode only take the first one
    ranked.sort(key=lambda x: x[0], reverse=True)
    for position, episode in ranked:
        try:
            stream_url, guid = get_stream_url_and_guid_from_episode(episode=episode)
        except ValueError:
            # no playable enclosure
            continue
        yield position, episode, guid if guid is not None else stream_url


def parse_directory_result(result: dict[str, Any], instance_id: str, domain: str) -> Podcast | None:
    """
    Return a podcast for an Apple Podcasts directory search result, None without a feed url.

    The item id is the feed url, as for a subscribed podcast, so the result opens like any
    feed and adding it to the library stores the url in the feed list.

    :param result: One entry of the iTunes Search API "results" list.
    :param instance_id: The provider instance id.
    :param domain: The provider domain.
    """
    feed_url = normalize_feed_url(str(result.get("feedUrl") or ""))
    name = result.get("collectionName") or result.get("trackName")
    if feed_url is None or not name:
        return None
    podcast = Podcast(
        item_id=feed_url,
        name=str(name),
        publisher=result.get("artistName"),
        provider=instance_id,
        provider_mappings={
            ProviderMapping(
                item_id=feed_url,
                provider_domain=domain,
                provider_instance=instance_id,
            )
        },
    )
    artwork = next(
        (result[x] for x in ("artworkUrl600", "artworkUrl100", "artworkUrl60") if result.get(x)),
        None,
    )
    if artwork is not None:
        podcast.metadata.images = UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=str(artwork),
                    provider=instance_id,
                    remotely_accessible=True,
                )
            ]
        )
    if result.get("trackCount"):
        podcast.total_episodes = int(result["trackCount"])
    return podcast


class PodcastFeedsProvider(MusicProvider):
    """Music provider serving any number of podcast RSS feeds."""

    throttler: ThrottlerManager

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        country_codes = await asyncio.to_thread(get_country_codes)
        # the store decides which catalog is searched; default to the server's region
        region = self.mass.metadata.locale.split("_")[-1].upper()
        return (
            ConfigEntry(
                key=CONF_FEED_URLS,
                type=ConfigEntryType.STRING,
                multi_value=True,
                default_value=[],
                required=False,
            ),
            ConfigEntry(
                key=CONF_DIRECTORY_SEARCH,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                required=False,
            ),
            ConfigEntry(
                key=CONF_COUNTRY,
                type=ConfigEntryType.STRING,
                options=[
                    ConfigValueOption(title=name, value=code.lower())
                    for code, name in sorted(country_codes.items(), key=lambda x: x[1])
                ],
                default_value=region.lower() if region in country_codes else DEFAULT_COUNTRY,
                required=False,
                depends_on=CONF_DIRECTORY_SEARCH,
            ),
        )

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.throttler = ThrottlerManager(
            rate_limit=ITUNES_RATE_LIMIT, period=ITUNES_RATE_PERIOD, retry_attempts=1
        )
        invalid = [x for x in self._get_stored_feeds() if normalize_feed_url(x) is None]
        for url in invalid:
            self.logger.warning("Ignoring invalid podcast feed url: %s", url)

    @property
    def is_streaming_provider(self) -> bool:
        """
        Return False: the library holds exactly the feeds the user added.

        Feeds removed from the list are then fully removed from the library on sync.
        """
        return False

    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 10
    ) -> SearchResults:
        """
        Search podcasts by feed url, by the titles of their episodes, or in the directory.

        A feed url returns that podcast, which can then be added to the library (and so to
        this provider's feed list). Any other query returns the subscribed podcasts with an
        episode whose title contains every word of the query (search results have no place
        for episodes, so this leads to the podcast, where its episodes can be filtered),
        followed by the matching podcasts of the Apple Podcasts directory.
        """
        result = SearchResults()
        if MediaType.PODCAST not in media_types:
            return result
        query = restore_search_url(search_query)
        if (feed_url := normalize_feed_url(query)) is not None:
            try:
                result.podcasts = [await self.get_podcast(feed_url)]
            except MediaNotFoundError as err:
                self.logger.debug("Search: %s is not a usable podcast feed: %s", feed_url, err)
            return result
        subscribed, directory = await asyncio.gather(
            self._search_episode_titles(query, limit), self._search_directory(query, limit)
        )
        seen = {x.item_id for x in subscribed}
        result.podcasts = [*subscribed, *(x for x in directory if x.item_id not in seen)]
        return result

    async def library_add(self, item: MediaItemType) -> bool:
        """Add a podcast to the library by storing its feed url."""
        if not isinstance(item, Podcast):
            return await super().library_add(item)
        if (feed_url := normalize_feed_url(item.item_id)) is None:
            self.logger.warning("Cannot add %s, its id is not a feed url", item.name)
            return False
        stored = self._get_stored_feeds()
        if feed_url in {normalize_feed_url(x) for x in stored}:
            return False
        self.logger.debug("Adding podcast %s (%s) to library", item.name, feed_url)
        self._update_config_value(CONF_FEED_URLS, [*stored, feed_url])
        return True

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> bool:
        """Remove a podcast from the library by dropping its feed url."""
        if media_type != MediaType.PODCAST:
            return await super().library_remove(prov_item_id, media_type)
        stored = self._get_stored_feeds()
        remaining = [x for x in stored if normalize_feed_url(x) != prov_item_id]
        if len(remaining) != len(stored):
            self.logger.debug("Removing podcast feed %s from library", prov_item_id)
            self._update_config_value(CONF_FEED_URLS, remaining)
        return True

    async def get_library_podcasts(self) -> AsyncGenerator[Podcast]:
        """Retrieve all configured podcasts, refreshing their feeds."""
        async for feed_url, parsed_feed in self._refresh_feeds(self._get_feed_urls()):
            yield self._parse_podcast(feed_url, parsed_feed)

    async def get_podcast(self, prov_podcast_id: str) -> Podcast:
        """Get full podcast details by id (its feed url)."""
        parsed_feed = await self._get_feed(prov_podcast_id)
        return self._parse_podcast(prov_podcast_id, parsed_feed)

    async def get_podcast_episodes(self, prov_podcast_id: str) -> AsyncGenerator[PodcastEpisode]:
        """List all episodes of the podcast, newest first."""
        parsed_feed = await self._get_feed(prov_podcast_id)
        for position, episode, _ in iter_episodes(parsed_feed):
            if mass_episode := self._parse_episode(prov_podcast_id, parsed_feed, episode, position):
                yield mass_episode

    async def get_podcast_episode(self, prov_episode_id: str) -> PodcastEpisode:
        """Get (full) podcast episode details by id."""
        feed_url, guid_or_stream_url = split_episode_id(prov_episode_id)
        parsed_feed = await self._get_feed(feed_url)
        for position, episode, episode_part in iter_episodes(parsed_feed):
            if episode_part != guid_or_stream_url:
                continue
            if mass_episode := self._parse_episode(feed_url, parsed_feed, episode, position):
                await enrich_episode_chapters(
                    session=self.mass.http_session,
                    chapters_json_url=episode.get("chapters_json_url"),
                    mass_episode=mass_episode,
                )
                return mass_episode
            break
        raise MediaNotFoundError(f"Episode {prov_episode_id} not found")

    async def get_podcast_episode_transcript(
        self, prov_episode_id: str
    ) -> tuple[str | None, list[MediaItemTranscriptCue] | None]:
        """Get the transcript for a podcast episode."""
        if not HAS_TRANSCRIPTS:
            return None, None
        feed_url, guid_or_stream_url = split_episode_id(prov_episode_id)
        parsed_feed = await self._get_feed(feed_url)
        return await get_episode_transcript(
            mass=self.mass,
            provider_instance_id=self.instance_id,
            transcripts=find_episode_transcripts(
                parsed_feed=parsed_feed, guid_or_stream_url=guid_or_stream_url
            ),
        )

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a podcast episode."""
        feed_url, guid_or_stream_url = split_episode_id(item_id)
        parsed_feed = await self._get_feed(feed_url)
        stream_url = find_episode_stream_url(
            parsed_feed=parsed_feed, guid_or_stream_url=guid_or_stream_url
        )
        if stream_url is None:
            raise MediaNotFoundError(f"Episode {item_id} has no playable stream")
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(content_type=ContentType.try_parse(stream_url)),
            media_type=MediaType.PODCAST_EPISODE,
            stream_type=StreamType.HTTP,
            path=stream_url,
            can_seek=True,
            allow_seek=True,
        )

    def _get_stored_feeds(self) -> list[str]:
        """Return the feed urls exactly as stored in the config."""
        # typed loosely on purpose: a hand-edited config may hold anything
        value: Any = self.get_config_value(CONF_FEED_URLS, [])
        if not isinstance(value, list):
            return []
        return [str(x) for x in value if x]

    def _get_feed_urls(self) -> list[str]:
        """Return the normalized, de-duplicated feed urls, in the configured order."""
        feed_urls: dict[str, None] = {}
        for url in self._get_stored_feeds():
            if (feed_url := normalize_feed_url(url)) is not None:
                feed_urls[feed_url] = None
        return list(feed_urls)

    async def _get_feed(self, feed_url: str) -> dict[str, Any]:
        """
        Return the parsed feed from cache, fetching it when needed.

        Any feed url is accepted, not only the configured ones, so a podcast found by
        searching its url can be shown before it is added to the library.
        """
        if normalize_feed_url(feed_url) != feed_url:
            raise MediaNotFoundError(f"Not a podcast feed url: {feed_url}")
        # raises MediaNotFoundError when the feed cannot be retrieved or parsed
        return await get_cached_podcast(
            mass=self.mass,
            provider_instance_id=self.instance_id,
            feed_url=feed_url,
        )

    async def _search_episode_titles(self, query: str, limit: int) -> list[Podcast]:
        """
        Return the subscribed podcasts with an episode title containing every query word.

        Podcasts are returned in the configured order. A feed that cannot be loaded is
        skipped, so one broken feed does not break the search.

        :param query: The search query; matching ignores case.
        :param limit: Maximum number of podcasts to return.
        """
        words = query.casefold().split()
        if not words or limit <= 0:
            return []
        feed_urls = self._get_feed_urls()
        # read concurrently: a feed missing from the cache is fetched over the network
        feeds = await asyncio.gather(
            *(self._get_feed(x) for x in feed_urls), return_exceptions=True
        )
        podcasts: list[Podcast] = []
        for feed_url, parsed_feed in zip(feed_urls, feeds, strict=True):
            if isinstance(parsed_feed, BaseException):
                if not isinstance(parsed_feed, MediaNotFoundError):
                    raise parsed_feed
                self.logger.debug("Search: skipping feed %s: %s", feed_url, parsed_feed)
                continue
            titles = (str(x.get("title") or "").casefold() for x in parsed_feed.get("episodes", []))
            if any(all(word in title for word in words) for title in titles):
                podcasts.append(self._parse_podcast(feed_url, parsed_feed))
                if len(podcasts) >= limit:
                    break
        return podcasts

    async def _search_directory(self, query: str, limit: int) -> list[Podcast]:
        """
        Return the podcasts the Apple Podcasts directory finds for the query.

        Returns nothing when directory search is turned off, and on any failure, so the
        directory being unreachable does not break the search of the subscribed feeds.

        :param query: The search query.
        :param limit: Maximum number of podcasts to return.
        """
        if not query.strip() or limit <= 0 or not self.config.get_value(CONF_DIRECTORY_SEARCH):
            return []
        country = str(self.config.get_value(CONF_COUNTRY) or DEFAULT_COUNTRY)
        try:
            results = await self._itunes_search(query, country, min(limit, 200))
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            self.logger.warning("Apple Podcasts directory search failed: %s", err)
            return []
        podcasts = (parse_directory_result(x, self.instance_id, self.domain) for x in results or [])
        return [x for x in podcasts if x is not None]

    @use_cache(DIRECTORY_CACHE_EXPIRATION, cache_none=False)
    @throttle_with_retries
    async def _itunes_search(
        self, query: str, country: str, limit: int
    ) -> list[dict[str, Any]] | None:
        """Return the raw iTunes Search API results, None on a failed request (not cached)."""
        params: dict[str, str | int] = {
            "media": "podcast",
            "entity": "podcast",
            "country": country,
            "limit": limit,
            "term": query,
        }
        async with self.mass.http_session.get(
            ITUNES_SEARCH_URL, params=params, timeout=aiohttp.ClientTimeout(total=ITUNES_TIMEOUT)
        ) as response:
            if response.status != 200:
                self.logger.warning("Apple Podcasts directory search returned %s", response.status)
                return None
            # Apple serves the json as text/javascript
            data: Any = await response.json(content_type=None)
        results = data.get("results") if isinstance(data, dict) else None
        return [x for x in results if isinstance(x, dict)] if isinstance(results, list) else None

    async def _refresh_feeds(
        self, feed_urls: list[str]
    ) -> AsyncGenerator[tuple[str, dict[str, Any]]]:
        """Refresh the cached feeds a few at a time, yielding each as soon as it is refreshed."""

        def refresh(feed_url: str) -> asyncio.Task[dict[str, Any]]:
            # tracked by mass, so a shutdown cancels it; its errors are handled below
            return self.mass.create_task(
                refresh_cached_podcast(
                    mass=self.mass,
                    provider_instance_id=self.instance_id,
                    feed_url=feed_url,
                ),
                task_name="podcast_feeds_refresh",
                log_exceptions=False,
            )

        pending = iter(feed_urls)
        refreshing = {refresh(x): x for x in islice(pending, FEED_REFRESH_CONCURRENCY)}
        try:
            while refreshing:
                done, _ = await asyncio.wait(refreshing, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    feed_url = refreshing.pop(task)
                    if (next_feed_url := next(pending, None)) is not None:
                        refreshing[refresh(next_feed_url)] = next_feed_url
                    try:
                        parsed_feed = task.result()
                    except MediaNotFoundError as err:
                        # one broken feed must not fail the sync of all the others
                        self.report_skipped_sync_item(MediaType.PODCAST, feed_url, err)
                        continue
                    yield feed_url, parsed_feed
        finally:
            for task in refreshing:
                task.cancel()

    def _parse_podcast(self, feed_url: str, parsed_feed: dict[str, Any]) -> Podcast:
        return parse_podcast(
            feed_url=feed_url,
            parsed_feed=parsed_feed,
            instance_id=self.instance_id,
            domain=self.domain,
        )

    def _parse_episode(
        self,
        feed_url: str,
        parsed_feed: dict[str, Any],
        episode: dict[str, Any],
        position: int,
    ) -> PodcastEpisode | None:
        return parse_podcast_episode(
            episode=episode,
            prov_podcast_id=feed_url,
            position=position,
            podcast_cover=parsed_feed.get("cover_url"),
            podcast_name=parsed_feed.get("title"),
            instance_id=self.instance_id,
            domain=self.domain,
        )
