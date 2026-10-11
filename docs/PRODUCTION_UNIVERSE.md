# Initial production universe

On 2026-10-09, the user selected these 50 distinct ticker symbols for the initial
production universe. Preserve the supplied order. This supersedes the unresolved
universe choice; approximately 500 companies remains a v0 capacity target.

## Selected symbols

```text
DAL
GLDG
HOVR
NRIX
JPM
JNJ
GS
WFC
UNH
C
DPZ
ACI
AZZ
FBK
ASML
BAC
MS
BLK
PGR
FAST
STT
HOMB
TSM
SCHW
PLD
BNY
USB
PNC
MRSH
IBKR
ERIC
JBHT
WIT
AA
FHN
CBSH
CMC
INDB
CNS
SFNC
TRV
TFC
MTB
CFG
RF
BANF
AMZN
AAPL
NVDA
META
```

## SEC identity mapping used for initial seeding

All 50 selected symbols matched exactly one row in the SEC's current
[ticker/CIK/name/exchange mapping](https://www.sec.gov/files/company_tickers_exchange.json).
All 50 CIKs are distinct and padded to ten digits. Names below preserve the
SEC conformed registrant names; runtime identity is CIK-based, not ticker-based.
This is a current identity snapshot, not a historical ticker mapping.

Source retrieved: `2026-10-10T01:00:08+00:00`; source Last-Modified:
`2026-10-09T20:52:29Z`. SHA-256 of the downloaded source:
`33b3998934205f0ce54c874470a41689ea5aaaba7d04c909f20976eb3e551ba4`.
Only the selected records are included in the seed input; the full SEC download
is kept outside the repository. SEC describes these associations in its
[EDGAR data guide](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)
and does not guarantee their accuracy or scope. Review the intended registrants
before applying the seed; no ticker substitutions or inferred aliases were used.

| Ticker | CIK | SEC registrant name | Exchange |
| --- | --- | --- | --- |
| DAL | `0000027904` | DELTA AIR LINES, INC. | NYSE |
| GLDG | `0001538847` | GoldMining Inc. | NYSE |
| HOVR | `0001930021` | New Horizon Aircraft Ltd. | Nasdaq |
| NRIX | `0001549595` | Nurix Therapeutics, Inc. | Nasdaq |
| JPM | `0000019617` | JPMORGAN CHASE & CO | NYSE |
| JNJ | `0000200406` | JOHNSON & JOHNSON | NYSE |
| GS | `0000886982` | GOLDMAN SACHS GROUP INC | NYSE |
| WFC | `0000072971` | WELLS FARGO & COMPANY/MN | NYSE |
| UNH | `0000731766` | UNITEDHEALTH GROUP INC | NYSE |
| C | `0000831001` | CITIGROUP INC | NYSE |
| DPZ | `0001286681` | DOMINOS PIZZA INC | Nasdaq |
| ACI | `0001646972` | Albertsons Companies, Inc. | NYSE |
| AZZ | `0000008947` | AZZ INC | NYSE |
| FBK | `0001649749` | FB Financial Corp | NYSE |
| ASML | `0000937966` | ASML HOLDING NV | Nasdaq |
| BAC | `0000070858` | BANK OF AMERICA CORP /DE/ | NYSE |
| MS | `0000895421` | MORGAN STANLEY | NYSE |
| BLK | `0002012383` | BlackRock, Inc. | NYSE |
| PGR | `0000080661` | PROGRESSIVE CORP/OH/ | NYSE |
| FAST | `0000815556` | FASTENAL CO | Nasdaq |
| STT | `0000093751` | STATE STREET CORP | NYSE |
| HOMB | `0001331520` | HOME BANCSHARES INC | NYSE |
| TSM | `0001046179` | TAIWAN SEMICONDUCTOR MANUFACTURING CO LTD | NYSE |
| SCHW | `0000316709` | SCHWAB CHARLES CORP | NYSE |
| PLD | `0001045609` | Prologis, Inc. | NYSE |
| BNY | `0001390777` | Bank of New York Mellon Corp | NYSE |
| USB | `0000036104` | US BANCORP DE | NYSE |
| PNC | `0000713676` | PNC FINANCIAL SERVICES GROUP, INC. | NYSE |
| MRSH | `0000062709` | MARSH & MCLENNAN COMPANIES, INC. | NYSE |
| IBKR | `0001381197` | Interactive Brokers Group, Inc. | Nasdaq |
| ERIC | `0000717826` | ERICSSON LM TELEPHONE CO | Nasdaq |
| JBHT | `0000728535` | HUNT J B TRANSPORT SERVICES INC | Nasdaq |
| WIT | `0001123799` | WIPRO LTD | NYSE |
| AA | `0001675149` | Alcoa Corp | NYSE |
| FHN | `0000036966` | FIRST HORIZON CORP | NYSE |
| CBSH | `0000022356` | COMMERCE BANCSHARES INC /MO/ | Nasdaq |
| CMC | `0000022444` | COMMERCIAL METALS Co | NYSE |
| INDB | `0000776901` | INDEPENDENT BANK CORP | Nasdaq |
| CNS | `0001284812` | COHEN & STEERS, INC. | NYSE |
| SFNC | `0000090498` | SIMMONS FIRST NATIONAL CORP | Nasdaq |
| TRV | `0000086312` | TRAVELERS COMPANIES, INC. | NYSE |
| TFC | `0000092230` | TRUIST FINANCIAL CORP | NYSE |
| MTB | `0000036270` | M&T BANK CORP | NYSE |
| CFG | `0000759944` | CITIZENS FINANCIAL GROUP INC/RI | NYSE |
| RF | `0001281761` | REGIONS FINANCIAL CORP | NYSE |
| BANF | `0000760498` | BANCFIRST CORP /OK/ | Nasdaq |
| AMZN | `0001018724` | AMAZON COM INC | Nasdaq |
| AAPL | `0000320193` | Apple Inc. | Nasdaq |
| NVDA | `0001045810` | NVIDIA CORP | Nasdaq |
| META | `0001326801` | Meta Platforms, Inc. | Nasdaq |

## Reviewed seed and maintenance

[`infra/seed/companies.prod.json`](../infra/seed/companies.prod.json) is the
create-only DynamoDB transaction for these 50 enabled companies, in the selected
order, targeting account `559007813222`, `us-east-1`, `finbot-prod-companies`.
Each item contains repository schema version and enabled-index marker; every Put
requires `attribute_not_exists(cik)`. This input cannot overwrite an existing CIK.
Keep the initial seed unchanged once applied; later universe changes need separate
reviewed inputs. The runtime reads the Companies table, not this document/seed.

On 2026-10-09 local time the seed was applied and strong base-table verification
matched all 50 records, with no missing, extra or mismatched attributes. The enabled
index subsequently returned 50 records. Initial seeding is complete; it is not a
recurring readiness check. See [deployment procedures](DEPLOYMENT.md#company-universe)
for initial setup and [dated verification](DEPLOYMENT.md#last-recorded-verification)
for service delivery evidence.

The first ordered `text` block is read by `tests/unit/test_production_seed.py`
to verify the seed against the independently selected ticker list. Preserve its
order/format when maintaining this document. The mapping is a reviewed initial
snapshot, not a live registry or historical ticker mapping.
