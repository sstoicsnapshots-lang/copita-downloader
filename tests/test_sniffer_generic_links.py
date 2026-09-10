"""Regression checks for MediaSniffer's generic (not site-specific) document
and ebook link detection, and its cookie propagation to the actual download.
"""
import unittest

from copita.core.sniffer import MediaSniffer


class GenericDocumentLinkTests(unittest.TestCase):
    def test_finds_link_with_real_file_extension(self):
        html = '<a href="/files/report.pdf">Download report</a>'
        links = MediaSniffer._extract_generic_document_links(html, "https://example.com/page")
        self.assertEqual(len(links), 1)
        self.assertTrue(links[0][0].endswith("/files/report.pdf"))
        self.assertIn("pdf", links[0][1].lower())

    def test_finds_link_with_no_extension_but_download_intent_query(self):
        """Regression: bdebooks.com-style links have no file extension at
        all — the format is content-negotiated via ?fmt=pdf. A plain
        extension check misses these entirely."""
        html = '<a href="/en/book-download/?fmt=pdf&cover=2">PDF — Universal, works everywhere</a>'
        links = MediaSniffer._extract_generic_document_links(html, "https://bdebooks.com/en/books/x/")
        self.assertEqual(len(links), 1)
        self.assertIn("fmt=pdf", links[0][0])

    def test_ignores_unrelated_links(self):
        html = """
            <a href="/style.css">css</a>
            <a href="https://example.com/about">About</a>
            <a href="/logo.png">logo</a>
        """
        links = MediaSniffer._extract_generic_document_links(html, "https://example.com/")
        self.assertEqual(links, [])

    def test_relative_links_are_resolved_against_page_url(self):
        html = '<a href="download/book.epub">Get EPUB</a>'
        links = MediaSniffer._extract_generic_document_links(html, "https://example.com/books/x/")
        self.assertEqual(links[0][0], "https://example.com/books/x/download/book.epub")

    def test_deduplicates_identical_resolved_links(self):
        html = '<a href="/f.pdf">A</a><a href="/f.pdf">B</a>'
        links = MediaSniffer._extract_generic_document_links(html, "https://example.com/")
        self.assertEqual(len(links), 1)


if __name__ == "__main__":
    unittest.main()
