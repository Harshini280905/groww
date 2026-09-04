"""Unit tests for narrator.py — the only module allowed to call an LLM.

No test in this file hits the real network. `fetch_recent_news` and the
Anthropic client are mocked so these run offline and deterministically —
the point being tested is the CONTROL FLOW (when does it call the model,
when does it fall back, does it ever fabricate a citation), not whether
yfinance or Anthropic's API happen to be reachable right now.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app import narrator
from app.narrator import NewsItem


class HeadlineFallback(unittest.TestCase):
    def test_no_news_returns_no_news_found(self):
        result = narrator._headline_fallback([])
        self.assertEqual(result.generated_by, "no-news-found")
        self.assertEqual(result.sources, [])

    def test_with_news_cites_top_headline_by_title(self):
        news = [NewsItem(title="TCS wins large IT deal", publisher="Reuters", link="http://x")]
        result = narrator._headline_fallback(news)
        self.assertEqual(result.generated_by, "headline-fallback")
        self.assertIn("TCS wins large IT deal", result.text)
        self.assertIn("Reuters", result.text)

    def test_states_plainly_that_no_key_is_configured(self):
        news = [NewsItem(title="Some headline", publisher="P", link="l")]
        result = narrator._headline_fallback(news)
        self.assertIn("No ANTHROPIC_API_KEY", result.text)


class NarrateEventWithoutKey(unittest.TestCase):
    @patch.object(narrator, "ANTHROPIC_API_KEY", None)
    @patch.object(narrator, "fetch_recent_news")
    def test_falls_back_when_no_key_configured(self, mock_fetch):
        mock_fetch.return_value = [NewsItem(title="Headline A", publisher="P", link="l")]
        result = narrator.narrate_event("TCS", "up", 5.0, 3.0, 0.9, "verified")
        self.assertEqual(result.generated_by, "headline-fallback")
        mock_fetch.assert_called_once_with("TCS")


class NarrateEventWithKey(unittest.TestCase):
    def _fake_response(self, text: str):
        block = MagicMock()
        block.type = "text"
        block.text = text
        resp = MagicMock()
        resp.content = [block]
        return resp

    @patch.object(narrator, "ANTHROPIC_API_KEY", "fake-key-for-test")
    @patch.object(narrator, "fetch_recent_news")
    @patch.object(narrator, "anthropic")
    def test_successful_synthesis_returns_claude_api(self, mock_anthropic_module, mock_fetch):
        mock_fetch.return_value = [NewsItem(title="Headline A", publisher="P", link="l")]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = self._fake_response("Synthesized explanation.")
        mock_anthropic_module.Anthropic.return_value = mock_client

        result = narrator.narrate_event("TCS", "up", 5.0, 3.0, 0.9, "verified")

        self.assertEqual(result.generated_by, "claude-api")
        self.assertEqual(result.text, "Synthesized explanation.")
        self.assertIsNone(result.error)

    @patch.object(narrator, "ANTHROPIC_API_KEY", "fake-key-for-test")
    @patch.object(narrator, "fetch_recent_news")
    @patch.object(narrator, "anthropic")
    def test_api_failure_degrades_to_headline_fallback(self, mock_anthropic_module, mock_fetch):
        mock_fetch.return_value = [NewsItem(title="Headline A", publisher="P", link="l")]
        mock_anthropic_module.Anthropic.side_effect = RuntimeError("network down")

        result = narrator.narrate_event("TCS", "up", 5.0, 3.0, 0.9, "verified")

        self.assertEqual(result.generated_by, "headline-fallback")
        self.assertIsNotNone(result.error)
        self.assertIn("RuntimeError", result.error)

    @patch.object(narrator, "ANTHROPIC_API_KEY", "fake-key-for-test")
    @patch.object(narrator, "fetch_recent_news")
    @patch.object(narrator, "anthropic")
    def test_empty_model_response_degrades_to_fallback(self, mock_anthropic_module, mock_fetch):
        mock_fetch.return_value = [NewsItem(title="Headline A", publisher="P", link="l")]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = self._fake_response("")   # empty text
        mock_anthropic_module.Anthropic.return_value = mock_client

        result = narrator.narrate_event("TCS", "up", 5.0, 3.0, 0.9, "verified")

        self.assertEqual(result.generated_by, "headline-fallback")
        self.assertIsNotNone(result.error)

    @patch.object(narrator, "ANTHROPIC_API_KEY", "fake-key-for-test")
    @patch.object(narrator, "fetch_recent_news")
    def test_no_news_skips_llm_call_entirely(self, mock_fetch):
        mock_fetch.return_value = []
        result = narrator.narrate_event("TCS", "up", 5.0, 3.0, 0.9, "verified")
        self.assertEqual(result.generated_by, "no-news-found")

    @patch.object(narrator, "ANTHROPIC_API_KEY", "fake-key-for-test")
    @patch.object(narrator, "fetch_recent_news")
    @patch.object(narrator, "anthropic")
    def test_never_states_a_different_number_in_prompt(self, mock_anthropic_module, mock_fetch):
        """The prompt sent to the model must contain the EXACT confirmed
        numbers, not a re-derived or rounded-differently version — this is
        the guard against the model "correcting" a number it was never
        supposed to touch."""
        mock_fetch.return_value = [NewsItem(title="Headline A", publisher="P", link="l")]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = self._fake_response("ok")
        mock_anthropic_module.Anthropic.return_value = mock_client

        narrator.narrate_event("TCS", "down", -6.789, -4.321, 0.85, "verified")

        call_kwargs = mock_client.messages.create.call_args.kwargs
        prompt_text = call_kwargs["messages"][0]["content"]
        self.assertIn("-6.79%", prompt_text)     # return_pct formatted to 2dp
        self.assertIn("-4.32", prompt_text)      # z_score formatted to 2dp


if __name__ == "__main__":
    unittest.main()
