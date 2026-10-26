"""U4 — the first-user-message parser.

PURE. Fixtures only. No filesystem, no tmux, no git.

Why this exists
---------------
`pi-tree` fed `registry.topic` into `pi_taxonomy`'s `first_user_message` slot.
But `topic` is a truncated *last*-prompt preview, so the field was labelled one
thing and filled with another. The spec's priority 4 says the first user message,
and the first message is also the more stable signal: what a session was opened
to do does not drift the way its latest prompt does.

The transcript format is one JSON object per line:
    {"type": "message", "message": {"role": "user", "content": ...}}
`content` is either a string or a list of parts, of which only `type == "text"`
carries text.
"""
import json
import unittest

from pi_sources import first_user_message_from_lines


def line(obj):
    return json.dumps(obj)


def msg(role, content):
    return line({"type": "message", "message": {"role": role, "content": content}})


class TestFirstUserMessage(unittest.TestCase):
    def test_string_content(self):
        lines = [msg("user", "uppdatera bloggartikeln")]
        self.assertEqual(first_user_message_from_lines(lines), "uppdatera bloggartikeln")

    def test_list_content_joins_text_parts(self):
        lines = [msg("user", [{"type": "text", "text": "hej"},
                              {"type": "text", "text": "varld"}])]
        self.assertEqual(first_user_message_from_lines(lines), "hej varld")

    def test_skips_non_text_parts(self):
        lines = [msg("user", [{"type": "image", "data": "zzz"},
                              {"type": "text", "text": "riktig text"}])]
        self.assertEqual(first_user_message_from_lines(lines), "riktig text")

    def test_returns_the_FIRST_user_message_not_the_last(self):
        lines = [msg("user", "first one"), msg("assistant", "reply"),
                 msg("user", "second one")]
        self.assertEqual(first_user_message_from_lines(lines), "first one")

    def test_skips_system_and_assistant(self):
        lines = [msg("system", "you are an agent"), msg("assistant", "hello"),
                 msg("user", "the real one")]
        self.assertEqual(first_user_message_from_lines(lines), "the real one")

    def test_ignores_non_message_records(self):
        lines = [line({"type": "session", "cwd": "/root", "version": 1}),
                 line({"type": "model_change", "provider": "p"}),
                 line({"type": "thinking_level_change", "thinkingLevel": "high"}),
                 msg("user", "after the preamble")]
        self.assertEqual(first_user_message_from_lines(lines), "after the preamble")

    def test_ignores_unparseable_lines(self):
        lines = ["not json at all", msg("user", "survives")]
        self.assertEqual(first_user_message_from_lines(lines), "survives")

    def test_no_user_message_yields_empty_string(self):
        self.assertEqual(first_user_message_from_lines([msg("assistant", "only me")]), "")

    def test_empty_input(self):
        self.assertEqual(first_user_message_from_lines([]), "")

    def test_result_is_capped_and_never_raises(self):
        lines = [msg("user", "x" * 5000)]
        got = first_user_message_from_lines(lines)
        self.assertLessEqual(len(got), 1000)

    def test_does_not_read_past_the_first_user_message(self):
        """A generator that raises if over-consumed: proves the early exit.

        Transcripts reach hundreds of KB, and 49 of them are parsed per run.
        """
        def gen():
            yield msg("system", "preamble")
            yield msg("user", "stop here")
            raise AssertionError("read past the first user message")

        self.assertEqual(first_user_message_from_lines(gen()), "stop here")


if __name__ == "__main__":
    unittest.main()
