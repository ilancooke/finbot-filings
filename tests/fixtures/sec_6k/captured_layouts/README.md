Public SEC captures read October 20, 2026 using the identified SEC client.
Fixture filenames describe the layout/selection behavior; capture metadata retains
original SEC identities and document filenames for traceability.

| Fixture | Behavior | Source document |
| --- | --- | --- |
| `wrapped_description_primary.html` | Quoted title and exhibit description split across positioned lines | ERIC `0001628280-26-065962`, `a2610156-kq326.htm` |
| `generic_release_primary.html` | Generic release label beside an excluded presentation | TSM `0001046179-26-000726`, `tsm-20261015x6k.htm` |
| `generic_release_attachment.html` | Opening paragraph explicitly reports quarterly financial results | Same TSM filing, `a3q26e_withguidancexfinal.htm` (28,001 bytes) |

`*_package.json` contains the enumerated SEC index/directory metadata returned
by `SecClient.get_filing_index`. All original document URLs are included there.

HTML files preserve original bytes, including separate absolutely positioned
title/exhibit lines and split links. The wrapped-description case's large results
attachment is not captured: it qualifies from the cover's Exhibit 99.1 description.
Tests use synthetic bytes for acquiring that already-selected attachment, and
deterministic test timestamps instead of live acceptance/discovery times.
