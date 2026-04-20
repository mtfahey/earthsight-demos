# Earthsight Demos

A pipeline to audit FSC certification claims against Indonesian timber industry data (RPBBI plans from the Ministry of Environment and Forestry).

## Components

### 1. FSC Indonesia scraper — `fetch_fsc_indonesia.py`
Pulls the full FSC certificate dataset for Indonesia via Playwright (reverse-engineered the FSC search API).

- **1,535 / 1,543** certificates captured (99.5%)
- **259 / 259** Forest Management certs (100%)

Outputs:
- `fsc_indonesia_all.csv` — all certificates (COC, FM, FM/COC, CW/FM)
- `fsc_indonesia_fm.csv` — FM-related certificates
- `fsc_indonesia_details_fm.csv` — FM entries with full details (area, dates, addresses)

### 2. RPBBI PDF extraction — `extract_rpbbi.py`
Parses ASP.NET-generated RPBBI PDFs using `pdfplumber` table extraction.

- Pulls company names, concessions/suppliers, provinces, and planned volumes (`rencana_m3`)
- Tested on 10 sample PDFs → **210 supplier entries, 100% province coverage**

```bash
python3 extract_rpbbi.py "RPBBI sample/" -o rpbbi_extracted.csv -v
```

### 3. FSC ↔ RPBBI matcher — `match_rpbbi_fsc.py`
Cross-references RPBBI companies and concessions against FSC certification.

- **IDF-weighted Jaccard similarity** (handles common Indonesian business words like JAYA, LESTARI, UTAMA)
- **Province cross-check** + confidence tiers (high / medium / low)
- Mills matched against COC certs; concessions against FM certs

```bash
python3 match_rpbbi_fsc.py rpbbi_extracted.csv fsc_indonesia_all.csv fsc_indonesia_details_fm.csv -o rpbbi_fsc_matches.csv
```

### 4. Volume-based sourcing analysis
Calculates what % of each FSC-certified mill's timber actually comes from FSC-certified concessions.

Sample result (10-PDF sample, 7 FSC COC-certified mills):
- Only **35.6%** of concession timber volume sourced by COC mills is from FSC concessions
- None of the COC mills source 100% FSC; range: **83.8% → 4.9%**

## Requirements

- Python 3.9+
- `playwright` + chromium: `pip install playwright && python -m playwright install chromium`
- `pdfplumber`

## Data notes

RPBBI sample PDFs are not included in this repo — contact Earthsight/Auriga for their "Risky Business" dataset (~10,000 documents).
