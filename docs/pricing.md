# Pricing maintenance

The last source review was **2026-10-20**. Both `pricing.yaml` copies must be
updated together; `tests/test_pricing_file_sync.py` checks this.

Use standard public API rates in USD per million tokens. Provider keys identify
where usage was recorded, so an OpenAI model routed through Claude Code or Cursor
needs its own provider row. These are API-equivalent estimates, not subscription
charges, included credits, or a vendor invoice.

## Sources checked

- [OpenAI API pricing](https://developers.openai.com/api/docs/pricing): GPT-6
  family, GPT-6.1 Sol and current GPT-5.6 family rates, including cache writes.
- [Claude pricing](https://platform.claude.com/docs/en/about-claude/pricing):
  Sonnet/Haiku 5.5 and current Opus rates, including Cursor-routed Opus variants.
- [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing): Gemini 4
  Argon, 3.6 Flash and 3.1 Pro. Argon's published December increase and Flash's
  published January increase have dated rows.
- [Cursor models and pricing](https://cursor.com/docs/models-and-pricing):
  Composer 2.5 standard and Fast rates. Unlike older opaque Composer entries,
  Composer 2.5 has published token rates and must not inherit a zero-cost row.

## Dates and limitations

Lookup uses the latest row on or before the usage event's date. The October 20
rows are verified-as-of baselines. Their dates do not assert release dates or
retroactively correct old estimates where the vendor's price-change date has not
been established. Existing historical rows remain unchanged. Add a future row
only when the vendor gives both a definite date and a definite new price.
GPT-5.6 Sol's promotion is guaranteed at least through November 21; this does not
establish the rate on November 22, so no speculative increase is scheduled.

The table models one context tier per model: OpenAI input up to 272K, Claude
Haiku 5.5 prompts up to 100K, and Gemini Pro prompts up to 200K. Long-context
surcharges, OpenAI Fast/Ultrafast/Batch/Flex tiers, regional premiums, Claude
one-hour cache writes, Gemini cache storage-hours and tool charges are not
represented. Composer's explicitly named Fast variant has its own row. Gemini
4 Argon has no explicit cache-write rate because it supports implicit caching
only. Other Gemini cache writes use ordinary input rates.

An unrecognized suffix still uses the longest available model prefix and is
flagged as approximate in the dashboard. Review exact model IDs when updating
prices; similar model names can have different rates.
