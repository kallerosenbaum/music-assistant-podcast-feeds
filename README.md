# Podcast Feeds – Music Assistant provider

A music provider for [Music Assistant](https://music-assistant.io) that subscribes to **any
number of podcast RSS feeds in a single provider instance**. Every feed becomes a podcast in
your library, and all its episodes can be browsed and played (with chapters and transcripts
when the feed has them).

The built-in "Podcast RSS Feed" provider takes one feed per instance; this one holds them all.

## Layout

```
podcast_feeds/                         -> music_assistant/providers/podcast_feeds/
tests/providers/podcast_feeds/         -> tests/providers/podcast_feeds/
```

## Install

Copy `podcast_feeds/` into the server's `music_assistant/providers/` directory and restart the
server. In Docker, mount it instead, for example:

```yaml
volumes:
  - ./podcast_feeds:/app/venv/lib/python3.14/site-packages/music_assistant/providers/podcast_feeds
```

(Check the actual `site-packages` path inside your image, for example with
`python -c "import music_assistant, os; print(os.path.dirname(music_assistant.__file__))"`.)

It has no extra requirements: it uses `podcastparser` and the shared
`music_assistant.helpers.podcast_parsers` module that Music Assistant already ships.

### Settings labels

The server doesn't read a provider's `strings.json` at runtime. It reads labels from one
compiled file, `music_assistant/translations/en.json`. Without this step the Feed URLs field
still works, but shows no label or help text.

- **Running from a source checkout:** after copying the provider in, run this from the
  checkout root, then restart the server:

  ```sh
  uv run python -m scripts.build_translations
  ```

- **Docker or another packaged install:** the build script isn't included in the image. Check
  out the server source at the same version as your image, copy `podcast_feeds/` into it and
  run the command above. Then mount the generated file over the image's copy as well:

  ```yaml
  volumes:
    - ./en.json:/app/venv/lib/python3.14/site-packages/music_assistant/translations/en.json
  ```

  Rebuild `en.json` whenever you upgrade the server, or the other providers' labels can drift.

## Use

1. **Settings → Music providers → Add → Podcast Feeds.**
2. Open the provider's settings and enter your feeds in **Feed URLs** (press Enter after each
   URL). Saving reloads the provider and syncs the library.
3. The podcasts show up under **Podcasts** and in **Browse → Podcast Feeds**.

You can also:

- **Find new podcasts:** search by name. Matching podcasts from the Apple Podcasts directory
  are listed after your own. Open one to see its episodes, and add it to your library to
  subscribe: its feed URL is appended to Feed URLs. Set the store in **Directory country**
  (it defaults to your server's region). If you'd rather not send search terms to Apple,
  turn off **Search the Apple Podcasts directory**. Directory results are cached for a day.
- **Add by URL:** paste a feed URL into the search box and add the resulting podcast to
  your library. Use this for feeds the directory doesn't list.
- **Remove:** remove the podcast from your library, or delete its URL from Feed URLs.
- **Find an episode:** search for words from its title. Music Assistant's search has no place
  for episodes, so the results list the *podcasts* that have an episode whose title contains
  every word you typed (any order, any case). Open the podcast and use the search icon above
  its episode list to get to the episode itself. Show notes aren't searched.

`feed://`, `itpc://`, `pcast://` and `podcast://` links are accepted too and are converted to
`http(s)`. Feeds are cached for 24 hours and refreshed on every library sync. A feed that fails
to load is skipped and reported; the rest still sync.

## Tests

From a Music Assistant server checkout with this provider copied in:

```sh
uv run pytest tests/providers/podcast_feeds
```

