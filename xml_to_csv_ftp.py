#!/usr/bin/env python3
"""
BI XML → SFMC Data Extension (Full Pipeline) - ASYNC API VERSION
================================================================
Uses the async Data Extension API which doesn't require primary key specification
"""

import os
import re
import sys
from datetime import datetime
from io import BytesIO

import paramiko
import requests

# =============================================================================
# CONFIG
# =============================================================================

FTP_HOST     = os.environ.get("FTP_HOST", "")
FTP_PORT     = int(os.environ.get("FTP_PORT", "22"))
FTP_USERNAME = os.environ.get("FTP_USERNAME", "")
FTP_PASSWORD = os.environ.get("FTP_PASSWORD", "")

SFMC_CLIENT_ID     = os.environ.get("SFMC_CLIENT_ID", "")
SFMC_CLIENT_SECRET = os.environ.get("SFMC_CLIENT_SECRET", "")
SFMC_AUTH_BASE_URI = os.environ.get("SFMC_AUTH_BASE_URI", "")
SFMC_REST_BASE_URI = os.environ.get("SFMC_REST_BASE_URI", "")

INCOMING_DIR  = os.environ.get("INCOMING_DIR",  "/bi/incoming")
ARCHIVE_DIR   = os.environ.get("ARCHIVE_DIR",   "/bi/archive")
PROCESSED_LOG = os.environ.get("PROCESSED_LOG", "/bi/processed/processed.log")

# NOTE: Correct External Key from SFMC
DE_EXTERNAL_KEY = "358E9826-DCC9-4611-98F1-233E639B96D3"
BATCH_SIZE = 50

XML_PATTERN = re.compile(r".*\.xml$", re.IGNORECASE)

# =============================================================================
# FTP HELPERS
# =============================================================================

def safe_join(d, f):
    return d.rstrip("/") + "/" + f

def ensure_remote_dirs(sftp, path):
    parts = path.strip("/").split("/")
    for i in range(len(parts) - 1):
        dp = "/" + "/".join(parts[:i+1])
        try: sftp.stat(dp)
        except:
            try: sftp.mkdir(dp)
            except: pass

def ftp_connect():
    print(f"[FTP] Connecting to {FTP_HOST}:{FTP_PORT}...")
    transport = paramiko.Transport((FTP_HOST, FTP_PORT))
    transport.connect(username=FTP_USERNAME, password=FTP_PASSWORD)
    sftp = paramiko.SFTPClient.from_transport(transport)
    print("[FTP] Connected")
    return transport, sftp

def ftp_download(sftp, path):
    print(f"[FTP] Downloading {path}...")
    buf = BytesIO()
    sftp.getfo(path, buf)
    content = buf.getvalue().decode("utf-8", errors="replace")
    print(f"[FTP] {len(content):,} chars")
    return content

def ftp_rename(sftp, src, dst):
    ensure_remote_dirs(sftp, dst)
    sftp.rename(src, dst)

def ftp_read_text(sftp, path):
    try:
        with sftp.open(path, "r") as f:
            return f.read().decode("utf-8", errors="replace")
    except: return ""

def ftp_write_text(sftp, path, content):
    ensure_remote_dirs(sftp, path)
    with sftp.open(path, "w") as f:
        f.write(content.encode("utf-8"))

def load_processed(sftp):
    """Load processed files as a set of 'filename|size|mtime' keys"""
    content = ftp_read_text(sftp, PROCESSED_LOG)
    return set(line.strip() for line in content.splitlines() if line.strip())

def mark_processed(sftp, filename, size, mtime):
    """Mark file as processed using filename|size|mtime format"""
    key = f"{filename}|{size}|{int(mtime)}"
    existing = ftp_read_text(sftp, PROCESSED_LOG)
    if existing and not existing.endswith("\n"):
        existing += "\n"
    ftp_write_text(sftp, PROCESSED_LOG, existing + key + "\n")
    print(f"[LOG] Marked as processed: {key}")

def list_incoming_xml(sftp):
    items = sftp.listdir_attr(INCOMING_DIR)
    files = []
    for it in items:
        if not it.longname.startswith("d") and XML_PATTERN.match(it.filename):
            files.append((it.filename, it.st_mtime, it.st_size))
    files.sort(key=lambda x: x[1])
    return files

# =============================================================================
# SFMC REST API - ASYNC VERSION
# =============================================================================

def sfmc_auth():
    url = f"https://{SFMC_AUTH_BASE_URI}/v2/token"
    print("[API] Authenticating...")
    resp = requests.post(url, json={
        "grant_type": "client_credentials",
        "client_id": SFMC_CLIENT_ID,
        "client_secret": SFMC_CLIENT_SECRET,
    })
    resp.raise_for_status()
    token = resp.json()["access_token"]
    print("[API] Authenticated")
    return token

def sfmc_insert_batch_async(token, rows):
    """Insert rows using the async Data Extension API"""
    url = f"https://{SFMC_REST_BASE_URI}/data/v1/async/dataextensions/key:{DE_EXTERNAL_KEY}/rows"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # Format rows for the async API
    payload = {
        "items": []
    }
    
    for row in rows:
        payload["items"].append({
            "Program_URL": row["Program_URL"],
            "Program_Ref": row["Program_Ref"],
            "Program_Name": row["Program_Name"],
            "Program_City": row["Program_City"],
            "Program_ZipCode": row["Program_ZipCode"],
            "Program_Department": row["Program_Department"],
            "Program_Arguments": row["Program_Arguments"],
            "Scraping_Date": row["Scraping_Date"],
            "Scraping_Status": row["Scraping_Status"],
            "Error_Message": row["Error_Message"],
            "Program_Image": row["Program_Image"],
        })

    resp = requests.post(url, json=payload, headers=headers)

    if resp.status_code in (200, 201, 202):
        print(f"[API] Batch accepted: {resp.status_code}")
        return len(rows), 0
    else:
        print(f"[API] Batch FAILED: HTTP {resp.status_code}")
        print(f"       {resp.text[:500]}")
        return 0, len(rows)

def sfmc_insert_all(token, programs):
    """Insert all rows in batches using async API"""
    total_ok = 0
    total_err = 0

    for i in range(0, len(programs), BATCH_SIZE):
        batch = programs[i:i + BATCH_SIZE]
        ok, err = sfmc_insert_batch_async(token, batch)
        total_ok += ok
        total_err += err
        print(f"[API] Batch {i // BATCH_SIZE + 1}: {ok} OK, {err} errors")

    print(f"[API] Total: {total_ok} OK, {total_err} errors")
    return total_ok, total_err

# =============================================================================
# XML HELPERS
# =============================================================================

def decode_xml(v):
    if not v: return ""
    v = re.sub(r"<!\[CDATA\[([\s\S]*?)\]\]>", r"\1", v)
    v = v.replace("&lt;", "<").replace("&gt;", ">")
    v = v.replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")
    return re.sub(r"\s+", " ", v).strip()

def tag_value(xml, tag):
    o, c = f"<{tag}>", f"</{tag}>"
    s = xml.find(o)
    if s < 0: return ""
    s += len(o)
    e = xml.find(c, s)
    return decode_xml(xml[s:e]) if e >= 0 else ""

def all_tag_values(xml, tag):
    out, o, c, p = [], f"<{tag}>", f"</{tag}>", 0
    while True:
        s = xml.find(o, p)
        if s < 0: break
        s += len(o)
        e = xml.find(c, s)
        if e < 0: break
        out.append(decode_xml(xml[s:e]))
        p = e + len(c)
    return out

def get_program_url(px):
    hit = px.find("/programme-neuf-")
    if hit < 0: return ""
    s = px.rfind("<URL>", 0, hit)
    if s < 0: return ""
    s += 5
    e = px.find("</URL>", s)
    return decode_xml(px[s:e]) if e >= 0 else ""

def get_points_forts(px):
    s = px.find("<POINTS_FORTS>")
    if s < 0: return []
    e = px.find("</POINTS_FORTS>", s)
    if e < 0: return []
    return all_tag_values(px[s:e + 15], "PF")

def clean_text(v):
    v = decode_xml(v or "")
    v = re.sub(r"<[^>]*>", " ", v)
    return re.sub(r"\s+", " ", v).strip()

def build_arguments(px, name):
    pfs = get_points_forts(px)
    if pfs: return clean_text(" | ".join(pfs))
    for tag in ["PROMESSE_PROGRAMME", "DESCRIPTIF_COURT", "DESCRIPTIF_LONG",
                "DESCRIPTIF_CENTRE_D_APPEL"]:
        val = clean_text(tag_value(px, tag))
        if val: return val
    return name if name else "N/A"

def get_program_image(px):
    s = px.find("<PERSPECTIVES>")
    if s < 0: return "NO IMAGE"
    e = px.find("</PERSPECTIVES>", s)
    if e < 0: return "NO IMAGE"
    urls = all_tag_values(px[s:e + 15], "URL")
    return urls[0] if urls and urls[0] else "NO IMAGE"

def cut(v, n):
    v = v or ""
    return v[:n] if len(v) > n else v

# =============================================================================
# PARSER
# =============================================================================

def parse_xml(raw):
    end = raw.find("</REPONSE>")
    if end > -1: raw = raw[:end + 10]

    dedup, programs = {}, []
    scanned, skipped, dups = 0, 0, 0
    pos = 0

    while True:
        ps = raw.find("<PROGRAMME>", pos)
        if ps < 0: break
        pe = raw.find("</PROGRAMME>", ps)
        if pe < 0: break
        pe += len("</PROGRAMME>")
        pos = pe
        scanned += 1

        p = raw[ps:pe]
        ref  = tag_value(p, "REF_OPERATION") or tag_value(p, "NUMERO")
        name = tag_value(p, "NOM")
        city = tag_value(p, "VILLE")
        zip_ = tag_value(p, "CP")
        dept = tag_value(p, "DEPARTEMENT")
        url  = get_program_url(p)

        if not all([ref, name, city, zip_, dept, url]):
            skipped += 1; continue
        if "/programme-neuf-" not in url:
            skipped += 1; continue

        key = f"{ref}||{url}"
        if key in dedup:
            dups += 1; continue
        dedup[key] = True

        if len(programs) < 3:
            print(f"[PARSE] #{len(programs)+1}: ref={ref} name={name}")

        programs.append({
            "Program_URL":        cut(url, 500),
            "Program_Ref":        cut(ref, 50),
            "Program_Name":       cut(name, 255),
            "Program_City":       cut(city, 100),
            "Program_ZipCode":    cut(zip_, 10),
            "Program_Department": cut(dept, 2),
            "Program_Arguments":  cut(build_arguments(p, name), 4000),
            "Scraping_Date":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "Scraping_Status":    "SUCCESS",
            "Error_Message":      "",
            "Program_Image":      cut(get_program_image(p) or "NO IMAGE", 500),
        })

    print(f"[PARSE] Scanned={scanned} Valid={len(programs)} Skipped={skipped} Dups={dups}")
    return programs

# =============================================================================
# MAIN
# =============================================================================

def main():
    print("=" * 60)
    print(f"[START] BI XML → SFMC DE (ASYNC) | {datetime.now()}")
    print("=" * 60)

    # Validate config
    missing = []
    if not FTP_PASSWORD: missing.append("FTP_PASSWORD")
    if not SFMC_CLIENT_ID: missing.append("SFMC_CLIENT_ID")
    if not SFMC_CLIENT_SECRET: missing.append("SFMC_CLIENT_SECRET")
    if not SFMC_AUTH_BASE_URI: missing.append("SFMC_AUTH_BASE_URI")
    if not SFMC_REST_BASE_URI: missing.append("SFMC_REST_BASE_URI")
    if missing:
        print(f"ERROR: Missing secrets: {', '.join(missing)}")
        sys.exit(1)

    print(f"[CONFIG] DE External Key: {DE_EXTERNAL_KEY}")

    # Connect FTP
    transport, sftp = ftp_connect()

    try:
        # Load processed list
        processed = load_processed(sftp)
        print(f"[STATE] Already processed: {len(processed)} files")

        # List incoming XML
        incoming = list_incoming_xml(sftp)
        print(f"[INCOMING] Found {len(incoming)} XML file(s)")

        # Filter out already processed files
        to_process = []
        for filename, mtime, size in incoming:
            key = f"{filename}|{size}|{int(mtime)}"
            if key not in processed:
                to_process.append((filename, mtime, size))
            else:
                print(f"[SKIP] Already processed: {filename}")

        print(f"[TODO] New: {len(to_process)}")

        if not to_process:
            print("[DONE] Nothing new.")
            return

        # Authenticate to SFMC
        token = sfmc_auth()

        for filename, mtime, size in to_process:
            xml_path = safe_join(INCOMING_DIR, filename)
            print("-" * 60)
            print(f"[PROCESS] {xml_path} ({size:,} bytes)")

            # Download XML
            xml_content = ftp_download(sftp, xml_path)

            # Parse
            programs = parse_xml(xml_content)
            if not programs:
                print("[WARN] No valid programs. Marking as processed.")
                mark_processed(sftp, filename, size, mtime)
                continue

            # Insert to DE via async API
            ok, err = sfmc_insert_all(token, programs)
            print(f"[RESULT] {ok} rows submitted, {err} errors")

            # Mark processed
            mark_processed(sftp, filename, size, mtime)

            # Archive XML
            archive_path = safe_join(ARCHIVE_DIR, filename)
            try:
                ftp_rename(sftp, xml_path, archive_path)
                print(f"[ARCHIVE] {xml_path} → {archive_path}")
            except Exception as e:
                print(f"[ARCHIVE] Could not move: {e}")

        print("=" * 60)
        print(f"[DONE] Processed {len(to_process)} file(s)")
        print("=" * 60)

    finally:
        sftp.close()
        transport.close()


if __name__ == "__main__":
    main()
