"""Task tag: round-trips through a kanban_comment line; a native task.model (Hermes
model_override) always overrides the comment's model. Offline."""

import unittest

from core.task_tag import RouteTag, TagError, parse_tag, serialize_tag


class TaskTagTest(unittest.TestCase):
    def test_round_trip_with_model(self):
        t = parse_tag(serialize_tag("claude", "standard", "anthropic/claude-sonnet"))
        self.assertEqual(t, RouteTag("claude", "standard", "anthropic/claude-sonnet"))

    def test_round_trip_without_model(self):
        t = parse_tag(serialize_tag("gemma-local", "trivial"))
        self.assertEqual(t.profile, "gemma-local")
        self.assertEqual(t.tier, "trivial")
        self.assertIsNone(t.model)

    def test_last_tag_wins_amid_prose(self):
        body = ("worker note\n" + serialize_tag("a", "trivial")
                + "\nmore prose\n" + serialize_tag("claude", "elevated", "m"))
        t = parse_tag(body)
        self.assertEqual((t.profile, t.tier), ("claude", "elevated"))

    def test_native_model_overrides_comment(self):
        t = parse_tag(serialize_tag("claude", "standard", "comment-model"), native_model="native-model")
        self.assertEqual(t.model, "native-model")     # invariant: native task.model is authoritative

    def test_no_tag_returns_none(self):
        self.assertIsNone(parse_tag("just a normal comment, no tag"))
        self.assertIsNone(parse_tag("", native_model="x"))   # native alone is not a route

    def test_malformed_raises(self):
        with self.assertRaises(TagError):
            parse_tag("ultracode-route:{not valid json")
        with self.assertRaises(TagError):
            parse_tag("ultracode-route:" + '{"tier":"standard"}')   # missing profile
        with self.assertRaises(TagError):
            serialize_tag("", "standard")


if __name__ == "__main__":
    unittest.main()
