#!/usr/bin/env python3
"""
BI XML -> CSV (SFMC SFTP)
========================
- Reads XML files from INCOMING_DIR (SFTP)
- Skips already processed files using PROCESSED_LOG
- Converts PROGRAMME blocks to CSV (string ops, same logic style as CloudPage)
- Uploads CSV with unique timestamp name to OUT_DIR
- Archives XML to ARCHIVE_DIR with unique timestamp name
"""

import csv
import os
import re
import sys
from datetime import datetime, timezone
from io import BytesIO, StringIO

import paramiko

# =============================================================================
# CONFIG (override via GitHub Actions env)
# =============================================================================
FTP_HOST     = os.environ.get("FTP_HOST", "")
FTP_PORT     = int(os.environ.get("FTP_PORT", "22"))
FTP_USERNAME = os.environ.get("FTP_USERNAME", "")
FTP_PASSWORD = os.environ.get("FTP_PASSWORD", "")

INCOMING_DIR  = os.environ.get("INCOMING_DIR",  "/bi/incoming")
OUT_DIR       = os.environ.get("OUT_DIR",       "/bi/out")
ARCHIVE_DIR   = os.environ.get("ARCHIVE_DIR",   "/bi/archive")
PROCESSED_LOG = os.environ.get("PROCESSED_LOG", "/bi/processed/processed.log")

XML_PATTERN = re.compile(r".*\.xml$", re.IGNORECASE)

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
# UTIL
# =============================================================================
def utc_stamp():
    # timezone-aware UTC timestamp (no deprecation warning)
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

def safe_join(dir_path, filename):
    return dir_path.rstrip("/") + "/" + filename

def ensure_remote_dirs(sftp, full_path):
    """Ensure all parent directories exist for a file path."""
    parts = full_path.strip("/").split("/")
    for i in range(len(parts) - 1):
        dir_path = "/" + "/".join(parts[:i+1])
        try:
            sftp.stat(dir_path)
        except Exception:
            try:
                sftp.mkdir(dir_path)
            except Exception:
                pass

# =============================================================================
# FTP
# =============================================================================
def ftp_connect():
    print(f"[FTP] Connecting to {FTP_HOST}:{FTP_PORT} ...")
    transport = paramiko.Transport((FTP_HOST, FTP_PORT))
    transport.connect(username=FTP_USERNAME, password=FTP_PASSWORD)
    sftp = paramiko.SFTPClient.from_transport(transport)
    print("[FTP] Connected")
    return transport, sftp

def ftp_list_dir(sftp, path, limit=200):
    try:
        items = sftp.listdir_attr(path)
        print(f"[FTP] ls {path} -> {len(items)} item(s)")
        for it in items[:limit]:
            kind = "DIR" if it.longname.startswith("d") else "FILE"
            print(f"      [{kind}] {it.filename} ({it.st_size} bytes)")
        if len(items) > limit:
            print("      ... (truncated)")
        return items
    except Exception as e:
        print(f"[FTP] ls {path} FAILED: {e}")
        return []

def ftp_download(sftp, path):
    print(f"[FTP] Downloading {path} ...")
    buf = BytesIO()
    sftp.getfo(path, buf)
    content = buf.getvalue().decode("utf-8", errors="replace")
    print(f"[FTP] Downloaded {len(content):,} chars")
    return content

def ftp_upload_text(sftp, path, content):
    ensure_remote_dirs(sftp, path)
    print(f"[FTP] Uploading -> {path} ({len(content):,} bytes)")
    buf = BytesIO(content.encode("utf-8"))
    sftp.putfo(buf, path)
    print("[FTP] Upload complete")

def ftp_rename(sftp, src, dst):
    ensure_remote_dirs(sftp, dst)
    sftp.rename(src, dst)

# =============================================================================
# PROCESSED MANIFEST
# =============================================================================
def ftp_read_text(sftp, path):
    try:
        with sftp.open(path, "r") as f:
            return f.read().decode("utf-8", errors="replace")
    except Exception:
        return ""

def ftp_write_text(sftp, path, content):
    ensure_remote_dirs(sftp, path)
    with sftp.open(path, "w") as f:
        f.write(content.encode("utf-8"))

def load_processed_set(sftp):
    content = ftp_read_text(sftp, PROCESSED_LOG)
    done = set()
    for line in content.splitlines():
        line = line.strip()
        if line:
            done.add(line)
    return done

def mark_processed(sftp, filename):
    existing = ftp_read_text(sftp, PROCESSED_LOG)
    if existing and not existing.endswith("\n"):
        existing += "\n"
    existing += filename + "\n"
    ftp_write_text(sftp, PROCESSED_LOG, existing)

# =============================================================================
# XML HELPERS — string ops (CloudPage-like)
# =============================================================================
def decode_xml(v):
    if not v:
        return ""
    v = re.sub(r"<!\[CDATA\[([\s\S]*?)\]\]>", r"\1", v)
    v = v.replace("&lt;", "<").replace("&gt;", ">")
    v = v.replace("&amp;", "&").replace("&quot;", '"')
    v = v.replace("&#39;", "'")
    v = re.sub(r"\s+", " ", v).strip()
    return v

def tag_value(xml, tag):
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
    marker = "/programme-neuf-"
    hit = program_xml.find(marker)
    if hit < 0:
        return ""
    s = program_xml.rfind("<URL>", 0, hit)
    if s < 0:
        return ""
    s += 5
    e = program_xml.find("</URL>", s)
    if e < 0:
        return ""
    return decode_xml(program_xml[s:e])

def get_points_forts(program_xml):
    s = program_xml.find("<POINTS_FORTS>")
    if s < 0:
        return []
    e = program_xml.find("</POINTS_FORTS>", s)
    if e < 0:
        return []
    block = program_xml[s:e + 15]
    return all_tag_values(block, "PF")

def clean_text(v):
    v = decode_xml(v or "")
    v = re.sub(r"<[^>]*>", " ", v)
    v = re.sub(r"\s+", " ", v).strip()
    return v

def build_program_arguments(program_xml, program_name):
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
    v = v or ""
    return v[:n] if len(v) > n else v

# =============================================================================
# PARSER
# =============================================================================
def parse_xml(raw):
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

        program_ref = tag_value(p, "REF_OPERATION") or tag_value(p, "NUMERO")
        program_name = tag_value(p, "NOM")
        city = tag_value(p, "VILLE")
        zip_code = tag_value(p, "CP")
        dept = tag_value(p, "DEPARTEMENT")
        program_url = get_program_url(p)

        program_arguments = build_program_arguments(p, program_name)
        program_image = get_program_image(p)
        scraping_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if not all([program_ref, program_name, city, zip_code, dept, program_url]):
            skipped += 1
            continue

        if "/programme-neuf-" not in program_url:
            skipped += 1
            continue

        unique_key = f"{program_ref}||{program_url}"
        if unique_key in dedup:
            dup_skipped += 1
            continue
        dedup[unique_key] = True

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

    print(f"[PARSE] Scanned={scanned} Valid={len(programs)} Skipped={skipped} Duplicates={dup_skipped}")
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
    print(f"[CSV] rows={len(programs)} bytes={len(csv_str):,}")
    return csv_str

# =============================================================================
# INCOMING LIST
# =============================================================================
def ftp_list_incoming_xml(sftp):
    items = sftp.listdir_attr(INCOMING_DIR)
    files = []
    for it in items:
        if it.longname.startswith("d"):
            continue
        if XML_PATTERN.match(it.filename):
            files.append((it.filename, it.st_mtime, it.st_size))
    files.sort(key=lambda x: x[1])  # oldest -> newest
    return files

# =============================================================================
# MAIN
# =============================================================================
def main():
    print("=" * 60)
    print("[BOOT] BI XML to CSV Converter for SFMC")
    print("[BOOT] FTP_HOST      =", FTP_HOST)
    print("[BOOT] FTP_USERNAME  =", FTP_USERNAME)
    print("[BOOT] INCOMING_DIR  =", INCOMING_DIR)
    print("[BOOT] OUT_DIR       =", OUT_DIR)
    print("[BOOT] ARCHIVE_DIR   =", ARCHIVE_DIR)
    print("[BOOT] PROCESSED_LOG =", PROCESSED_LOG)
    print("=" * 60)

    if not FTP_PASSWORD:
        print("ERROR: FTP_PASSWORD not set.")
        sys.exit(1)

    transport, sftp = ftp_connect()

    try:
        # Debug visibility (confirms folders & files exist)
        ftp_list_dir(sftp, "/")
        ftp_list_dir(sftp, "/bi")
        ftp_list_dir(sftp, INCOMING_DIR)
        ftp_list_dir(sftp, OUT_DIR)
        ftp_list_dir(sftp, ARCHIVE_DIR)

        processed = load_processed_set(sftp)
        print(f"[STATE] Already processed: {len(processed)}")

        incoming = ftp_list_incoming_xml(sftp)
        print(f"[INCOMING] Found {len(incoming)} xml file(s)")

        to_process = [f for f in incoming if f[0] not in processed]
        print(f"[TODO] New xml to process: {len(to_process)}")

        if not to_process:
            print("[DONE] Nothing new.")
            return

        for filename, mtime, size in to_process:
            stamp = utc_stamp()
            xml_path = safe_join(INCOMING_DIR, filename)

            print("-" * 60)
            print(f"[PROCESS] {xml_path} ({size} bytes)")

            xml_content = ftp_download(sftp, xml_path)
            programs = parse_xml(xml_content)

            if not programs:
                print("[WARN] No valid programs. Marking as processed anyway.")
                mark_processed(sftp, filename)
                continue

            csv_content = programs_to_csv(programs)

            csv_name = f"PartenaireBI_{stamp}.csv"
            csv_path = safe_join(OUT_DIR, csv_name)

            ftp_upload_text(sftp, csv_path, csv_content)
            print(f"[OUT] CSV uploaded -> {csv_path}")

            # ✅ proof in logs that it exists in /bi/out
            ftp_list_dir(sftp, OUT_DIR)

            mark_processed(sftp, filename)
            print(f"[STATE] processed -> {filename}")

            archived_name = f"{filename.rsplit('.', 1)[0]}_{stamp}.xml"
            archive_path = safe_join(ARCHIVE_DIR, archived_name)
            try:
                ftp_rename(sftp, xml_path, archive_path)
                print(f"[ARCHIVE] XML moved -> {archive_path}")
            except Exception as e:
                print(f"[ARCHIVE] Could not move XML (ok): {e}")

        print("=" * 60)
        print(f"[DONE] Processed {len(to_process)} new file(s).")

    finally:
        try:
            sftp.close()
        finally:
            transport.close()

if __name__ == "__main__":
    main()
