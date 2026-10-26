# Production universe

On 2026-10-09, the user selected these 50 distinct ticker symbols for the initial
production universe. Preserve the supplied order. This supersedes the unresolved
universe choice; approximately 500 companies remains a v0 capacity target.

The current reviewed universe contains **92 companies** in one consolidated
[`infra/seed/companies.prod.json`](../infra/seed/companies.prod.json) file. It lists
the original 50 first, followed by the 22 October 15 additions and the 20 October 26
additions below, with no duplicate CIKs or tickers. DynamoDB remains the running service's source of enabled companies;
editing this file alone does not update production.

## Initial selected symbols

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
consolidated create-only DynamoDB transaction for all 92 reviewed companies,
targeting account `559007813222`, `us-east-1`, `finbot-prod-companies`.
Each item contains repository schema version and enabled-index marker; every Put
requires `attribute_not_exists(cik)`. This input cannot overwrite an existing CIK.
The original 50 and October 15 additions were applied in separate transactions;
the October 26 additions were applied with the additive loader. All records are
consolidated locally. Use the additive loader after appending
reviewed new records to this single file:

```bash
.venv/bin/python scripts/load_companies.py --profile default
```

It inserts missing CIKs and skips existing records with a successful exit, so the
file can be loaded repeatedly. Existing names, symbols, enabled states and other
attributes remain unchanged; mismatches are reported. Removing records from the
file does not disable or delete them in DynamoDB. The loader validates all input
before AWS access, checks the account from the table ARN, and conditionally creates
each missing row. Identical duplicates collapse; conflicting duplicates reject
the input. A failed/interrupted partial load can resume with the same command.
`--dry-run` reports missing/existing records without writes.

The JSON retains its create-only transaction format for compatibility. Submitting
it directly to `aws dynamodb transact-write-items` still fails atomically if any
CIK exists; routine additions use the loader instead. Its top-level transaction
token is ignored by the loader. The runtime reads the Companies table, not this
document/seed. See [operator details](DEPLOYMENT.md#company-universe).

On 2026-10-09 local time the original 50-company seed was applied and strong base-table verification
matched all 50 records, with no missing, extra or mismatched attributes. The enabled
index subsequently returned 50 records. Initial seeding is complete; it is not a
recurring readiness check. See [deployment procedures](DEPLOYMENT.md#company-universe)
for initial setup and [dated verification](DEPLOYMENT.md#last-recorded-verification)
for service delivery evidence.

The first ordered `text` block is read by `tests/unit/test_production_seed.py`
to verify the original 50 entries in the consolidated seed against the independently
selected ticker list. Preserve its order/format when maintaining this document.
The mapping is a reviewed initial snapshot, not a live registry or historical
ticker mapping.

## October 15, 2026 additions

The user requested 23 symbols on October 15 (America/Los_Angeles). `META` was
already enabled and is retained once. The following 22 new companies extend the
reviewed universe to **72 enabled companies**. The original ordered 50-company
list is preserved as the first 50 entries in the consolidated seed.

| Ticker | CIK | SEC registrant name | Exchange |
| --- | --- | --- | --- |
| NFLX | `0001065280` | NETFLIX INC | Nasdaq |
| RTX | `0000101829` | RTX Corp | NYSE |
| MMM | `0000066740` | 3M CO | NYSE |
| COF | `0000927628` | CAPITAL ONE FINANCIAL CORP | NYSE |
| GM | `0001467858` | General Motors Co | NYSE |
| NOC | `0001133421` | NORTHROP GRUMMAN CORP /DE/ | NYSE |
| UAL | `0000100517` | United Airlines Holdings, Inc. | Nasdaq |
| HAL | `0000045012` | HALLIBURTON CO | NYSE |
| TSLA | `0001318605` | Tesla, Inc. | Nasdaq |
| LRCX | `0000707549` | LAM RESEARCH CORP | Nasdaq |
| PM | `0001413329` | Philip Morris International Inc. | NYSE |
| SAP | `0001000184` | SAP SE | NYSE |
| IBM | `0000051143` | INTERNATIONAL BUSINESS MACHINES CORP | NYSE |
| ABT | `0000001800` | ABBOTT LABORATORIES | NYSE |
| T | `0000732717` | AT&T INC. | NYSE |
| PG | `0000080424` | PROCTER & GAMBLE Co | NYSE |
| UNP | `0000100885` | UNION PACIFIC CORP | NYSE |
| WDC | `0000106040` | WESTERN DIGITAL CORP | Nasdaq |
| SPOT | `0001639920` | Spotify Technology S.A. | NYSE |
| HON | `0000773840` | HONEYWELL INTERNATIONAL INC | Nasdaq |
| AXP | `0000004962` | AMERICAN EXPRESS CO | NYSE |
| INFY | `0001067491` | Infosys Ltd | NYSE |

All 23 requested symbols matched exactly one row in the SEC
[ticker/CIK/name/exchange mapping](https://www.sec.gov/files/company_tickers_exchange.json).
The 22 new CIKs are distinct and disjoint from the existing universe. Retrieved
`2026-10-16T00:22:02.770247+00:00`; source Last-Modified
`2026-10-15T13:39:20Z`; source SHA-256:
`d04923d674c6d0084dc56e67efd9cb5756f965366affe9d3e18d38b379ffd45f`.

The 22 additions were applied as a separate create-only transaction, preserving
the requested order after omitting the existing META record. Strong verification
at `2026-10-16T00:23:28.861765Z` (October 15 locally) matched all 72 records exactly,
including all 50 unchanged original records. The additions are now included in
`infra/seed/companies.prod.json`; the separate additions file has been removed.
The consolidated file uses a new request token because its transaction contents
differ from both applied requests. Use the loader for repeat loads; do not replay
the raw transaction as a readiness check.

The enabled-company index subsequently returned all 72 CIKs. The running service's
Yahoo refresh at `2026-10-16T00:23:53.035186Z` included all 72 companies, confirming
runtime pickup without restart. It failed with `IncompleteCalendarSnapshot`;
CloudWatch shows the same error before this update (including `00:10:34Z`). The
last successful calendar refresh remained `2026-10-13T14:48:45.962504Z` for the
original scope. This pre-existing provider issue does not disable safety polling;
the universe update does not establish fresh calendar coverage.

Normal five-minute runtime reloads discover company changes without an image
release or restart. Calendar observations for additions arrive with the next
scheduled full Yahoo refresh; an already running refresh loop retains its daily
deadline. Until then, companies without expectations use safety polling. A restart
would refresh mismatched scope, but is not needed to enable filing discovery.
The fixed inclusive `2026-10-06T04:00:00Z` acceptance cutoff also applies to these
newly enabled companies. No historical submissions traversal is introduced.

The calendar-only diagnostic reads the consolidated file directly, covering all
92 reviewed companies without applying any DynamoDB writes:

```bash
.venv/bin/python -m finbot_ingestion.calendar.check \
  --companies-file infra/seed/companies.prod.json --days 30 \
  --output /private/tmp/finbot-calendar-check-NEW-ID.json
```

The finite Fargate `--deployment-check` still requires its original reviewed
50-company count. It is not a current expanded-universe readiness check; review
and update that diagnostic's expected scope before using it against the expanded universe.

## October 26, 2026 additions

The user requested 23 symbols in this order:

```text
V
KO
UNH
NVS
BA
STX
GLW
FCX
UPS
MSFT
TSLA
IBM
TMUS
ADP
FDX
INTC
MO
SBUX
COIN
LYV
CZR
JBLU
RIVN
```

`UNH`, `TSLA` and `IBM` were already present and remain included once. The following
20 new companies extend the reviewed seed to **92 companies**. The previous 72
records retain their order and attributes; additions follow the supplied order.

| Ticker | CIK | SEC registrant name | Exchange |
| --- | --- | --- | --- |
| V | `0001403161` | VISA INC. | NYSE |
| KO | `0000021344` | COCA COLA CO | NYSE |
| NVS | `0001114448` | NOVARTIS AG | NYSE |
| BA | `0000012927` | BOEING CO | NYSE |
| STX | `0001137789` | Seagate Technology Holdings plc | Nasdaq |
| GLW | `0000024741` | CORNING INC /NY | NYSE |
| FCX | `0000831259` | FREEPORT-MCMORAN INC | NYSE |
| UPS | `0001090727` | UNITED PARCEL SERVICE INC | NYSE |
| MSFT | `0000789019` | MICROSOFT CORP | Nasdaq |
| TMUS | `0001283699` | T-Mobile US, Inc. | Nasdaq |
| ADP | `0000008670` | AUTOMATIC DATA PROCESSING INC | Nasdaq |
| FDX | `0001048911` | FEDEX CORP | NYSE |
| INTC | `0000050863` | INTEL CORP | Nasdaq |
| MO | `0000764180` | ALTRIA GROUP, INC. | NYSE |
| SBUX | `0000829224` | STARBUCKS CORP | Nasdaq |
| COIN | `0001679788` | Coinbase Global, Inc. | Nasdaq |
| LYV | `0001335258` | Live Nation Entertainment, Inc. | NYSE |
| CZR | `0001590895` | Caesars Entertainment, Inc. | Nasdaq |
| JBLU | `0001158463` | JETBLUE AIRWAYS CORP | Nasdaq |
| RIVN | `0001874178` | Rivian Automotive, Inc. / DE | Nasdaq |

All 23 symbols matched exactly one row in the SEC
[ticker/CIK/name/exchange mapping](https://www.sec.gov/files/company_tickers_exchange.json).
The 20 new CIKs are distinct and disjoint from the previous universe. Retrieved
`2026-10-26T23:13:00.972078+00:00`; source Last-Modified
`Fri, 23 Oct 2026 21:00:38 GMT`; source SHA-256:
`f5a8b5c09137999ca3815f91df8b97a3602f24d0818255001e38bc77aae9c472`. The full source is retained outside Git.

The additive loader inserted all 20 missing records and skipped the previous 72
with no existing-record differences. Strong base-table verification at
`2026-10-26T23:14:53.006Z` matched all 92 records exactly to the reviewed seed, including
all 72 unchanged original records, with no missing or extra companies. The
enabled-company index independently returned 92 records. Verification evidence is
retained outside Git at `/private/tmp/finbot-universe-20261026-production-verification.json`.

Normal five-minute company reloads pick up additions without a release or restart;
calendar observations wait for the next scheduled refresh. Runtime pickup and fresh
92-company calendar coverage were not verified by this data update. The fixed live
acceptance cutoff remains `2026-10-06T04:00:00Z`. No service restart or release was
performed.
