#!/usr/bin/env python3
"""
Debug version - checks what's actually in /bi/incoming
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

INCOMING_DIR  = os.environ.get("INCOMING_DIR",  "/bi/incoming")
PROCESSED_LOG = os.environ.get("PROCESSED_LOG", "/bi/processed/processed.log")

XML_PATTERN = re.compile(r".*\.xml$", re.IGNORECASE)

# =============================================================================
# FTP HELPERS
# =============================================================================

def ftp_connect():
    print(f"[FTP] Connecting to {FTP_HOST}:{FTP_PORT}...")
    transport = paramiko.Transport((FTP_HOST, FTP_PORT))
    transport.connect(username=FTP_USERNAME, password=FTP_PASSWORD)
    sftp = paramiko.SFTPClient.from_transport(transport)
    print("[FTP] Connected")
    return transport, sftp

def ftp_read_text(sftp, path):
    try:
        with sftp.open(path, "r") as f:
            return f.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[DEBUG] Error reading {path}: {e}")
        return ""

def load_processed(sftp):
    content = ftp_read_text(sftp, PROCESSED_LOG)
    return set(line.strip() for line in content.splitlines() if line.strip())

def list_incoming_xml_debug(sftp):
    """Debug version that shows everything"""
    print(f"\n[DEBUG] Listing contents of: {INCOMING_DIR}")
    try:
        items = sftp.listdir_attr(INCOMING_DIR)
        print(f"[DEBUG] Found {len(items)} total items")
        
        for it in items:
            is_dir = it.longname.startswith("d")
            matches_pattern = XML_PATTERN.match(it.filename)
            
            print(f"\n[DEBUG] Item: {it.filename}")
            print(f"  - Longname: {it.longname}")
            print(f"  - Is directory: {is_dir}")
            print(f"  - Matches XML pattern: {matches_pattern}")
            print(f"  - Size: {it.st_size} bytes")
            print(f"  - Modified: {datetime.fromtimestamp(it.st_mtime)}")
        
        # Now filter for XML files
        files = []
        for it in items:
            if not it.longname.startswith("d") and XML_PATTERN.match(it.filename):
                files.append((it.filename, it.st_mtime, it.st_size))
        
        files.sort(key=lambda x: x[1])
        print(f"\n[DEBUG] Filtered XML files: {len(files)}")
        for f in files:
            print(f"  - {f[0]} ({f[2]} bytes)")
        
        return files
        
    except Exception as e:
        print(f"[ERROR] Failed to list directory: {e}")
        import traceback
        traceback.print_exc()
        return []

# =============================================================================
# MAIN
# =============================================================================

def main():
    print("=" * 60)
    print(f"[DEBUG START] {datetime.now()}")
    print("=" * 60)

    # Validate config
    if not FTP_PASSWORD:
        print("ERROR: Missing FTP_PASSWORD")
        sys.exit(1)

    # Connect FTP
    transport, sftp = ftp_connect()

    try:
        # Load processed list
        processed = load_processed(sftp)
        print(f"\n[STATE] Already processed: {len(processed)} files")
        if processed:
            print("[STATE] Processed files:")
            for pf in sorted(processed):
                print(f"  - {pf}")

        # List incoming XML with debug info
        incoming = list_incoming_xml_debug(sftp)
        print(f"\n[RESULT] Found {len(incoming)} XML file(s)")

        to_process = [f for f in incoming if f[0] not in processed]
        print(f"[RESULT] New files to process: {len(to_process)}")
        for f in to_process:
            print(f"  - {f[0]}")

    finally:
        sftp.close()
        transport.close()


if __name__ == "__main__":
    main()
