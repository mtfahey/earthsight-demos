#!/usr/bin/env python3.9
"""
Fetch all FSC-certified certificates for Indonesia from search.fsc.org.

Uses Playwright to interact with the FSC search API via POST requests,
which avoids URL encoding issues with certificate types containing
slashes (FM/COC, CW/FM) and supports full pagination.

Outputs:
  - fsc_indonesia_all.csv         — all certificate types (complete)
  - fsc_indonesia_fm.csv          — forest management certificates (FM, FM/COC, CW/FM)
  - fsc_indonesia_details_fm.csv  — detailed info for FM-related certificates

Requirements:
    pip3 install playwright
    python3.9 -m playwright install chromium

Usage:
    python3.9 fetch_fsc_indonesia.py
"""

import asyncio
import csv
import json
import os
import sys
import time

BASE_URL = "https://search.fsc.org"
OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
PAGE_SIZE = 100
API_CAP = 500


def cert_key(row):
    """Composite key — sites share Ids with their certificate holder."""
    return (row.get("Id", ""), row.get("Organization", ""), row.get("Role", ""))


def extract_cert_type(certificate_code):
    """Extract type from code like 'SCS-FM/COC-009836' -> 'FM/COC'."""
    if not certificate_code:
        return "Unknown"
    parts = certificate_code.split("-")
    return parts[1] if len(parts) >= 2 else "Unknown"


def is_fm_related(certificate_code):
    """Check if certificate code indicates forest management."""
    return "FM" in extract_cert_type(certificate_code)


def save_csv(rows, filepath, fieldnames):
    """Save rows to CSV."""
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"  Saved {len(rows)} rows to {os.path.basename(filepath)}")


class FSCBrowserClient:
    """Client that uses Playwright page.evaluate() for authenticated API calls."""

    def __init__(self, page):
        self.page = page

    async def _fetch_json(self, method, path, body=None):
        """Make an authenticated fetch call via the browser context."""
        js = """async ([method, path, bodyStr]) => {
            const xsrf = decodeURIComponent(
                document.cookie.match(/XSRF-TOKEN=([^;]+)/)?.[1] || ''
            );
            const opts = {
                method,
                credentials: 'include',
                headers: {
                    'Accept': 'application/json',
                    'x-xsrf-token': xsrf,
                },
            };
            if (bodyStr) {
                opts.headers['Content-Type'] = 'application/json-patch+json';
                opts.body = bodyStr;
            }
            const res = await fetch(path, opts);
            return { status: res.status, body: await res.text() };
        }"""
        result = await self.page.evaluate(
            js, [method, path, json.dumps(body) if body else None]
        )
        if result["status"] == 200 and result["body"]:
            return json.loads(result["body"])
        if result["status"] != 200:
            print(f"    Warning: HTTP {result['status']} for {method} {path[:80]}")
        return None

    async def search_page(self, filters, page_num=1):
        """Fetch a single page of search results via POST."""
        body = {
            "Country": ["Indonesia"],
            "CertificateType": filters.get("types", []),
            "CertificateStatus": filters.get("statuses", []),
            "CertificateBody": filters.get("bodies", []),
            "Role": [],
            "ProductType": [],
            "TreeSpecies": [],
            "RegulatoryModule": [],
            "SalesActivityStatus": [],
            "PageSize": PAGE_SIZE,
            "PageNumber": page_num,
            "OrderColumn": "Organization",
            "OrderDirection": "Asc",
        }
        return await self._fetch_json(
            "POST", "/api/v1/Certification/Search?certificateUri=false", body
        )

    async def search_all(self, **filters):
        """Fetch all pages for a search query. Returns (rows, total)."""
        all_rows = []
        total = 0
        page_num = 1

        while True:
            data = await self.search_page(filters, page_num)
            if not data:
                break

            rows = data.get("Rows", [])
            page_count = data.get("Count", 0)
            if page_count > 0:
                total = page_count

            if not rows:
                break

            all_rows.extend(rows)

            if data.get("IsLast", True) or page_num >= data.get("TotalPages", 1):
                break

            page_num += 1
            await asyncio.sleep(0.15)

        return all_rows, total

    async def get_bodies(self):
        """Get all available certification body codes."""
        data = await self._fetch_json(
            "GET", "/api/v1/Certification/Filters/AllCertificationBody"
        )
        if isinstance(data, list):
            return [b["Value"] if isinstance(b, dict) else b for b in data]
        return []

    async def get_details(self, cert_id):
        """Fetch header + address for a certificate."""
        header = await self._fetch_json(
            "GET", f"/api/v1/Certification/Details/{cert_id}/Header"
        )
        address = await self._fetch_json(
            "GET", f"/api/v1/Certification/Details/{cert_id}/Address"
        )
        return header, address


async def fetch_type_complete(client, cert_type, all_bodies):
    """Fetch all certificates for a type, splitting by status/body if needed."""
    rows, total = await client.search_all(types=[cert_type])
    fetched = len(rows)
    print(f"  {cert_type}: {fetched}/{total}")

    if fetched >= total:
        return rows

    # Split by status
    all_rows = {cert_key(r): r for r in rows}
    print(f"    Splitting {cert_type} by status...")

    for status in ["Valid", "Terminated", "Suspended"]:
        s_rows, s_total = await client.search_all(
            types=[cert_type], statuses=[status]
        )
        if s_total == 0:
            continue

        print(f"    {cert_type} + {status}: {len(s_rows)}/{s_total}")
        for r in s_rows:
            all_rows[cert_key(r)] = r

        if len(s_rows) >= s_total:
            continue

        # Still capped — split further by certification body
        print(f"      Splitting {cert_type} + {status} by body...")
        for body in all_bodies:
            b_rows, b_total = await client.search_all(
                types=[cert_type], statuses=[status], bodies=[body]
            )
            if b_total > 0:
                print(f"        {body}: {len(b_rows)}/{b_total}")
                for r in b_rows:
                    all_rows[cert_key(r)] = r
            await asyncio.sleep(0.1)

    result = list(all_rows.values())
    print(f"    {cert_type} total unique: {len(result)}")
    return result


async def main():
    from playwright.async_api import async_playwright

    print("FSC Indonesia Certificate Fetcher (Playwright)")
    print("=" * 55)

    print("\n1. Launching browser...")
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    page = await browser.new_page()

    print("  Navigating to search.fsc.org...")
    await page.goto(f"{BASE_URL}/search/", wait_until="networkidle", timeout=30000)
    print("  Session established")

    client = FSCBrowserClient(page)

    # Get certification bodies for potential splitting
    all_bodies = await client.get_bodies()
    print(f"  {len(all_bodies)} certification bodies available")

    all_certs = {}

    print("\n2. Fetching certificates by type...")
    for cert_type in ["FM", "FM/COC", "CW/FM", "COC"]:
        rows = await fetch_type_complete(client, cert_type, all_bodies)
        for r in rows:
            all_certs[cert_key(r)] = r

    # Convert to sorted list and add derived type
    all_list = sorted(all_certs.values(), key=lambda x: x.get("Organization", ""))
    for cert in all_list:
        cert["CertificateType"] = extract_cert_type(cert.get("CertificateCode", ""))

    print(f"\n  Total unique certificates: {len(all_list)}")

    # Save all certificates
    all_fields = [
        "Id", "OrganizationId", "CertifiedStatus", "License",
        "CertificateCode", "CertificateType", "Organization", "Role",
        "Country", "CertificateUrl", "SalesActivityStatus",
    ]
    all_path = os.path.join(OUTPUT_DIR, "fsc_indonesia_all.csv")
    save_csv(all_list, all_path, all_fields)

    # Filter FM-related certificates
    print("\n3. Filtering FM-related certificates...")
    fm_certs = [c for c in all_list if is_fm_related(c.get("CertificateCode", ""))]
    print(f"  Found {len(fm_certs)} FM-related certificates")

    fm_path = os.path.join(OUTPUT_DIR, "fsc_indonesia_fm.csv")
    save_csv(fm_certs, fm_path, all_fields)

    # Fetch detailed info for FM certificates
    # Details are per certificate Id, not per site — fetch once per unique Id
    if fm_certs:
        unique_ids = {}
        for cert in fm_certs:
            cid = cert["Id"]
            if cid not in unique_ids:
                unique_ids[cid] = cert

        print(f"\n4. Fetching details for {len(unique_ids)} unique FM certificates...")
        details_cache = {}
        for i, (cid, cert) in enumerate(unique_ids.items()):
            header, address = await client.get_details(cid)
            details_cache[cid] = (header, address)
            org = cert.get("Organization", "?")
            status = header.get("Status", "?") if header else "?"
            area = header.get("CertifiedForestArea", "N/A") if header else "N/A"
            print(f"  [{i+1}/{len(unique_ids)}] {org}: {status} ({area})")
            await asyncio.sleep(0.15)

        # Build detailed rows for ALL FM entries (including sites)
        detailed_rows = []
        for cert in fm_certs:
            cid = cert["Id"]
            header, address = details_cache.get(cid, (None, None))
            row = {**cert, **(header or {})}

            if address:
                addr = address[0] if isinstance(address, list) and address else address
                if isinstance(addr, dict):
                    row["Address_City"] = addr.get("City", "")
                    row["Address_State"] = addr.get("State", "")
                    row["Address_PostalCode"] = addr.get("PostalCode", "")
                    row["Address_Street"] = addr.get("Street", "")

            detailed_rows.append(row)

        detail_fields = [
            "Id", "Organization", "License", "CertificateCode", "CertificateType",
            "CertifiedStatus", "Role", "Country",
            "CertifiedForestArea", "FirstIssueDate", "LastIssueDate",
            "ExpiryDate", "SuspensionDate", "TerminatedDate",
            "Address_City", "Address_State", "Address_Street", "Address_PostalCode",
            "CertificateUrl", "SalesActivityStatus",
        ]
        detail_path = os.path.join(OUTPUT_DIR, "fsc_indonesia_details_fm.csv")
        save_csv(detailed_rows, detail_path, detail_fields)

    # Summary
    print("\n" + "=" * 55)
    print("Summary:")
    print(f"  Total Indonesia certificates: {len(all_list)}")
    print(f"  FM-related certificates: {len(fm_certs)}")

    type_counts = {}
    for c in all_list:
        t = c.get("CertificateType", "Unknown")
        type_counts[t] = type_counts.get(t, 0) + 1
    print("  Type breakdown:")
    for t, count in sorted(type_counts.items(), key=lambda x: -x[1]):
        fm_flag = " <-- FM-related" if "FM" in t else ""
        print(f"    {t}: {count}{fm_flag}")

    if fm_certs:
        statuses = {}
        for c in fm_certs:
            s = c.get("CertifiedStatus", "Unknown")
            statuses[s] = statuses.get(s, 0) + 1
        print("  FM status breakdown:")
        for status, count in sorted(statuses.items()):
            print(f"    {status}: {count}")

        # Count unique cert holders vs sites
        holders = sum(1 for c in fm_certs if c.get("Role") == "Certificate holder")
        sites = sum(1 for c in fm_certs if "Site" in (c.get("Role") or ""))
        print(f"  FM roles: {holders} certificate holders, {sites} sites")

    print(f"\nOutput files in: {OUTPUT_DIR}/")

    await browser.close()
    await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
