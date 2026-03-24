#!/usr/bin/env python3
"""
Match RPBBI companies and concessions against FSC certification data.

Two types of matching:
  1. PBPHH mills (from RPBBI header) -> FSC COC certificates (chain of custody)
  2. Concession suppliers (RPBBI sections II,V,X,XVII) -> FSC FM certificates (forest management)

Matching strategy:
  - Normalize names (strip PT/CV/UD, uppercase, remove punctuation)
  - Use token-based Jaccard similarity with IDF weighting to down-weight
    common Indonesian business words (JAYA, LESTARI, UTAMA, INDONESIA, etc.)
  - Require a minimum number of "distinctive" tokens to match
  - Province matching as a secondary confirmation signal
  - Output confidence levels: high (>=0.8), medium (>=0.6), low (>=0.4)

Usage:
    python3 match_rpbbi_fsc.py rpbbi_extracted.csv fsc_indonesia_all.csv fsc_indonesia_details_fm.csv
    python3 match_rpbbi_fsc.py rpbbi_extracted.csv fsc_indonesia_all.csv fsc_indonesia_details_fm.csv -o matches.csv
"""

import argparse
import csv
import math
import re
import sys
from collections import Counter


# --- Name normalization ---

LEGAL_SUFFIXES = re.compile(
    r'\b(PT|CV|UD|PD|PD\.|PERUM|TBK|KOP|KSU|GAPOKTANHUT|LKMA|KTH)\b\.?',
    re.IGNORECASE
)

# Parenthetical notes to strip for matching (but preserve for display)
PAREN_NOTES = re.compile(r'\([^)]*\)')

# Common words in Indonesian forestry company names - these get low IDF weight
# automatically, but we also use them to detect "distinctive" vs "generic" tokens
STOP_WORDS = {
    'INDONESIA', 'JAYA', 'LESTARI', 'UTAMA', 'ABADI', 'MAKMUR', 'INDAH',
    'MANDIRI', 'KARYA', 'PERKASA', 'PRATAMA', 'PRIMA', 'INDO', 'SEJAHTERA',
    'SURYA', 'SUMBER', 'NUSANTARA', 'TIMBER', 'WOOD', 'KAYU', 'INDUSTRY',
    'INDUSTRIES', 'INDUSTRI', 'GROUP', 'FURNITURE', 'INTERNATIONAL',
    'CORPORATION', 'CORP', 'UNIT', 'HUTAN', 'RIMBA', 'WANA',
}


def normalize_name(name):
    """Normalize a company name for matching."""
    name = name.upper()
    # Remove legal entity types
    name = LEGAL_SUFFIXES.sub('', name)
    # Remove parenthetical notes
    name = PAREN_NOTES.sub('', name)
    # Remove punctuation
    name = re.sub(r'[,\.\(\)\{\}\[\]\'":\-/]+', ' ', name)
    # Collapse whitespace
    name = re.sub(r'\s+', ' ', name).strip()
    return name


def tokenize(name):
    """Split normalized name into tokens, filtering short ones."""
    return [w for w in name.split() if len(w) > 1]


def is_distinctive(token):
    """Check if a token is distinctive (not a common business word)."""
    return token not in STOP_WORDS and len(token) > 2


# --- IDF-weighted Jaccard similarity ---

class NameMatcher:
    """Fuzzy name matcher using IDF-weighted token similarity."""

    def __init__(self, reference_names):
        """Build IDF weights from a corpus of reference names."""
        self.reference_names = reference_names
        self.doc_count = len(reference_names)

        # Count document frequency for each token
        df = Counter()
        for name in reference_names:
            tokens = set(tokenize(normalize_name(name)))
            for t in tokens:
                df[t] += 1

        # Compute IDF: log(N / df) -- rare tokens get high weight
        self.idf = {}
        for token, freq in df.items():
            self.idf[token] = math.log(self.doc_count / freq) if freq > 0 else 0

        # Pre-tokenize all reference names
        self.ref_tokens = {}
        for name in reference_names:
            self.ref_tokens[name] = set(tokenize(normalize_name(name)))

    def get_idf(self, token):
        """Get IDF weight for a token (unseen tokens get max weight)."""
        return self.idf.get(token, math.log(self.doc_count))

    def weighted_jaccard(self, tokens_a, tokens_b):
        """
        Compute IDF-weighted Jaccard similarity.
        Weight each token by its IDF, so rare/distinctive tokens matter more.
        """
        if not tokens_a or not tokens_b:
            return 0.0

        intersection = tokens_a & tokens_b
        union = tokens_a | tokens_b

        if not union:
            return 0.0

        weighted_inter = sum(self.get_idf(t) for t in intersection)
        weighted_union = sum(self.get_idf(t) for t in union)

        return weighted_inter / weighted_union if weighted_union > 0 else 0.0

    def distinctive_overlap(self, tokens_a, tokens_b):
        """Count how many distinctive tokens overlap."""
        distinctive_a = {t for t in tokens_a if is_distinctive(t)}
        distinctive_b = {t for t in tokens_b if is_distinctive(t)}
        return len(distinctive_a & distinctive_b)

    def find_matches(self, query_name, min_score=0.4, top_n=3):
        """
        Find best matches for a query name.
        Returns list of (ref_name, score, distinctive_count, confidence) tuples.
        """
        query_tokens = set(tokenize(normalize_name(query_name)))
        if not query_tokens:
            return []

        results = []
        for ref_name, ref_tokens in self.ref_tokens.items():
            score = self.weighted_jaccard(query_tokens, ref_tokens)
            if score >= min_score:
                dist_count = self.distinctive_overlap(query_tokens, ref_tokens)
                results.append((ref_name, score, dist_count))

        # Sort by score descending
        results.sort(key=lambda x: (-x[1], -x[2]))

        # Assign confidence levels
        output = []
        for ref_name, score, dist_count in results[:top_n]:
            if score >= 0.8 and dist_count >= 1:
                confidence = "high"
            elif score >= 0.6 and dist_count >= 1:
                confidence = "medium"
            elif score >= 0.8 and dist_count == 0:
                confidence = "medium"  # High overlap but only common words
            else:
                confidence = "low"
            output.append((ref_name, score, dist_count, confidence))

        return output


# --- Province matching ---

PROVINCE_ALIASES = {
    # Map RPBBI province names to FSC Address_State patterns
    "Jawa Timur": ["Jawa Timur", "East Java", "Province of East Java"],
    "Jawa Tengah": ["Jawa Tengah", "Central Java", "Province of Central Java"],
    "Jawa Barat": ["Jawa Barat", "West Java", "Province of West Java"],
    "Kalimantan Tengah": ["Kalimantan Tengah", "Central Kalimantan"],
    "Kalimantan Timur": ["Kalimantan Timur", "East Kalimantan"],
    "Kalimantan Barat": ["Kalimantan Barat", "West Kalimantan"],
    "Kalimantan Selatan": ["Kalimantan Selatan", "South Kalimantan"],
    "Kalimantan Utara": ["Kalimantan Utara", "North Kalimantan"],
    "Maluku": ["Maluku", "Province of Maluku"],
    "Maluku Utara": ["Maluku Utara", "North Maluku"],
    "Riau": ["Riau", "Province of Riau"],
    "Jambi": ["Jambi", "Province of Jambi"],
    "Sumatera Utara": ["Sumatera Utara", "North Sumatra", "North Sumatera"],
    "Sumatera Selatan": ["Sumatera Selatan", "South Sumatra", "South Sumatera"],
}


def provinces_match(rpbbi_province, fsc_state):
    """Check if RPBBI province matches FSC address state."""
    if not rpbbi_province or not fsc_state:
        return None  # Unknown
    fsc_upper = fsc_state.upper()
    aliases = PROVINCE_ALIASES.get(rpbbi_province, [rpbbi_province])
    for alias in aliases:
        if alias.upper() in fsc_upper:
            return True
    # Also try direct substring
    if rpbbi_province.upper() in fsc_upper:
        return True
    return False


# --- Main pipeline ---

def load_rpbbi(path):
    """Load RPBBI extracted data."""
    with open(path, encoding='utf-8') as f:
        return list(csv.DictReader(f))


def load_fsc_all(path):
    """Load FSC all certificates."""
    with open(path, encoding='utf-8') as f:
        return list(csv.DictReader(f))


def load_fsc_fm(path):
    """Load FSC FM details."""
    with open(path, encoding='utf-8') as f:
        return list(csv.DictReader(f))


def clean_fsc_org(name):
    """Remove quotes from FSC organization names."""
    return name.strip('"').strip()


def match_mills(rpbbi_data, fsc_all_data):
    """Match PBPHH mills against FSC COC certificates."""
    # Get unique PBPHH companies with their locations
    mills = {}
    for r in rpbbi_data:
        company = r['pbphh_company']
        if company not in mills:
            mills[company] = r['pbphh_location']

    # Build FSC COC reference set (certificate holders only for primary match)
    coc_names = {}
    for r in fsc_all_data:
        if r['CertificateType'] == 'COC':
            org = clean_fsc_org(r['Organization'])
            if org not in coc_names:
                coc_names[org] = {
                    'status': r['CertifiedStatus'],
                    'license': r['License'],
                    'code': r['CertificateCode'],
                    'role': r['Role'],
                }

    # Also include all types for broader matching
    all_names = {}
    for r in fsc_all_data:
        org = clean_fsc_org(r['Organization'])
        if org not in all_names:
            all_names[org] = {
                'status': r['CertifiedStatus'],
                'license': r['License'],
                'code': r['CertificateCode'],
                'type': r['CertificateType'],
                'role': r['Role'],
            }

    matcher = NameMatcher(list(coc_names.keys()))

    results = []
    for mill_name, location in mills.items():
        matches = matcher.find_matches(mill_name, min_score=0.4, top_n=3)

        if matches:
            for ref_name, score, dist_count, confidence in matches:
                info = coc_names[ref_name]
                results.append({
                    'match_type': 'mill_to_coc',
                    'rpbbi_name': mill_name,
                    'rpbbi_location': location,
                    'fsc_name': ref_name,
                    'fsc_cert_type': 'COC',
                    'fsc_status': info['status'],
                    'fsc_license': info['license'],
                    'fsc_cert_code': info['code'],
                    'fsc_role': info['role'],
                    'score': f"{score:.3f}",
                    'distinctive_tokens': dist_count,
                    'confidence': confidence,
                    'province_match': '',
                })
        else:
            results.append({
                'match_type': 'mill_to_coc',
                'rpbbi_name': mill_name,
                'rpbbi_location': location,
                'fsc_name': '(no match)',
                'fsc_cert_type': '',
                'fsc_status': '',
                'fsc_license': '',
                'fsc_cert_code': '',
                'fsc_role': '',
                'score': '',
                'distinctive_tokens': '',
                'confidence': 'none',
                'province_match': '',
            })

    return results


def match_concessions(rpbbi_data, fsc_fm_data):
    """Match concession suppliers against FSC FM certificates."""
    # Get unique concessions from relevant sections
    fm_sections = {
        'iuphhk_hutan_alam', 'iuphhk_hti', 'iuphhk_restorasi',
        'ils_ipk', 'area_penyiapan_ht',
    }
    concessions = {}
    for r in rpbbi_data:
        if r['section_category'] in fm_sections:
            name = r['entry_name']
            if name not in concessions:
                concessions[name] = {
                    'province': r['province'],
                    'section': r['section_category'],
                    'pbphh': r['pbphh_company'],
                }

    # Build FSC FM reference
    fm_names = {}
    for r in fsc_fm_data:
        org = clean_fsc_org(r['Organization'])
        key = f"{org}|{r['CertificateCode']}"
        if key not in fm_names:
            fm_names[org] = {
                'status': r['CertifiedStatus'],
                'license': r['License'],
                'code': r['CertificateCode'],
                'type': r['CertificateType'],
                'role': r['Role'],
                'area': r.get('CertifiedForestArea', ''),
                'state': r.get('Address_State', ''),
            }

    matcher = NameMatcher(list(fm_names.keys()))

    results = []
    for conc_name, info in concessions.items():
        matches = matcher.find_matches(conc_name, min_score=0.4, top_n=3)

        if matches:
            for ref_name, score, dist_count, confidence in matches:
                fsc_info = fm_names[ref_name]

                # Province cross-check
                prov_match = provinces_match(info['province'], fsc_info['state'])
                prov_str = "yes" if prov_match is True else ("no" if prov_match is False else "unknown")

                # Boost confidence if province matches, downgrade if it doesn't
                if prov_match is True and confidence == "medium":
                    confidence = "high"
                elif prov_match is False and confidence != "low":
                    confidence = "low"

                results.append({
                    'match_type': 'concession_to_fm',
                    'rpbbi_name': conc_name,
                    'rpbbi_location': info['province'],
                    'fsc_name': ref_name,
                    'fsc_cert_type': fsc_info['type'],
                    'fsc_status': fsc_info['status'],
                    'fsc_license': fsc_info['license'],
                    'fsc_cert_code': fsc_info['code'],
                    'fsc_role': fsc_info['role'],
                    'score': f"{score:.3f}",
                    'distinctive_tokens': dist_count,
                    'confidence': confidence,
                    'province_match': prov_str,
                })
        else:
            results.append({
                'match_type': 'concession_to_fm',
                'rpbbi_name': conc_name,
                'rpbbi_location': info['province'],
                'fsc_name': '(no match)',
                'fsc_cert_type': '',
                'fsc_status': '',
                'fsc_license': '',
                'fsc_cert_code': '',
                'fsc_role': '',
                'score': '',
                'distinctive_tokens': '',
                'confidence': 'none',
                'province_match': '',
            })

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Match RPBBI companies/concessions against FSC certification"
    )
    parser.add_argument("rpbbi_csv", help="RPBBI extracted CSV")
    parser.add_argument("fsc_all_csv", help="FSC all certificates CSV")
    parser.add_argument("fsc_fm_csv", help="FSC FM details CSV")
    parser.add_argument("--output", "-o", default=None, help="Output CSV")
    parser.add_argument("--min-score", type=float, default=0.4, help="Minimum match score")
    args = parser.parse_args()

    rpbbi = load_rpbbi(args.rpbbi_csv)
    fsc_all = load_fsc_all(args.fsc_all_csv)
    fsc_fm = load_fsc_fm(args.fsc_fm_csv)

    print(f"RPBBI entries: {len(rpbbi)}", file=sys.stderr)
    print(f"FSC certificates: {len(fsc_all)}", file=sys.stderr)
    print(f"FSC FM details: {len(fsc_fm)}", file=sys.stderr)

    # Run matching
    mill_results = match_mills(rpbbi, fsc_all)
    conc_results = match_concessions(rpbbi, fsc_fm)

    all_results = mill_results + conc_results

    # Output
    fieldnames = [
        'match_type', 'rpbbi_name', 'rpbbi_location',
        'fsc_name', 'fsc_cert_type', 'fsc_status', 'fsc_license', 'fsc_cert_code', 'fsc_role',
        'score', 'distinctive_tokens', 'confidence', 'province_match',
    ]

    out = open(args.output, 'w', newline='', encoding='utf-8') if args.output else sys.stdout
    writer = csv.DictWriter(out, fieldnames=fieldnames)
    writer.writeheader()
    for row in all_results:
        writer.writerow(row)
    if args.output:
        out.close()

    # Summary
    high = sum(1 for r in all_results if r['confidence'] == 'high')
    med = sum(1 for r in all_results if r['confidence'] == 'medium')
    low = sum(1 for r in all_results if r['confidence'] == 'low')
    none_ = sum(1 for r in all_results if r['confidence'] == 'none')
    mill_matched = sum(1 for r in mill_results if r['confidence'] != 'none')
    conc_matched = sum(1 for r in conc_results if r['confidence'] != 'none')

    print(f"\n=== Match Summary ===", file=sys.stderr)
    print(f"Mills:       {mill_matched} matched / {len(set(r['rpbbi_name'] for r in mill_results))} total", file=sys.stderr)
    print(f"Concessions: {conc_matched} matched / {len(set(r['rpbbi_name'] for r in conc_results))} total", file=sys.stderr)
    print(f"\nConfidence: high={high}, medium={med}, low={low}, none={none_}", file=sys.stderr)


if __name__ == "__main__":
    main()
