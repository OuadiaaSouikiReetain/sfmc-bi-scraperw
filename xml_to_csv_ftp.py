#!/usr/bin/env python3

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

INCOMING_DIR   = os.environ.get("INCOMING_DIR", "/bi/incoming")
OUT_DIR        = os.environ.get("OUT_DIR",      "/bi/out")
ARCHIVE_DIR    = os.environ.get("ARCHIVE_DIR",  "/bi/archive")
PROCESSED_LOG  = os.environ.get("PROCESSED_LOG","/bi/processed/processed.log")

XML_PATTERN = re.compile(r".*\.xml$", re.IGNORECASE)


def utc_stamp():
    # timestamp stable, triable
    return datetime.utcnow().strftime("%Y%m%d_%H%M%S")

def safe_join(dir_path, filename):
    return dir_path.rstrip("/") + "/" + filename

# =============================================================================
# PROCESSED MANIFEST
# =============================================================================
def ftp_read_text(sftp, path):
    try:
        with sftp.open(path, "r") as f:
            return f.read().decode("utf-8", errors="replace")
    except Exception:
        return ""

def ftp_append_text(sftp, path, line):
    # ensure directory exists
    parts = path.strip("/").split("/")
    for i in range(len(parts) - 1):
        dir_path = "/" + "/".join(parts[:i+1])
        try:
            sftp.stat(dir_path)
        except Exception:
            try:
                sftp.mkdir(dir_path)
            except Exception:
                pass

    existing = ftp_read_text(sftp, path)
    new_content = existing + ("" if existing.endswith("\n") or existing == "" else "\n") + line + "\n"
    with sftp.open(path, "w") as f:
        f.write(new_content.encode("utf-8"))

def load_processed_set(sftp):
    content = ftp_read_text(sftp, PROCESSED_LOG)
    done = set()
    for line in content.splitlines():
        line = line.strip()
        if line:
            done.add(line)
    return done

# =============================================================================
# LIST INCOMING XML
# =============================================================================
def ftp_list_incoming_xml(sftp):
    items = sftp.listdir_attr(INCOMING_DIR)
    files = []
    for it in items:
        if it.longname.startswith("d"):
            continue
        if XML_PATTERN.match(it.filename):
            files.append((it.filename, it.st_mtime, it.st_size))
    # trier par mtime croissant (du plus ancien au plus récent)
    files.sort(key=lambda x: x[1])
    return files

def ftp_rename(sftp, src, dst):
    # ensure dst dir exists
    parts = dst.strip("/").split("/")
    for i in range(len(parts) - 1):
        dir_path = "/" + "/".join(parts[:i+1])
        try:
            sftp.stat(dir_path)
        except Exception:
            try:
                sftp.mkdir(dir_path)
            except Exception:
                pass
    sftp.rename(src, dst)

# =============================================================================
# MAIN MODIFIE
# =============================================================================
def main():
    print("=" * 60)
    print("BI XML to CSV Converter for SFMC")
    print(f"Started: {datetime.now()} (local)")
    print("=" * 60)

    if not FTP_PASSWORD:
        print("ERROR: FTP_PASSWORD not set.")
        sys.exit(1)

    transport, sftp = ftp_connect()

    try:
        # 1) charger la liste des déjà traités
        processed = load_processed_set(sftp)
        print(f"[STATE] Already processed: {len(processed)} files")

        # 2) lister incoming
        incoming = ftp_list_incoming_xml(sftp)
        print(f"[INCOMING] Found {len(incoming)} xml files in {INCOMING_DIR}")

        # 3) filtrer ceux à traiter
        to_process = [f for f in incoming if f[0] not in processed]
        print(f"[TODO] New files to process: {len(to_process)}")

        if not to_process:
            print("[DONE] Nothing new.")
            return

        for filename, mtime, size in to_process:
            xml_path = safe_join(INCOMING_DIR, filename)
            print("-" * 60)
            print(f"[PROCESS] {xml_path} ({size} bytes)")

            # 4) download xml
            xml_content = ftp_download(sftp, xml_path)

            # 5) parse
            programs = parse_xml(xml_content)
            if not programs:
                print("[WARN] No valid programs. Marking as processed anyway.")
                ftp_append_text(sftp, PROCESSED_LOG, filename)
                continue

            # 6) csv content
            csv_content = programs_to_csv(programs)

            # 7) csv unique name
            stamp = utc_stamp()
            csv_name = f"PartenaireBI_{stamp}.csv"
            csv_path = safe_join(OUT_DIR, csv_name)

            # 8) upload csv
            ftp_upload(sftp, csv_path, csv_content)
            print(f"[OUT] Uploaded CSV -> {csv_path}")

            # 9) mark processed
            ftp_append_text(sftp, PROCESSED_LOG, filename)
            print(f"[STATE] Marked processed -> {filename}")

            # 10) (optionnel) archive xml avec nom unique (évite collisions)
            archived_name = f"{filename.rsplit('.',1)[0]}_{stamp}.xml"
            archive_path = safe_join(ARCHIVE_DIR, archived_name)
            try:
                ftp_rename(sftp, xml_path, archive_path)
                print(f"[ARCHIVE] Moved XML -> {archive_path}")
            except Exception as e:
                print(f"[ARCHIVE] Could not move XML (ok) : {e}")

        print("=" * 60)
        print(f"[DONE] Processed {len(to_process)} new xml file(s).")

    finally:
        try:
            sftp.close()
        finally:
            transport.close()
