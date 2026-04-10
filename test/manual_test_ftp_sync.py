"""
Manual test script for FTPSyncUtility.

This script directly calls the sync_reports_directory function to test synchronization
with a REAL FTP server.

*** INSTRUCTIONS ***
1. Fill in your FTP server details in the CONFIG section below.
2. Run this script from the project's root directory:
   python test/manual_test_ftp_sync.py
3. Check your FTP server to see if the 'temp_manual_test_reports' directory
   and its contents were uploaded correctly under the specified remote directory.
"""
import os
import sys
import shutil
from pathlib import Path

# Add the parent directory to the sys.path to allow imports from the main project
sys.path.insert(0,
                os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from market_sentiment_report import FTPSyncUtility

# --- CONFIGURATION ---
# !!! IMPORTANT: Fill in your FTP server details here !!!
# FTP_CONFIG = {
#     "host": '<redacted>',  # e.g., "ftp.example.com"
#     "username": '<redacted>',
#     "password": '<redacted>',
#     "remote_dir":
#     "htdocs/test"  # The directory on the server where files will be uploaded
# }

FTP_CONFIG = {
    "host": '<redacted>',  # e.g., "ftp.example.com"
    "username": '<redacted>',
    "password": '<redacted>',
    "remote_dir":
    '<redacted>'  # The directory on the server where files will be uploaded
}

def create_local_test_data(base_dir: Path):
    """Creates a local directory structure with dummy files for testing."""
    print(f"Creating local test directory: {base_dir}")
    if base_dir.exists():
        shutil.rmtree(base_dir)

    # Create nested directories
    sub_dir = base_dir / "data" / "nested"
    sub_dir.mkdir(parents=True, exist_ok=True)

    # Create dummy files
    (base_dir /
     "index.html").write_text("<html><body><h1>Main Page</h1></body></html>")
    (base_dir /
     "report_20250805.html").write_text("<html>Report for today</html>")
    (sub_dir.parent / "data.json").write_text('{"value": 123}')
    (sub_dir / "details.txt").write_text("some details")

    print("Local test directory and files created successfully.")


def main():
    """Main function to run the manual test."""

    # Check if config is filled
    if "YOUR_FTP_HOST" in FTP_CONFIG.values():
        print(
            "!!! ERROR: Please fill in your FTP details in the FTP_CONFIG section of this script."
        )
        return

    local_dir = Path("temp_manual_test_reports")

    try:
        # 1. Set up local test data
        create_local_test_data(local_dir)

        # 2. Initialize the FTP sync utility
        print("\nInitializing FTPSyncUtility...")
        ftp_sync = FTPSyncUtility(host=FTP_CONFIG["host"],
                                  username=FTP_CONFIG["username"],
                                  password=FTP_CONFIG["password"],
                                  remote_dir=FTP_CONFIG["remote_dir"])

        # 3. Run the synchronization
        print(
            f"Attempting to sync '{local_dir}' to remote directory '{FTP_CONFIG['remote_dir']}'..."
        )
        success = ftp_sync.sync_reports_directory(str(local_dir))

        # 4. Report result
        if success:
            print("\n✅ Synchronization completed successfully!")
            print("Please check your FTP server to verify the files.")
        else:
            print(
                "\n❌ Synchronization failed. Please check the logs for errors."
            )

    except Exception as e:
        print(f"\nAn unexpected error occurred: {e}")

    finally:
        # 5. Clean up local test data
        if local_dir.exists():
            print(f"\nCleaning up local directory: {local_dir}")
            shutil.rmtree(local_dir)


if __name__ == "__main__":
    main()
