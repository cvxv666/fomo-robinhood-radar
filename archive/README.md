# The archive

What the project concluded, kept after the machine that produced it was turned off on
**6 October 2026**. The database itself is not here — it is 283 MB and it is a copy of a public
chain, rebuildable by anyone who runs the pipeline again. These four files are the part nobody
could rebuild: every alert the bot sent and what became of it, the verdict on every wallet it
judged, and the shape of the whole two months.

| file | what it is |
|---|---|
| `alerts.csv` | one row per alert: when, which kind, the entry price the cohort paid, what was known at the push (heat, conviction, wallets, pool depth, the cohort's share of the pool), and what the pool's own candles did afterwards — the peak and when it came, where it stood at the hour, the trail read, the volume that followed. `chats = 0` means it was measured but sent to nobody, which is what launches became after 22 September. |
| `traders.csv` | 558 wallets with a verdict: the score, the status, the model that wrote it, fomo's PnL figures, and the summary in full. This is the output of the part of the product that had an opinion. |
| `tape_by_day.csv` | the tape itself, by day: fills, wallets active, tokens touched, and how many of those fills provenance called seeding rather than trading. |
| `summary.json` | the totals every claim in the README rests on, for the whole run and split by alert kind. |

## The numbers, as they stood at the end

Two months of tape: 6 August – 6 October 2026. 187,306 fills from 562 tracked wallets over 28,524
tokens; 2,943 fomo.family profiles seen, 2,094 of them resolved to the wallet that actually trades.
558 wallets judged.

157 alerts sent, 136 of them measurable afterwards, 105 traded above the call at some point, 30
reached twice the call. A hundred dollars into every one of them, out at the hour read, came to
**−$175**; stepping off at a fifth below the running high, **−$54**.

That total hides the finding the project ended on, and the reason it ended the way it did:

| | alerts | $100 into each, at the hour | at the trail | reached 2× |
|---|---|---|---|---|
| **bursts** — several trusted wallets into one token inside minutes | 67 | **+$1,030** | +$151 | 14 |
| **launches** — the cohort entering something new | 90 | **−$1,205** | −$205 | 16 |

The bursts paid and the launches did not, by almost exactly the same amount. On 22 September the
launch feed was taken off the push entirely and the burst alerts were made free to everyone; the
launches kept being written here, unsent, so the question would stay measurable. Had the product
started as what it finished as, the paper run would read +$1,030 rather than −$175.

Everything else the run produced — the research behind the honeypot and seeding defences, the exit
study, the cohort-share study, the pool-depth study — is in `docs/STATUS.md`, with the dates and
the numbers each decision was made on.
