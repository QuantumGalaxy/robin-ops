# Importing DoltHub IV history

The paper watchlist includes Mastercard (`MA`) in place of QQQ.

Install Excel support with `pip install -e '.[excel]'`. CSV imports do not need
that extra dependency. Import the supplied workbook or monthly CSV downloads:

```sh
optionsagent iv-history-import '/path/to/IV History.xlsx' --config config/paper-robinhood.yaml
optionsagent iv-history-status --config config/paper-robinhood.yaml
```

Accepted headers are `symbol,date,iv` or DoltHub's `act_symbol,date,iv_current`.
The XLSX file must contain one populated worksheet; formula cells are not evaluated.
IV must be a positive finite decimal (for example, 0.25 represents 25%). The importer
does not guess units, repair dates, fill gaps, or substitute another stock's data.

Imports persist in `iv-research.sqlite3` under the configured state directory.
They are separate from `iv.sqlite3`, which supplies the execution filter. The source
name, file SHA-256, import time, original rejected rows, and rejection reasons are
recorded. Re-importing the identical file is a no-op. Matching overlapping rows are
deduplicated; conflicting values are preserved as rejected records, not overwritten.
Import subsequent downloads using the same command. There is no automatic vendor
download or recurring subscription configured.

The private dashboard shows each stock's observation count, latest observation date,
missing sessions in the last 252 completed XNYS sessions, and whether the latest
completed session is present. Counts alone do not establish validity. Reference
readiness counts only the current watchlist, so a previous QQQ reference cannot
stand in for missing Mastercard reference data.

## Current source limitations

The provided workbook has 4,865 rows. Import accepted 4,785 into research storage
and flagged 80 rows on non-trading dates: 2025-12-25, 2026-01-01, 2026-02-16,
and 2026-04-03. Its latest date is 2026-09-03; 2026-09-04 is absent.
The meaning of these date labels and the provider's IV aggregation/tenor remain
unverified. Even accepted rows have passed structural checks only.

This command does not promote research data into the execution archive or change
`require_iv_rank`. The paper profile continues with historical IV rank disabled.
Before enabling it, establish the provider's IV definition and observation times,
resolve the holiday labels, validate missing/current sessions, and use the same
definition for both historical and new observations. Comparing an aggregate
underlying IV series directly to an individual option's IV can be misleading.

The existing execution archive now counts only the last 252 completed-session
window, so old observations cannot satisfy its 200-observation minimum.
This data import is not evidence of strategy profitability or live-trading readiness.
