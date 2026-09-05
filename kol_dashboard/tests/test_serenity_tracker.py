from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
LIB_DIR = ROOT / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

import serenity_tracker  # noqa: E402


TWEET_ID = "2096149203278037407"
PUBLISHED_MS = 1_788_595_889_000
NOW = datetime(2026, 9, 6, tzinfo=timezone.utc)


def rsc_fixture(
    *,
    handle: str = "aleabitoreddit",
    linked_handle: str | None = None,
    rest_id: str = TWEET_ID,
    published_ms: int = PUBLISHED_MS,
) -> str:
    linked_handle = handle if linked_handle is None else linked_handle
    return f'''
    <div data-href="/{linked_handle}/status/{TWEET_ID}"></div>
    entry_id:"tweet-{TWEET_ID}",
    "TweetResults:{TWEET_ID}":$R[1]={{
      __id:"TweetResults:{TWEET_ID}",__typename:"TweetResults",
      rest_id:"{rest_id}",result:$R[2]={{__ref:"tweet-ref"}}
    }},
    "tweet-ref":$R[3]={{
      __id:"tweet-ref",__typename:"Tweet",rest_id:"{TWEET_ID}",
      core:$R[4]={{__ref:"client:tweet:core"}},
      details:$R[5]={{__ref:"client:tweet:details"}}
    }},
    "client:tweet:core":$R[6]={{
      __id:"client:tweet:core",__typename:"TweetCore",
      user_results:$R[7]={{__ref:"user-results"}}
    }},
    "user-results":$R[8]={{
      __id:"user-results",__typename:"UserResults",
      result:$R[9]={{__ref:"user"}}
    }},
    "user":$R[10]={{
      __id:"user",__typename:"User",
      core:$R[11]={{__ref:"client:user:core"}}
    }},
    "client:user:core":$R[12]={{
      __id:"client:user:core",__typename:"UserCore",
      screen_name:"{handle}",name:"Serenity"
    }},
    "client:tweet:details":$R[13]={{
      __id:"client:tweet:details",__typename:"TBirdData",
      full_text:"Memory demand improved.\\n\\n$MU and $SNDK remain important.",
      created_at_ms:{published_ms}
    }}
    '''


class SerenityTrackerTests(unittest.TestCase):
    def test_current_rsc_tweet_is_resolved_with_author_time_and_url(self) -> None:
        tweets = serenity_tracker.parse_tweets(rsc_fixture(), now=NOW)

        self.assertEqual(len(tweets), 1)
        self.assertEqual(tweets[0]["tid"], TWEET_ID)
        self.assertEqual(tweets[0]["handle"], "aleabitoreddit")
        self.assertEqual(
            tweets[0]["url"],
            f"https://x.com/aleabitoreddit/status/{TWEET_ID}",
        )
        self.assertEqual(
            tweets[0]["published_at"], "2026-09-05T08:11:29+00:00"
        )
        self.assertEqual(
            tweets[0]["text"],
            "Memory demand improved. $MU and $SNDK remain important.",
        )

    def test_rsc_rejects_wrong_author_or_profile_status_url(self) -> None:
        wrong_author = rsc_fixture(handle="differentuser")
        wrong_link = rsc_fixture(linked_handle="differentuser")

        self.assertEqual(
            serenity_tracker.parse_tweets(
                wrong_author, handle="aleabitoreddit", now=NOW
            ),
            [],
        )
        self.assertEqual(
            serenity_tracker.parse_tweets(wrong_link, now=NOW),
            [],
        )

    def test_rsc_rejects_mismatched_id_and_future_timestamp(self) -> None:
        mismatched = rsc_fixture(rest_id="2096149203278037408")
        future_ms = int(datetime(2026, 9, 7, tzinfo=timezone.utc).timestamp() * 1000)

        self.assertEqual(serenity_tracker.parse_tweets(mismatched, now=NOW), [])
        self.assertEqual(
            serenity_tracker.parse_tweets(
                rsc_fixture(published_ms=future_ms), now=NOW
            ),
            [],
        )

    def test_legacy_dom_parser_remains_supported(self) -> None:
        legacy = f'''
        <article data-tweet-id="{TWEET_ID}">
          <div dir="auto" class="font-chirp max-w-full whitespace-pre-wrap break-words text-text text-body font-normal">
            Legacy parser still captures this sufficiently long market post.
          </div>
          <a href="/aleabitoreddit/status/{TWEET_ID}">2h</a>
        </article>
        '''

        tweets = serenity_tracker.parse_tweets(legacy, now=NOW)

        self.assertEqual(len(tweets), 1)
        self.assertEqual(tweets[0]["tid"], TWEET_ID)
        self.assertEqual(tweets[0]["date"], "2h")

    def test_fetch_tweets_raises_observable_empty_parse_error(self) -> None:
        with mock.patch.object(
            serenity_tracker, "fetch_page", return_value="unrecognized payload"
        ):
            with self.assertRaises(serenity_tracker.SourceParseError) as caught:
                serenity_tracker.fetch_tweets("aleabitoreddit")

        self.assertEqual(caught.exception.code, "source_parse_empty")
        self.assertEqual(str(caught.exception), "source_parse_empty")

    def test_invalid_handle_is_rejected_before_building_a_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid_x_handle"):
            serenity_tracker.fetch_page("someone/else")


if __name__ == "__main__":
    unittest.main()
