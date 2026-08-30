# Venture Radar / Business Analyzer research iteration — 2026-08-16

Scope: task 193 (Venture Radar) + task 202 (Business Analyzer). Research and
record only — no builds, no spend, no outreach, no accounts, no proprietary
code/branding copied. Public web reading + read-only internal recon only.

## 1. Market map — GEO/AEO/AI-visibility tooling (public evidence, Aug 2026)

| Player | Model | Entry price | Mid tier | Top/enterprise | Segment | Traction signal |
|---|---|---|---|---|---|---|
| **Profound** | Standalone GEO SaaS | $99/mo (ChatGPT only, 50 prompts) | $399/mo (3 engines, 100 prompts) | $2,000-5,000+/mo | Mid-market/enterprise | $96M Series C, $1B valuation (Feb 2026) |
| **Peec AI** | Standalone GEO SaaS | $89/mo (25 prompts, unlimited seats) | $199/mo (100 prompts) | $499/mo (300 prompts) | Marketing teams/agencies | $29.1M raised, $4M+ ARR in 10 months, 2,500+ teams, G2 4.9 |
| **Otterly.ai** | Standalone GEO SaaS | $29/mo (15 prompts) | $189/mo (100 prompts) | $489/mo (400 prompts) | Budget/SMB, agencies | Bootstrapped, Gartner 2025 Cool Vendor |
| **Ahrefs Brand Radar** | Add-on to Ahrefs subscription | $199/mo (1 platform) | $398/mo (select platforms) | $699/mo (all platforms) | Existing Ahrefs users | Requires base Ahrefs sub (~$228-828/mo realistic total) |
| **Semrush AI Visibility Toolkit** | Add-on / bundled suite | $99/mo/domain add-on | $199-549/mo (Semrush One AIO tiers) | Custom Enterprise AIO | Existing Semrush users | Per-seat multiplier noted as a cost trap |
| **SE Ranking AI Search** | Add-on / standalone (SE Visible) | $79/mo standalone (SE Visible, 150 prompts) | $89/mo add-on to $129-279 base | — | Existing SE Ranking users, budget buyers | Add-on model closest to our reuse strategy |
| **Scrunch AI** | Standalone GEO/AEO platform | $100/mo (ChatGPT only) | $250/mo (4 engines) | $500/mo (all LLMs) | Mid-market | **Acquired by Sitecore, June 2026** (consolidation signal) |
| **Rank Prompt** | Agency white-label SaaS | — | $149/mo Agency (500 brands) | $299/mo Agency Plus (1,000 brands, portal) | Agencies, multi-client | Free-tool/course funnel; no public funding data |
| **GEO Scout** | Standalone, RU/CIS-capable | Free tier (7-day refresh) | Paid tiers undisclosed | — | SMB, agencies, **RU-market operators (named)** | Only tool found covering Yandex/Alice/GigaChat; scale unclear |

Full source list is inline in the Business Analyzer cards (state=SCORED,
ids below); key ones: profound pricing via g2.com/products/profound/pricing
and thatmarketingbuddy.com; Peec via peec.ai/pricing and
techfundingnews.com/peec-ai-200m-valuation-10m-arr-geo-marketing; Otterly via
otterly.ai/pricing and trakkr.ai/reviews/otterly-review/pricing; Ahrefs via
aeolabs.ai/blog/ahrefs-brand-radar-review; Semrush via
semrush.com/pricing/ai/; SE Ranking via
trakkr.ai/reviews/seranking-review/pricing; Scrunch via scrunch.com/pricing/
and scalenut.com/blogs/scrunch-ai-review; Rank Prompt via
rankprompt.com/best-ai-visibility-tools-for-agencies/; GEO Scout via
geoscout.pro/en/blog/best-geo-monitoring-tools-2026.

### Top-3 findings

1. **The category is real and well-capitalized, not speculative.** Profound
   alone raised $96M at a $1B valuation; Peec AI hit $4M+ ARR in 10 months.
   Willingness to pay is proven in the $30-500/mo band. This meaningfully
   raises confidence that *a* market exists — but says nothing about whether
   *our* SMB-bundled angle specifically will convert.
2. **The bundled-add-on model (not standalone SaaS) is the closer precedent
   for this venture**, and SE Ranking/Semrush/Ahrefs are all already racing
   to bundle AI-visibility into suites our own SEO clients may already use —
   which could pre-empt our upsell before we ship it. This is a genuine
   structural validation of the "reuse existing delivery stack" thesis, but
   also a real risk (see below).
3. **The claimed RU/UA differentiation angle is not an open gap.** GEO Scout
   already ships Yandex/Alice/GigaChat coverage and names "Russian-market
   operators" as a target segment. This does not kill the venture, but it
   downgrades that specific claim from "differentiator" to "parity feature
   to match later" — the seed card has been corrected accordingly.

## 2. Internal reuse recon (read-only)

Checked `/opt/seo/MASTER_SYSTEM_STATE.md` and the live `traffic_os` Postgres
schema (`docker exec seo-postgres-1 psql ... \dt`, plus targeted `\d` on
`plans`, `white_label_configs`, `seo_competitors`).

- **No GEO/AEO/AI-answer-monitoring capability exists today** anywhere in
  the 213-table schema (no `ai_visibility`, `brand_mentions`, `geo_*` etc.)
  — this is a genuinely new capability, not a rename of something already
  built.
- **What's reusable:** crawler (`seo_crawl_jobs`/`seo_crawled_pages`),
  keyword/rank infra (`keywords`, `keyword_rank_checks`,
  `keyword_rank_results`, `serp_snapshots`), competitor tracking
  (`seo_competitors`), report/billing plumbing (`plans` — 4 tiers,
  $29/$99/$299/enterprise, all `is_active`), and `white_label_configs`
  (brand_name/logo/colors/support_email — a ready-made agency-resale
  mechanism matching the Rank Prompt angle, at zero build cost).
- **Existing client base is small.** Live counts: 5 `users`, 24
  `client_leads`, 2 `seo_connected_sites`. The seed card's "existing SEO
  client base first" acquisition channel is directionally right but was
  overstated — this is a pilot-sized list (well under 30), not a scaled
  warm channel. Corrected in the updated card.

## 3. Venture Radar update

- **Candidate:** `2e2e165a1a82` — "GEO/AEO visibility product"
- **State:** IDEA → **RESEARCHED** (transitioned `by="radar"`,
  note: "public-evidence sweep + internal reuse recon 2026-08-16")
- **Score:** 11.25 (confidence 0.45 / cost 2 / difficulty 2 — confidence
  moved down from 0.5)

Card fields updated with the evidence above: `market_evidence`,
`differentiation`, `risks`, `acquisition`, `monetization`,
`validation_experiment`, `kill_criteria`, `confidence`. Net honest
recalibration: **demand for the category is more solidly evidenced than
before** (real dollars, real funding, real reviews), but **two of the three
originally-claimed differentiators are weaker than assumed** — the RU/UA
angle has a named competitor, and the core AI-answer-check mechanism itself
is commodity across every player found. The one differentiator that
survives is distribution: bundling into an existing paid SEO relationship
at a below-market add-on price, which is a real but narrow edge, not a
technical moat. Confidence was moved from 0.5 to 0.45 to reflect that net
change (evidence up, differentiation down, on balance a wash slightly
negative).

## 4. Business Analyzer cards recorded (all SCORED, all 7 axes argued)

| id | title | score | state |
|---|---|---|---|
| `f5bd1f970db1` | Otterly.ai — AI visibility SaaS (bootstrapped budget tier) | 65.71 | SCORED |
| `647604ae8c8f` | Rank Prompt — agency white-label AI visibility | 68.57 | SCORED |
| `93a684d4a94d` | SE Ranking AI Search add-on — bundled-into-existing-SEO precedent | 74.29 | SCORED |
| `2445992f9297` | GEO Scout — RU/CIS AI-visibility coverage (competitive risk) | 42.86 | SCORED |
| `700bbe482d13` | Peec AI — VC-backed category leader (market calibration) | 42.86 | SCORED |

Highest-scored is the SE Ranking add-on card (74.29) — it's the closest
structural precedent for "bundle into an existing paid relationship," which
is exactly this venture's strategy. GEO Scout and Peec AI were recorded
deliberately low/DRAFT-adjacent-but-still-argued: GEO Scout as a
competitive-risk record (not a clone target), Peec AI as market-calibration
evidence of what a well-funded, product-deep winner looks like (also not a
realistic clone target for an SMB-priced add-on). None were transitioned
past SCORED — PROPOSED/APPROVED remain owner/analyzer-gated decisions not
taken here.

## 5. Open questions for the owner

1. Does the existing SEO client base (5 users / 24 leads / 2 connected
   sites) actually include anyone plausible for a paid AI-visibility
   add-on, or is the real first market cold-outreach — in which case the
   zero-reply/deliverability problem (task 200) blocks this venture too,
   not just the original SEO outreach motion?
2. Is it worth a cheap diligence pass on GEO Scout's actual Yandex/GigaChat
   answer quality before assuming it's a credible threat, or is it likely a
   thin free-tier wrapper we could still out-execute?
3. Given Semrush/Ahrefs/SE Ranking are all bundling AI-visibility into
   suites now, should the MVP ship faster/leaner (smaller prompt set,
   fewer assistants) to get real client signal before those incumbents'
   add-ons reach our own existing clients first?
4. Should the agency/white-label SKU (Rank Prompt precedent, `648...` card)
   be sequenced as MVP-v2 once the core audit is proven, given
   `white_label_configs` already exists at zero build cost — or is that
   premature given we have no confirmed agency clients yet?
