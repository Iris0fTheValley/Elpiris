# Approved-source real-news evidence cards

These 1400x820 cards were produced from real published pages on 2026-08-09. They
are deterministic evidence inputs for the local E2E workflow, not raw full-page
screenshots. The capture pipeline extracts the article title, lead text,
publication metadata, and an editorial image while excluding navigation, login
prompts, cookie UI, promotions, advertising URLs, and banner-shaped assets.

The project accepts news only from Guardian, Reddit, Pikabu, and Dazhong. Reddit
is not represented in this image set because no valid local OAuth credential was
available and test credentials must never be fabricated. Each PNG has a JSON
sidecar containing the canonical URL, capture transport, dimensions, SHA-256,
and rights state.

| File | Registered source | Canonical page | SHA-256 |
|---|---|---|---|
| `guardian-arizona-teen.png` | Guardian | https://www.theguardian.com/us-news/2026/jul/18/arizona-teen-rescue-woman-dementia-heatwave | `2c37513c8f11dff202f331587a8f1336ddf07d5510af3a3d74e30bf8aede17eb` |
| `guardian-fathers-day.png` | Guardian | https://www.theguardian.com/lifeandstyle/2026/jul/20/kindness-of-strangers-grief-loss-fathers-day-diner-who-paid-my-bill | `ec1e02e6ae2a3fcc01aaeaea8500e760707fd4aa77520d032408dae9c76f84b2` |
| `guardian-hotel-kindness.png` | Guardian | https://www.theguardian.com/lifeandstyle/2026/jul/27/the-kindness-of-strangers-when-mum-was-hospitalised-on-a-family-holiday-our-hotels-owners-were-heaven-sent | `7c9f758d86f23a023ddbdf4e56b236d0ab4c3d908fe9d6c0bc233b168e79811e` |
| `pikabu-swan-rescue.png` | Pikabu | https://pikabu.ru/story/sotrudnitsa_politsii_vmeste_s_druzyami_spasla_dvukh_lebedyatsirot_14207552 | `a589d591d88df52cfa9a392d46e86e56febe631db3baa4ee3807e2cbdfc82226` |
| `pikabu-nest-rescue.png` | Pikabu | https://pikabu.ru/story/v_peterburge_spasli_zamurovannoe_gnezdyishko_s_ptentsami_13922789 | `8413f5b0620c85af3c28004984c133b2f5644edecc1cc7dc1e12e0c9b754ab05` |
| `dazhong-free-meals.png` | Dazhong | https://m.dzplus.dzng.com/share/general/0/NEWS3581000EHEVSBXUWQQSM | `53c4bc676f42fda7131250406bc7d98904d2a0021f588420cc1374230fbd5b98` |

Guardian, Dazhong, and the swan story were extracted through a live browser.
Pikabu rate-limited the later nest-story request, so that card was rendered from
the checked-in normalized real-source fixture and records
`capture_transport=normalized_source_fixture` rather than claiming a live page
capture.

Reproduce a live capture from `frontend/` with:

```powershell
pnpm capture:source-evidence -- --source guardian --url <canonical-url> --output <png-path>
```

Rights status: internal testing and editorial review only. Public accessibility
does not grant republication permission. Every screenshot remains
`publish_eligible=false` until an operator records sufficient rights evidence.
Do not redistribute these files as standalone assets.
