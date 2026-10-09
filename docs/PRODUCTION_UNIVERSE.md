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

## Identity preparation and deployment status

The complete verified ticker/CIK/name mapping remains pending. Before seeding,
resolve each selected symbol to its SEC registrant and validate the existing
`Company(ticker, cik, name, enabled)` contract with ten-digit CIKs and enabled
records. Runtime identity remains CIK-based; it reads the Companies table.

The user confirmed `SCHW` (Charles Schwab) on 2026-10-09, correcting a typo in
the original selection.

This selection does not perform DynamoDB seeding, provision resources or activate
the runtime. Follow [DEPLOYMENT.md](DEPLOYMENT.md) for separately authorized
seeding, live validation and deployment. Ongoing Yahoo access and full 30-day live
coverage validation remain pending.
