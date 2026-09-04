"""Unit tests for narrator.py — the only module allowed to call an LLM.

No test in this file hits the real network. `fetch_recent_news` and both
LLM transports are mocked so these run offline and deterministically — the
point being tested is the CONTROL FLOW (which provider gets picked, when
does it fall back, does it ever fabricate a citation), not whether yfinance
or Groq/Anthropic happen to be reachable right now.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app import narrator
from app.narrator import NewsItem

_NEWS = [NewsItem(title="Headline A", publisher="Reuters", link="http://x")]


class HeadlineFallback(unittest.TestCase):
    def test_no_news_returns_no_news_found(self):
        result = narrator._headline_fallback([])
        self.assertEqual(result.generated_by, "no-news-found")
        self.assertEqual(result.sources, [])

    def test_with_news_cites_top_headline_by_title(self):
        result = narrator._headline_fallback(_NEWS)
        self.assertEqual(result.generated_by, "headline-fallback")
        self.assertIn("Headline A", result.text)
        self.assertIn("Reuters", result.text)

    def test_states_plainly_it_is_not_ai_generated(self):
        result = narrator._headline_fallback(_NEWS)
        self.assertIn("not an AI-synthesized", result.text)


class ProviderResolution(unittest.TestCase):
    @patch.object(narrator, "NARRATOR_PROVIDER", "auto")
    @patch.object(narrator, "GROQ_API_KEY", None)
    @patch.object(narrator, "ANTHROPIC_API_KEY", None)
    def test_auto_with_no_keys_resolves_none(self):
        self.assertEqual(narrator.resolve_provider(), "none")

    @patch.object(narrator, "NARRATOR_PROVIDER", "auto")
    @patch.object(narrator, "GROQ_API_KEY", "gsk_fake")
    @patch.object(narrator, "ANTHROPIC_API_KEY", None)
    def test_auto_prefers_groq_when_its_key_is_set(self):
        self.assertEqual(narrator.resolve_provider(), "groq")

    @patch.object(narrator, "NARRATOR_PROVIDER", "auto")
    @patch.object(narrator, "GROQ_API_KEY", "gsk_fake")
    @patch.object(narrator, "ANTHROPIC_API_KEY", "sk-ant-fake")
    def test_auto_prefers_groq_over_anthropic_when_both_set(self):
        # Groq's free tier is the pragmatic default; Anthropic is opt-in.
        self.assertEqual(narrator.resolve_provider(), "groq")

    @patch.object(narrator, "NARRATOR_PROVIDER", "anthropic")
    @patch.object(narrator, "GROQ_API_KEY", "gsk_fake")
    @patch.object(narrator, "ANTHROPIC_API_KEY", "sk-ant-fake")
    def test_explicit_provider_overrides_auto_detection(self):
        self.assertEqual(narrator.resolve_provider(), "anthropic")

    @patch.object(narrator, "NARRATOR_PROVIDER", "none")
    @patch.object(narrator, "GROQ_API_KEY", "gsk_fake")
    def test_explicit_none_disables_llm_even_with_a_key_present(self):
        self.assertEqual(narrator.resolve_provider(), "none")


class NarrateViaGroq(unittest.TestCase):
    @patch.object(narrator, "NARRATOR_PROVIDER", "groq")
    @patch.object(narrator, "GROQ_API_KEY", "gsk_fake")
    @patch.object(narrator, "fetch_recent_news")
    @patch.object(narrator, "_synthesize_openai_compatible")
    def test_successful_groq_synthesis(self, mock_synth, mock_fetch):
        mock_fetch.return_value = _NEWS
        mock_synth.return_value = "Groq-synthesized explanation."

        result = narrator.narrate_event("TCS", "up", 5.0, 3.0, 0.9, "verified")

        self.assertEqual(result.generated_by, "groq-api")
        self.assertEqual(result.text, "Groq-synthesized explanation.")
        self.assertEqual(result.model, narrator.GROQ_MODEL)
        self.assertIsNone(result.error)

    @patch.object(narrator, "NARRATOR_PROVIDER", "groq")
    @patch.object(narrator, "GROQ_API_KEY", "gsk_fake")
    @patch.object(narrator, "fetch_recent_news")
    @patch.object(narrator, "_synthesize_openai_compatible")
    def test_groq_failure_degrades_to_headline_fallback(self, mock_synth, mock_fetch):
        mock_fetch.return_value = _NEWS
        mock_synth.side_effect = RuntimeError("429 rate limited")

        result = narrator.narrate_event("TCS", "up", 5.0, 3.0, 0.9, "verified")

        self.assertEqual(result.generated_by, "headline-fallback")
        self.assertIn("RuntimeError", result.error)

    @patch.object(narrator, "NARRATOR_PROVIDER", "groq")
    @patch.object(narrator, "GROQ_API_KEY", None)
    @patch.object(narrator, "fetch_recent_news")
    def test_groq_pinned_but_key_missing_falls_back(self, mock_fetch):
        mock_fetch.return_value = _NEWS
        result = narrator.narrate_event("TCS", "up", 5.0, 3.0, 0.9, "verified")
        self.assertEqual(result.generated_by, "headline-fallback")
        self.assertIsNotNone(result.error)

    @patch.object(narrator, "NARRATOR_PROVIDER", "groq")
    @patch.object(narrator, "GROQ_API_KEY", "gsk_fake")
    @patch.object(narrator, "fetch_recent_news")
    @patch.object(narrator, "_synthesize_openai_compatible")
    def test_empty_model_response_degrades_to_fallback(self, mock_synth, mock_fetch):
        mock_fetch.return_value = _NEWS
        mock_synth.return_value = "   "        # whitespace only

        result = narrator.narrate_event("TCS", "up", 5.0, 3.0, 0.9, "verified")
        self.assertEqual(result.generated_by, "headline-fallback")


class NarrateViaAnthropic(unittest.TestCase):
    @patch.object(narrator, "NARRATOR_PROVIDER", "anthropic")
    @patch.object(narrator, "ANTHROPIC_API_KEY", "sk-ant-fake")
    @patch.object(narrator, "fetch_recent_news")
    @patch.object(narrator, "_synthesize_anthropic")
    def test_successful_anthropic_synthesis(self, mock_synth, mock_fetch):
        mock_fetch.return_value = _NEWS
        mock_synth.return_value = "Claude-synthesized explanation."

        result = narrator.narrate_event("TCS", "up", 5.0, 3.0, 0.9, "verified")

        self.assertEqual(result.generated_by, "anthropic-api")
        self.assertEqual(result.model, narrator.ANTHROPIC_MODEL)


class NarrateGuards(unittest.TestCase):
    @patch.object(narrator, "NARRATOR_PROVIDER", "groq")
    @patch.object(narrator, "GROQ_API_KEY", "gsk_fake")
    @patch.object(narrator, "fetch_recent_news")
    def test_no_news_skips_llm_call_entirely(self, mock_fetch):
        mock_fetch.return_value = []
        result = narrator.narrate_event("TCS", "up", 5.0, 3.0, 0.9, "verified")
        self.assertEqual(result.generated_by, "no-news-found")

    def test_prompt_carries_exact_confirmed_numbers(self):
        """The prompt must contain the EXACT confirmed numbers — this is the
        guard against a model 'correcting' a number it never should touch."""
        prompt = narrator._build_user_prompt(
            "TCS", "down", -6.789, -4.321, "verified", _NEWS
        )
        self.assertIn("-6.79%", prompt)     # return_pct, 2dp
        self.assertIn("-4.32", prompt)      # z_score, 2dp
        self.assertIn("Headline A", prompt)

    def test_system_prompt_forbids_inventing_a_cause(self):
        self.assertIn("do not invent a cause", narrator.NARRATOR_SYSTEM_PROMPT)
        self.assertIn("Never give investment advice", narrator.NARRATOR_SYSTEM_PROMPT)


class CitationIntegrity(unittest.TestCase):
    """Regression guard for a real bug found in live testing.

    The prompt was built from 5 headlines but the response only returned
    news[:3]. When the model legitimately cited headline #4, it looked to
    the reader like a fabricated source with no way to verify it — silently
    breaking the "always cited" guarantee this whole module exists to make.
    Every headline the model can see MUST come back in `sources`.
    """

    _FIVE = [
        NewsItem(title=f"Headline {i}", publisher="P", link=f"http://x/{i}")
        for i in range(5)
    ]

    @patch.object(narrator, "NARRATOR_PROVIDER", "groq")
    @patch.object(narrator, "GROQ_API_KEY", "gsk_fake")
    @patch.object(narrator, "fetch_recent_news")
    @patch.object(narrator, "_synthesize_openai_compatible")
    def test_every_prompted_headline_is_returned_as_a_source(self, mock_synth, mock_fetch):
        mock_fetch.return_value = self._FIVE
        mock_synth.return_value = "Some explanation."

        result = narrator.narrate_event("TCS", "up", 5.0, 3.0, 0.9, "verified")

        prompted = narrator._build_user_prompt(
            "TCS", "up", 5.0, 3.0, "verified", self._FIVE
        )
        # Anything the model was shown must be verifiable by the reader.
        for item in self._FIVE:
            self.assertIn(item.title, prompted)
            self.assertIn(
                item.title, [s.title for s in result.sources],
                msg=f"{item.title!r} was in the prompt but not returned as a source",
            )

    @patch.object(narrator, "NARRATOR_PROVIDER", "none")
    @patch.object(narrator, "fetch_recent_news")
    def test_fallback_also_returns_the_full_source_set(self, mock_fetch):
        mock_fetch.return_value = self._FIVE
        result = narrator.narrate_event("TCS", "up", 5.0, 3.0, 0.9, "verified")
        self.assertEqual(len(result.sources), 5)


if __name__ == "__main__":
    unittest.main()
