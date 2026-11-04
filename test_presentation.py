import unittest
from html import escape
from html.parser import HTMLParser

from presentation import split_messages, visible_length


class ParsedMessage(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.stack, self.text = [], []
        self.feed(text)
        self.close()
        assert not self.stack, "Unclosed HTML tags"

    def handle_starttag(self, tag, attrs):
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack.pop() == tag, "Mismatched HTML tags"

    def handle_data(self, data):
        self.text.append(data)


class MessageSplittingTests(unittest.TestCase):
    def test_large_block_preserves_text_entities_links_and_nested_formatting(self):
        plain = '😀 & <text> "quotes" ' * 800
        block = '<b>Title <a href="https://example.com/?a=1&amp;b=2">' + escape(plain) + '</a></b>'
        chunks = split_messages([block])
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(visible_length(chunk), 4096)
            self.assertGreater(visible_length(chunk), 0)
            self.assertIn('href="https://example.com/?a=1&amp;b=2"', chunk)
        self.assertEqual("".join("".join(ParsedMessage(chunk).text) for chunk in chunks), "Title " + plain)

    def test_utf16_boundary_and_numeric_entities(self):
        chunks = split_messages(["<i>" + "x" * 4095 + "&#128512;&amp;</i>"])
        self.assertEqual([visible_length(chunk) for chunk in chunks], [4095, 3])
        self.assertEqual("".join("".join(ParsedMessage(chunk).text) for chunk in chunks), "x" * 4095 + "😀&")

    def test_small_blocks_keep_existing_format(self):
        self.assertEqual(split_messages(["<b>A</b>", "B"], separator="\n\n"), ["<b>A</b>\n\nB"])
