#!/usr/bin/env python3
"""
BI XML to CSV Converter for SFMC
=================================
Downloads PartenaireBI.xml from SFMC Enhanced FTP, parses PROGRAMME blocks
using the EXACT same logic as the CloudPage, converts to CSV, uploads to
/Import/ on the same FTP.

SFMC Import Activity picks up the CSV automatically.

Runs on: GitHub Actions (daily cron) or locally via terminal.
Requirements: pip install paramiko
"""

import csv
import os
import re
import sys
from datetime import datetime
from io import BytesIO, StringIO

import paramiko

# =============================================================================
# CONFIG
# =============================================================================

FTP_HOST     = os.environ.get("FTP_HOST",     "mct8vv9h4h0gy1x8xmv8np06rlpy.ftp.marketingcloudops.com")
FTP_PORT     = int(os.environ.get("FTP_PORT",  "22"))
FTP_USERNAME = os.environ.get("FTP_USERNAME",  "536005700_7")
FTP_PASSWORD = os.environ.get("FTP_PASSWORD",  "")

FILENAME         = "PartenaireBI.xml"
CSV_UPLOAD_PATH  = "PartenaireBI.csv"



CSV_COLUMNS = [
    "Program_URL",
    "Program_Ref",
    "Program_Name",
    "Program_City",
    "Program_ZipCode",
    "Program_Department",
    "Program_Arguments",
    "Scraping_Date",
    "Scraping_Status",
    "Error_Message",
    "Program_Image",
]

# =============================================================================
# XML HELPERS — Mirrors the CloudPage SSJS logic exactly using string ops
#               (not ElementTree) to behave identically to the SSJS version
# =============================================================================

def decode_xml(v):
    """Exact port of the CloudPage decodeXml() function."""
    if not v:
        return ""
    v = re.sub(r"<!\[CDATA\[([\s\S]*?)\]\]>", r"\1", v)
    v = v.replace("&lt;", "<").replace("&gt;", ">")
    v = v.replace("&amp;", "&").replace("&quot;", '"')
    v = v.replace("&#39;", "'")
    v = re.sub(r"\s+", " ", v).strip()
    return v


def tag_value(xml, tag):
    """Exact port of the CloudPage tagValue() function."""
    o = f"<{tag}>"
    c = f"</{tag}>"
    s = xml.find(o)
    if s < 0:
        return ""
    s += len(o)
    e = xml.find(c, s)
    if e < 0:
        return ""
    return decode_xml(xml[s:e])


def all_tag_values(xml, tag):
    """Exact port of the CloudPage allTagValues() function."""
    out = []
    o = f"<{tag}>"
    c = f"</{tag}>"
    p = 0
    while True:
        s = xml.find(o, p)
        if s < 0:
            break
        s += len(o)
        e = xml.find(c, s)
        if e < 0:
            break
        out.append(decode_xml(xml[s:e]))
        p = e + len(c)
    return out


def get_program_url(program_xml):
    """Exact port of the CloudPage getProgramUrl() function.
    Finds /programme-neuf- marker first, then searches backwards for <URL> tag."""
    marker = "/programme-neuf-"
    hit = program_xml.find(marker)
    if hit < 0:
        return ""
    # Search backwards from the marker to find the enclosing <URL>
    s = program_xml.rfind("<URL>", 0, hit)
    if s < 0:
        return ""
    s += 5  # len("<URL>")
    e = program_xml.find("</URL>", s)
    if e < 0:
        return ""
    return decode_xml(program_xml[s:e])


def get_points_forts(program_xml):
    """Exact port of the CloudPage getPointsForts() function."""
    s = program_xml.find("<POINTS_FORTS>")
    if s < 0:
        return []
    e = program_xml.find("</POINTS_FORTS>", s)
    if e < 0:
        return []
    block = program_xml[s:e + 15]
    return all_tag_values(block, "PF")


def clean_text(v):
    """Exact port of the CloudPage cleanText() function."""
    v = decode_xml(v or "")
    v = re.sub(r"<[^>]*>", " ", v)
    v = re.sub(r"\s+", " ", v).strip()
    return v


def build_program_arguments(program_xml, program_name):
    """Exact port of the CloudPage buildProgramArguments() function."""
    pfs = get_points_forts(program_xml)
    if pfs:
        return clean_text(" | ".join(pfs))

    candidates = [
        tag_value(program_xml, "PROMESSE_PROGRAMME"),
        tag_value(program_xml, "DESCRIPTIF_COURT"),
        tag_value(program_xml, "DESCRIPTIF_LONG"),
        tag_value(program_xml, "DESCRIPTIF_CENTRE_D_APPEL"),
        program_name,
    ]
    for c in candidates:
        cleaned = clean_text(c)
        if cleaned:
            return cleaned
    return "N/A"


def get_program_image(program_xml):
    """Exact port of the CloudPage getProgramImage() function.
    Searches inside <PERSPECTIVES> block for ALL <URL> tags at any depth."""
    s = program_xml.find("<PERSPECTIVES>")
    if s < 0:
        return "NO IMAGE"
    e = program_xml.find("</PERSPECTIVES>", s)
    if e < 0:
        return "NO IMAGE"
    block = program_xml[s:e + 15]
    urls = all_tag_values(block, "URL")
    if urls and urls[0]:
        return urls[0]
    return "NO IMAGE"


def cut(v, n):
    """Exact port of the CloudPage cut() function."""
    v = v or ""
    return v[:n] if len(v) > n else v


# =============================================================================
# MAIN PARSER — Exact port of the CloudPage scrape() function
# =============================================================================

def parse_xml(raw):
    """Parse XML using identical logic to the CloudPage."""

    # Safety cut at </REPONSE>
    end = raw.find("</REPONSE>")
    if end > -1:
        raw = raw[:end + 10]

    dedup = {}
    programs = []
    scanned = 0
    skipped = 0
    dup_skipped = 0

    open_tag = "<PROGRAMME>"
    close_tag = "</PROGRAMME>"
    block_start = 0

    while True:
        ps = raw.find(open_tag, block_start)
        if ps < 0:
            break
        pe = raw.find(close_tag, ps)
        if pe < 0:
            break
        pe += len(close_tag)
        block_start = pe
        scanned += 1

        p = raw[ps:pe]

        program_ref = tag_value(p, "REF_OPERATION")
        if not program_ref:
            program_ref = tag_value(p, "NUMERO")

        program_name = tag_value(p, "NOM")
        city         = tag_value(p, "VILLE")
        zip_code     = tag_value(p, "CP")
        dept         = tag_value(p, "DEPARTEMENT")
        program_url  = get_program_url(p)

        program_arguments = build_program_arguments(p, program_name)
        program_image     = get_program_image(p)
        scraping_date     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Same filters as CloudPage
        if not all([program_ref, program_name, city, zip_code, dept, program_url]):
            skipped += 1
            if scanned <= 5:
                missing = []
                if not program_ref: missing.append("ref")
                if not program_name: missing.append("name")
                if not city: missing.append("city")
                if not zip_code: missing.append("zip")
                if not dept: missing.append("dept")
                if not program_url: missing.append("url")
                print(f"[PARSE] SKIP #{scanned}: missing {', '.join(missing)}")
            continue

        if "/programme-neuf-" not in program_url:
            skipped += 1
            continue

        unique_key = f"{program_ref}||{program_url}"
        if unique_key in dedup:
            dup_skipped += 1
            continue
        dedup[unique_key] = True

        # Log first few for diagnostics
        if len(programs) < 3:
            print(f"[PARSE] Program #{len(programs)+1}: ref={program_ref} name={program_name} "
                  f"city={city} image={program_image[:80]}")

        programs.append({
            "Program_URL":        cut(program_url, 500),
            "Program_Ref":        cut(program_ref, 50),
            "Program_Name":       cut(program_name, 255),
            "Program_City":       cut(city, 100),
            "Program_ZipCode":    cut(zip_code, 10),
            "Program_Department": cut(dept, 2),
            "Program_Arguments":  cut(program_arguments, 4000),
            "Scraping_Date":      scraping_date,
            "Scraping_Status":    "SUCCESS",
            "Error_Message":      "",
            "Program_Image":      cut(program_image or "NO IMAGE", 500),
        })

    print(f"[PARSE] Scanned: {scanned}, Valid: {len(programs)}, "
          f"Skipped: {skipped}, Duplicates: {dup_skipped}")
    return programs


# =============================================================================
# CSV
# =============================================================================

def programs_to_csv(programs):
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS, quoting=csv.QUOTE_ALL)
    writer.writeheader()
    for p in programs:
        writer.writerow(p)
    csv_str = output.getvalue()
    print(f"[CSV] Generated {len(programs)} rows, {len(csv_str):,} bytes")
    return csv_str


# =============================================================================
# FTP
# =============================================================================

def ftp_connect():
    print(f"[FTP] Connecting to {FTP_HOST}:{FTP_PORT}...")
    transport = paramiko.Transport((FTP_HOST, FTP_PORT))
    transport.connect(username=FTP_USERNAME, password=FTP_PASSWORD)
    sftp = paramiko.SFTPClient.from_transport(transport)
    print("[FTP] Connected")
    return transport, sftp


def ftp_list_dir(sftp, path):
    try:
        items = sftp.listdir_attr(path)
        print(f"[FTP] Contents of '{path}':")
        for item in items:
            kind = "DIR" if item.longname.startswith("d") else "FILE"
            print(f"       [{kind}] {item.filename}  ({item.st_size} bytes)")
        return items
    except Exception as e:
        print(f"[FTP] Cannot list '{path}': {e}")
        return []


def ftp_find_xml(sftp):
    """Auto-discover the XML file on FTP with fallbacks."""
    paths_to_try = [
        "/import/bi/PartenaireBI.xml",
        "/Import/bi/PartenaireBI.xml",
        "/import/PartenaireBI.xml",
        "/Import/PartenaireBI.xml",
        "/bi/PartenaireBI.xml",
        "import/bi/PartenaireBI.xml",
        "Import/bi/PartenaireBI.xml",
        "import/PartenaireBI.xml",
        "Import/PartenaireBI.xml",
        "bi/PartenaireBI.xml",
        "/PartenaireBI.xml",
        "PartenaireBI.xml",
    ]

    for path in paths_to_try:
        try:
            stat = sftp.stat(path)
            print(f"[FTP] FOUND: {path} ({stat.st_size:,} bytes)")
            return path
        except (FileNotFoundError, IOError):
            continue

    # Explore FTP to find it
    print("[FTP] Known paths failed. Scanning FTP...")
    dirs_to_scan = ["."]
    for depth in range(3):
        next_dirs = []
        for d in dirs_to_scan:
            items = ftp_list_dir(sftp, d)
            for item in items:
                child = f"{d}/{item.filename}" if d != "." else item.filename
                if item.filename.lower() == FILENAME.lower():
                    try:
                        stat = sftp.stat(child)
                        print(f"[FTP] DISCOVERED: {child} ({stat.st_size:,} bytes)")
                        return child
                    except (FileNotFoundError, IOError):
                        pass
                if item.longname.startswith("d"):
                    next_dirs.append(child)
        dirs_to_scan = next_dirs

    raise FileNotFoundError(f"Could not find {FILENAME} anywhere on FTP.")


def ftp_download(sftp, path):
    print(f"[FTP] Downloading {path}...")
    buffer = BytesIO()
    sftp.getfo(path, buffer)
    content = buffer.getvalue().decode("utf-8", errors="replace")
    print(f"[FTP] Downloaded {len(content):,} characters")
    return content


def ftp_upload(sftp, path, content):
    parts = path.strip("/").split("/")
    for i in range(len(parts) - 1):
        dir_path = "/" + "/".join(parts[:i+1])
        try:
            sftp.stat(dir_path)
        except (FileNotFoundError, IOError):
            print(f"[FTP] Creating directory {dir_path}...")
            try:
                sftp.mkdir(dir_path)
            except Exception:
                pass

    print(f"[FTP] Uploading to {path}...")
    buffer = BytesIO(content.encode("utf-8"))
    sftp.putfo(buffer, path)
    print(f"[FTP] Upload complete ({len(content):,} bytes)")


# =============================================================================
# MAIN
# =============================================================================

def main():
    print("=" * 60)
    print("BI XML to CSV Converter for SFMC")
    print(f"Started: {datetime.now()}")
    print("=" * 60)

    if not FTP_PASSWORD:
        print("ERROR: FTP_PASSWORD not set.")
        print("  Local:   export FTP_PASSWORD='your_password'")
        print("  GitHub:  Add FTP_PASSWORD to repository Secrets")
        sys.exit(1)

    transport, sftp = ftp_connect()

    try:
        # Step 1: Find the XML
        xml_path = ftp_find_xml(sftp)

        # Step 2: Download
        xml_content = ftp_download(sftp, xml_path)

        # Step 3: Parse (same logic as CloudPage)
        programs = parse_xml(xml_content)
        if not programs:
            print("[DONE] No valid programs. No CSV created.")
            sys.exit(0)

        # Step 4: Convert to CSV
        csv_content = programs_to_csv(programs)

        # Step 5: Upload CSV
        ftp_upload(sftp, CSV_UPLOAD_PATH, csv_content)

    finally:
        sftp.close()
        transport.close()

    print("=" * 60)
    print(f"[DONE] {len(programs)} programs -> {CSV_UPLOAD_PATH}")
    print(f"Finished: {datetime.now()}")
    print("=" * 60)


if __name__ == "__main__":
    main()
