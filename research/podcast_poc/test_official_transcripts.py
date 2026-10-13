"""Synthetic publisher structures only: no copied copyrighted transcript text."""
import unittest

from research.podcast_poc.official_transcripts import (
    TranscriptUnavailable, find_transcript_url, parse_transcript,
    parse_webvtt, validate_transcript_url,
)


SPEECH = "A synthetic explanation describes how an example system measures useful work and checks each result before continuing. "
DWARKESH = "https://www.dwarkesh.com/p/synthetic-interview"
LEX = "https://lexfridman.com/synthetic-guest-transcript"
DIARY = "https://rss.flightcast.com/transcripts/EXAMPLE123.vtt"


def stamp(index):
    return f"00:{index // 2:02d}:{30 * (index % 2):02d}"


def dwarkesh(*, chapter=False, count=24):
    paragraphs = []
    for i in range(count):
        if chapter and i % 4 == 0:
            paragraphs.append(f"<h3>{stamp(i)} - Synthetic chapter</h3>")
        if chapter:
            paragraphs.append(f"<p><strong>Speaker {i % 2}</strong></p><p>{SPEECH}</p>")
        else:
            paragraphs.append(f"<p>Speaker {i % 2} ({stamp(i)})</p><p>{SPEECH}</p>")
    return '<div class="body markup"><h2>Show notes</h2><p>not speech</p><h2><strong>Transcript</strong></h2>' + ''.join(paragraphs) + '</div>'


def lex(count=24):
    return '<div class="entry-content">' + ''.join(
        f'<div class="ts-segment"><span class="ts-name">Speaker {i % 2}</span>'
        f'<span class="ts-timestamp"><a href="https://example.invalid">({stamp(i)})</a></span>'
        f'<span class="ts-text">{SPEECH}</span></div>' for i in range(count)) + '</div>'


def vtt(count=24):
    return 'WEBVTT\n\n' + '\n\n'.join(
        f'{i + 1}\n{stamp(i)}.000 --> {stamp(i)}.900\n<v Speaker {i % 2}>{SPEECH}</v>'
        for i in range(count))


class OfficialTranscriptTests(unittest.TestCase):
    def test_final_urls_are_fixed_official_paths(self):
        for source, url in [('dwarkesh', DWARKESH), ('lex', LEX), ('diary', DIARY)]:
            self.assertEqual(validate_transcript_url(source, url + '?utm_source=test#content'), url)
        invalid = [
            ('dwarkesh', 'http://www.dwarkesh.com/p/synthetic-interview'),
            ('dwarkesh', 'https://www.dwarkeshpatel.com/p/synthetic-interview'),
            ('dwarkesh', 'https://www.dwarkesh.com/p/../api'),
            ('dwarkesh', 'https://www.dwarkesh.com/p/example%2fother'),
            ('lex', 'https://lexfridman.com/synthetic-guest'),
            ('lex', 'https://lexfridman.com.evil.invalid/synthetic-guest-transcript'),
            ('lex', 'https://user@lexfridman.com/synthetic-guest-transcript'),
            ('lex', 'https://lexfridman.com:443/synthetic-guest-transcript'),
            ('lex', 'https://lexfridman.com:bad/synthetic-guest-transcript'),
            ('diary', 'https://rss.flightcast.com/other/EXAMPLE123.vtt'),
            ('diary', 'https://rss.flightcast.com/transcripts/EXAMPLE123.mp3'),
            ('unsupported', DWARKESH),
        ]
        for source, url in invalid:
            with self.subTest(url=url), self.assertRaises(TranscriptUnavailable):
                validate_transcript_url(source, url)

    def test_lex_follows_real_episode_link_and_strips_tracking(self):
        html = '<a href="/other-transcript">Related</a><a href="/synthetic-guest-transcript/?a=b#top">Transcript</a><a href="/synthetic-guest-transcript">Transcript again</a>'
        self.assertEqual(find_transcript_url('lex', 'https://lexfridman.com/synthetic-guest/?utm=test', html), LEX + '/')

    def test_lex_preserves_only_actual_link_variant(self):
        self.assertEqual(find_transcript_url('lex', 'https://lexfridman.com/synthetic-guest/',
                         '<a href="/synthetic-guest-transcript">Transcript</a>'), LEX)

    def test_lex_does_not_guess_missing_or_related_transcript_url(self):
        for html in ['', '<a href="/other-transcript">Transcript</a>',
                     '<a href="https://evil.invalid/synthetic-guest-transcript">Transcript</a>']:
            with self.assertRaises(TranscriptUnavailable):
                find_transcript_url('lex', 'https://lexfridman.com/synthetic-guest/', html)

    def test_dwarkesh_speaker_timestamps_and_continuations(self):
        result = parse_transcript('dwarkesh', dwarkesh(), source_url=DWARKESH)
        self.assertEqual(len(result['segments']), 24)
        self.assertEqual(result['segments'][0]['id'], 's0001')
        self.assertEqual(result['segments'][0]['end_seconds'], 30)
        self.assertEqual(result['segments'][-1]['end_seconds'], None)
        self.assertNotIn('not speech', str(result['segments']))
        self.assertEqual(result['source_kind'], 'official_edited_transcript')
        self.assertIn('未独立核对', result['coverage']['note'])
        self.assertEqual(result['transcript_url'], DWARKESH)

    def test_dwarkesh_time_first_format(self):
        html = dwarkesh().replace('Speaker 0 (00:00:00)', '[00:00:00] Speaker 0:')
        result = parse_transcript('dwarkesh', html, source_url=DWARKESH)
        self.assertEqual(result['segments'][0]['speaker'], 'Speaker 0')

    def test_dwarkesh_chapter_only_anchors_are_explicit(self):
        result = parse_transcript('dwarkesh', dwarkesh(chapter=True), source_url=DWARKESH)
        self.assertEqual(len(result['segments']), 24)
        self.assertEqual([s['start_seconds'] for s in result['segments'][:4]], [0] * 4)
        self.assertEqual([s['end_seconds'] for s in result['segments'][:4]], [120] * 4)
        self.assertEqual(result['segments'][0]['timestamp_precision'], 'chapter')
        self.assertIn('仅为官方章节锚点', result['coverage']['note'])

    def test_dwarkesh_requires_real_heading_not_prose_transcript_mention(self):
        for html in [dwarkesh().replace('<h2><strong>Transcript</strong></h2>', '<p>Transcript</p>'),
                     '<h2>Transcript</h2>' + dwarkesh().replace('<h2><strong>Transcript</strong></h2>', '')]:
            with self.assertRaises(TranscriptUnavailable):
                parse_transcript('dwarkesh', html, source_url=DWARKESH)

    def test_dwarkesh_chapter_list_and_short_body_are_not_transcripts(self):
        chapters = ''.join(f'<h3>{stamp(i)} - Chapter number {i}</h3>' for i in range(30))
        for html in [f'<div class="body markup"><h2>Transcript</h2>{chapters}</div>',
                     dwarkesh(count=19), dwarkesh().replace(SPEECH, 'Short.'),
                     dwarkesh().replace('Speaker 0', 'A different unique chapter heading')
                               .replace('Speaker 1', 'Another heading') .replace(SPEECH, '')]:
            with self.assertRaises(TranscriptUnavailable):
                parse_transcript('dwarkesh', html, source_url=DWARKESH)

    def test_dwarkesh_excludes_footer_and_script(self):
        html = dwarkesh().replace(SPEECH, SPEECH + '<script>UNTRUSTED SCRIPT</script>', 1)
        html = html.replace('</div>', '<h2>References</h2><p>UNRELATED FOOTER</p></div>')
        result = parse_transcript('dwarkesh', html, source_url=DWARKESH)
        self.assertNotIn('UNTRUSTED', str(result))
        self.assertNotIn('UNRELATED', str(result))

    def test_dwarkesh_rejects_regressing_timestamp_or_untimed_speaker(self):
        for html in [dwarkesh().replace('00:01:30', '00:00:10'),
                     dwarkesh(chapter=True).replace('00:04:00', '00:00:10'),
                     dwarkesh(chapter=True).replace('<h3>00:00:00 - Synthetic chapter</h3>', '')]:
            with self.assertRaises(TranscriptUnavailable):
                parse_transcript('dwarkesh', html, source_url=DWARKESH)

    def test_lex_uses_actual_structured_speech(self):
        result = parse_transcript('lex', lex(), source_url=LEX)
        self.assertEqual(len(result['segments']), 24)
        self.assertEqual(result['segments'][1]['start_seconds'], 30)
        self.assertEqual(result['segments'][1]['speaker'], 'Speaker 1')
        self.assertEqual(result['coverage']['scope'], 'full')
        self.assertEqual(result['coverage']['segments_processed'], 0)

    def test_lex_rejects_format_drift_empty_names_and_chapters(self):
        for html in [lex().replace('class="ts-text"', 'class="description"', 1),
                     lex().replace('Speaker 0', '', 1), lex().replace('00:01:30', '00:00:10'),
                     lex().replace('00:01:30', '00:99:10'), lex(count=19),
                     '<div class="entry-content"><h2>Transcript</h2><p>show notes only</p></div>']:
            with self.assertRaises(TranscriptUnavailable):
                parse_transcript('lex', html, source_url=LEX)

    def test_webvtt_retains_cue_end_and_voice(self):
        result = parse_webvtt('\ufeff' + vtt().replace('\n', '\r\n'), source_url=DIARY)
        self.assertEqual(result['source_kind'], 'official_transcript')
        self.assertEqual(result['segments'][0]['speaker'], 'Speaker 0')
        self.assertEqual(result['segments'][0]['end_seconds'], .9)
        self.assertIn('未独立听校', result['coverage']['note'])

    def test_webvtt_allows_no_voice_and_cue_identifier_and_skips_notes(self):
        text = vtt().replace('<v Speaker 0>', '').replace('<v Speaker 1>', '').replace('</v>', '')
        text = text.replace('WEBVTT\n\n', 'WEBVTT\n\nNOTE example comment\nignored\n\n')
        result = parse_webvtt(text, source_url=DIARY)
        self.assertEqual(result['segments'][0]['speaker'], '')
        self.assertEqual(len(result['segments']), 24)

    def test_webvtt_rejects_html_error_partial_cues_bad_time_and_short_text(self):
        for text in ['<html>403 Forbidden</html>', vtt(count=19), vtt().replace(SPEECH, 'brief'),
                     vtt().replace('00:00:30.000 --> 00:00:30.900', '00:00:30.000 --> 00:00:29.900'),
                     vtt().replace('00:01:30.000', '00:00:10.000'),
                     vtt().replace('00:01:30.000', '00:99:30.000'),
                     vtt() + '\n\nbroken incomplete cue']:
            with self.subTest(text=text[:30]), self.assertRaises(TranscriptUnavailable):
                parse_webvtt(text, source_url=DIARY)


if __name__ == '__main__':
    unittest.main()
