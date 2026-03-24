#!/usr/bin/env python3
"""
Extract company names and concession/supplier names from RPBBI PDF documents.

RPBBI = Rencana Pemenuhan Bahan Baku Industri (Industrial Raw Material Supply Plan)
These are standardized Indonesian government forms listing timber processing mills
and their raw material sources (concessions, community forests, suppliers, etc.)

Usage:
    python3 extract_rpbbi.py "RPBBI sample/"
    python3 extract_rpbbi.py "RPBBI sample/" --output results.csv
"""

import argparse
import csv
import glob
import os
import re
import sys

import pdfplumber


# Section definitions: Roman numeral -> (section_id, category, description)
SECTIONS = {
    "I":     ("I",     "persediaan_akhir",      "Persediaan Akhir di IPHHK (Previous Year Stock)"),
    "II":    ("II",    "iuphhk_hutan_alam",      "IUPHHK Pada Hutan Alam (Natural Forest Concession)"),
    "III":   ("III",   "iuphhk_restorasi",       "IUPHHK Restorasi Ekosistem (Ecosystem Restoration)"),
    "IV":    ("IV",    "htr_hkm",                "HTR/HKm (Community Forestry)"),
    "V":     ("V",     "iuphhk_hti",             "IUPHHK HTI (Industrial Plantation)"),
    "VI":    ("VI",    "hutan_rakyat_alami",      "Hutan Rakyat Kayu Tumbuh Alami (Natural Community Forest)"),
    "VII":   ("VII",   "hkm",                    "Hutan Kemasyarakatan (Community Forest)"),
    "VIII":  ("VIII",  "perum_perhutani",         "Perum Perhutani (State Forestry Corp)"),
    "IX":    ("IX",    "kph",                     "Kesatuan Pengelolaan Hutan (Forest Management Unit)"),
    "X":     ("X",     "ils_ipk",                "Izin Lainnya (ILS/IPK - Other Permits)"),
    "XI":    ("XI",    "hutan_rakyat_budidaya",   "Hutan Rakyat Kayu Tanaman Budidaya (Cultivated Community Timber)"),
    "XII":   ("XII",   "kayu_perkebunan",         "Kayu Perkebunan (Plantation Timber)"),
    "XIII":  ("XIII",  "impor",                   "Impor Kayu (Timber Import)"),
    "XIV":   ("XIV",   "lelang",                  "Hasil Lelang (Auction Timber)"),
    "XV":    ("XV",    "tptkb",                   "TPTKB/TPKRT/Pengumpul Limbah (Collection Points/Mill Waste)"),
    "XVI":   ("XVI",   "iphhk_lain",             "IPHHK Lain (Other Timber Processors)"),
    "XVII":  ("XVII",  "area_penyiapan_ht",       "Area Penyiapan Lahan HT (Plantation Land Preparation)"),
}

# Roman numeral pattern for section headers in column 0 (No.)
ROMAN_RE = re.compile(r'^(I{1,3}|IV|VI{0,3}|IX|XI{0,3}|XIV|XV|XVI{0,3}|XVII)\.$')

TABLE_SETTINGS = {
    "vertical_strategy": "lines",
    "horizontal_strategy": "text",
    "snap_tolerance": 5,
}


def cell_text(cell):
    """Clean a table cell value."""
    if cell is None:
        return ""
    return str(cell).replace('\n', ' ').strip()


def extract_table_rows(pdf_path):
    """Extract all table rows across all pages, returning (col0, col1, col2, col3) tuples.
    col0=No, col1=Source/Name, col2=Province, col3=Rencana Tahun Ini
    """
    rows = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables(TABLE_SETTINGS)
            for table in tables:
                for row in table:
                    if len(row) >= 4:
                        rows.append((
                            cell_text(row[0]),
                            cell_text(row[1]),
                            cell_text(row[2]),
                            cell_text(row[3]),
                        ))
    return rows


def extract_text(pdf_path):
    """Extract plain text for header parsing."""
    with pdfplumber.open(pdf_path) as pdf:
        parts = []
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                parts.append(text)
    return "\n".join(parts)


def parse_header(text):
    """Extract PBPHH company name and location from header."""
    pbphh_match = re.search(r'PBPHH\s*:\s*(.+)', text)
    lokasi_match = re.search(r'Lokasi\s+(.+)', text)
    tahun_match = re.search(r'TAHUN\s+(\d{4})\s+Sampai\s+Dengan\s+Bulan\s+(\w+)', text)

    return {
        "company": pbphh_match.group(1).strip() if pbphh_match else "",
        "location": lokasi_match.group(1).strip() if lokasi_match else "",
        "year": tahun_match.group(1) if tahun_match else "",
        "month": tahun_match.group(2) if tahun_match else "",
    }


def parse_table_rows(rows):
    """
    Parse structured table rows to extract entries with proper column separation.

    Each row is (col0_no, col1_source, col2_province, col3_rencana).
    Multi-line entries have data only in col1/col2 with col0 empty.
    """
    entries = []
    current_section = None
    current_subsection = None

    # Accumulator for multi-row entries
    pending_entry = None  # (entry_num, name_parts, prov_parts, rencana)

    def flush_pending():
        """Finalize and append the pending entry."""
        nonlocal pending_entry
        if pending_entry is None:
            return
        entry_num, name_parts, prov_parts, rencana = pending_entry

        name = " ".join(name_parts).strip()
        province = " ".join(prov_parts).strip()

        # Clean up name
        name = re.sub(r'\s+', ' ', name).rstrip(',').strip()
        province = re.sub(r'\s+', ' ', province).strip()

        if name and not name.startswith("atas nama") and current_section != "I":
            section_info = SECTIONS.get(current_section, (current_section, "unknown", "Unknown"))
            category = section_info[1]
            if current_section == "XVI" and current_subsection:
                sub_labels = {"a": "kayu_bulat", "b": "olahan_setengah_jadi", "c": "limbah"}
                category = f"{category}_{sub_labels.get(current_subsection, current_subsection)}"

            entries.append({
                "section_id": section_info[0],
                "section_category": category,
                "section_description": section_info[2],
                "entry_number": entry_num,
                "entry_name": name,
                "province": province,
                "rencana_m3": rencana,
            })

        pending_entry = None

    for col0, col1, col2, col3 in rows:
        # Skip header rows (but not province-only continuation rows where col2 has data)
        if col0 in ("No.", "") and col1 in (
            "Sumber Atau Asal Usul", "Bahan Baku", "Setengah Jadi",
        ):
            continue
        if "(M3)" in col3 or "Tahun Ini" in col3 or "Bulan" in col3:
            continue
        if not col1 and not col0 and not col2:
            continue

        # Check for section header in col0
        m = ROMAN_RE.match(col0.strip())
        if m:
            flush_pending()
            current_section = m.group(1)
            current_subsection = None
            continue

        if current_section is None:
            continue

        # Stop at totals
        if col1.startswith("Total Kayu Bulat") or col1.startswith("Total Bahan Baku"):
            flush_pending()
            continue

        # Check for XVI sub-sections in col1 - must check BEFORE continuation logic
        # because sub-section headers can appear as continuation text
        if current_section == "XVI":
            sub_match = re.match(r'^([a-c])\.\s*(?:Kayu Bulat|Bahan Baku|Limbah)', col1)
            if sub_match:
                flush_pending()
                current_subsection = sub_match.group(1)
                continue

        # Also check for sub-section headers that might appear in continuation rows
        # (e.g., when page breaks mid-entry before a sub-section header)
        if re.match(r'^[a-c]\.\s*(?:Kayu Bulat|Bahan Baku|Limbah)', col1):
            flush_pending()
            if current_section == "XVI":
                sub_match = re.match(r'^([a-c])\.', col1)
                current_subsection = sub_match.group(1) if sub_match else None
            continue

        # Check for numbered entry start in col1
        entry_match = re.match(r'^(\d+)\.\s*(.*)', col1)
        if entry_match:
            flush_pending()

            entry_num = entry_match.group(1)
            name_text = entry_match.group(2).strip()
            prov_text = col2
            rencana = col3.replace(',', '') if col3 else ""

            pending_entry = (entry_num, [name_text], [prov_text] if prov_text else [], rencana)
            continue

        # Continuation row (col0 is empty, this row continues the previous entry)
        if not col0 and pending_entry is not None and (col1 or col2):
            entry_num, name_parts, prov_parts, rencana = pending_entry
            if col1:
                name_parts.append(col1)
            if col2:
                prov_parts.append(col2)
            pending_entry = (entry_num, name_parts, prov_parts, rencana)
            continue

        # Section description continuation (e.g., "atas nama :", "Tanaman Industri atau")
        if not col0 and col1 and pending_entry is None:
            # This is part of a section header description, skip
            continue

    flush_pending()
    return entries


def process_pdf(pdf_path):
    """Process a single RPBBI PDF and return structured data."""
    text = extract_text(pdf_path)
    header = parse_header(text)

    rows = extract_table_rows(pdf_path)
    entries = parse_table_rows(rows)

    filename = os.path.basename(pdf_path)

    results = []
    for entry in entries:
        results.append({
            "filename": filename,
            "pbphh_company": header["company"],
            "pbphh_location": header["location"],
            "report_year": header["year"],
            "report_month": header["month"],
            **entry,
        })

    if not results:
        results.append({
            "filename": filename,
            "pbphh_company": header["company"],
            "pbphh_location": header["location"],
            "report_year": header["year"],
            "report_month": header["month"],
            "section_id": "",
            "section_category": "",
            "section_description": "",
            "entry_number": "",
            "entry_name": "(no supplier entries found)",
            "province": "",
            "rencana_m3": "",
        })

    return results


def main():
    parser = argparse.ArgumentParser(description="Extract company and concession names from RPBBI PDFs")
    parser.add_argument("input_path", help="Path to PDF file or directory of PDFs")
    parser.add_argument("--output", "-o", default=None, help="Output CSV path (default: stdout)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print progress to stderr")
    args = parser.parse_args()

    if os.path.isdir(args.input_path):
        pdf_files = sorted(glob.glob(os.path.join(args.input_path, "*.pdf")))
    else:
        pdf_files = [args.input_path]

    if not pdf_files:
        print(f"No PDF files found in {args.input_path}", file=sys.stderr)
        sys.exit(1)

    fieldnames = [
        "filename", "pbphh_company", "pbphh_location", "report_year", "report_month",
        "section_id", "section_category", "section_description",
        "entry_number", "entry_name", "province", "rencana_m3",
    ]

    out_file = open(args.output, 'w', newline='', encoding='utf-8') if args.output else sys.stdout
    writer = csv.DictWriter(out_file, fieldnames=fieldnames)
    writer.writeheader()

    total_entries = 0
    for pdf_path in pdf_files:
        if args.verbose:
            print(f"Processing: {os.path.basename(pdf_path)}...", file=sys.stderr)
        try:
            results = process_pdf(pdf_path)
            for row in results:
                writer.writerow(row)
            total_entries += len(results)
            if args.verbose:
                print(f"  -> {len(results)} entries extracted", file=sys.stderr)
        except Exception as e:
            print(f"ERROR processing {pdf_path}: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc(file=sys.stderr)

    if args.output:
        out_file.close()

    print(f"\nDone: {len(pdf_files)} PDFs, {total_entries} total entries", file=sys.stderr)


if __name__ == "__main__":
    main()
