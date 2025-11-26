# aiman_api_min.py
# -------------------------------------------------------------------------
# SAFE VERSION - ALL SECRETS LOADED FROM ENVIRONMENT VARIABLES
# -------------------------------------------------------------------------

import os
import logging
import sys
from pathlib import Path
from datetime import datetime, timedelta

import pyotp
import pytz
import yaml
from dotenv import load_dotenv
from kiteconnect import KiteConnect
from retrying import retry
from pymongo import MongoClient

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from webdriver_manager.chrome import ChromeDriverManager

# -------------------------------------------------------------------------
# LOAD SECRETS FROM ENV VARIABLES
# (Set these in GitHub Actions or .env locally)
# -------------------------------------------------------------------------

USR1_ZERODHA_USER_ID = os.getenv("ZERODHA_USER_ID")
USR1_ZERODHA_PASSWORD = os.getenv("ZERODHA_PASSWORD")
USR1_ZERODHA_API_KEY = os.getenv("ZERODHA_API_KEY")
USR1_ZERODHA_API_SECRET = os.getenv("ZERODHA_API_SECRET")
USR1_ZERODHA_AUTHENTICATOR = os.getenv("ZERODHA_TOTP_SECRET")

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB_NAME = ""   # default empty if not set
MONGO_COLLECTION_NAME = "zerodhatokens"
TOKEN_UPDATED_BY = "aiman.singh30@gmail.com"

# Validate required env vars
required_env = {
    "ZERODHA_USER_ID": USR1_ZERODHA_USER_ID,
    "ZERODHA_PASSWORD": USR1_ZERODHA_PASSWORD,
    "ZERODHA_API_KEY": USR1_ZERODHA_API_KEY,
    "ZERODHA_API_SECRET": USR1_ZERODHA_API_SECRET,
    "ZERODHA_TOTP_SECRET": USR1_ZERODHA_AUTHENTICATOR,
    "MONGO_URI": MONGO_URI,
}
missing = [k for k, v in required_env.items() if not v]
if missing:
    raise ValueError(f"Missing required environment variables: {missing}")

# -------------------------------------------------------------------------
# GENERAL CONFIG
# -------------------------------------------------------------------------

IST = pytz.timezone("Asia/Kolkata")

logging.getLogger("selenium").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_EMBEDDED_APP_CONFIG_YAML = """
user_credentials_map:
  XW7136: "USR1_"
exchanges:
  - NSE
  - BSE
  - MCX
  - NFO
  - BFO
chrome_driver_path: ""
chrome_user_data_dir: ""
"""

# -------------------------------------------------------------------------
# HELPERS
# -------------------------------------------------------------------------

def _find_project_root():
    try:
        return Path(__file__).parent
    except NameError:
        return Path.cwd()


def load_app_config() -> dict:
    try:
        project_root = _find_project_root()
        candidates = [
            project_root / "config" / "app_config.yaml",
            project_root / "app_config.yaml",
            Path("/mnt/data/app_config.yaml"),
        ]
        for config_path in candidates:
            if config_path.is_file():
                with open(config_path, "r", encoding="utf-8") as f:
                    app_config = yaml.safe_load(f) or {}
                logging.info(f"Loaded app_config from {config_path}")
                return app_config

        app_config = yaml.safe_load(_EMBEDDED_APP_CONFIG_YAML) or {}
        logging.info("Using embedded app_config YAML fallback.")
        return app_config

    except Exception as e:
        logging.error(f"Error loading app_config.yaml: {e}")
        raise

APP_CONFIG = load_app_config()

# -------------------------------------------------------------------------
# LOAD CREDENTIALS CLEANLY (NO EMBEDDED SECRETS)
# -------------------------------------------------------------------------

def load_config(user_key="USR1_"):
    """
    Now loads only from environment variables (no fallback secrets).
    """
    ZERODHA_SECRETS = {
        "api_key": USR1_ZERODHA_API_KEY,
        "api_secret": USR1_ZERODHA_API_SECRET,
        "usr": USR1_ZERODHA_USER_ID,
        "pwd": USR1_ZERODHA_PASSWORD,
        "authenticator": USR1_ZERODHA_AUTHENTICATOR,
    }

    for k, v in ZERODHA_SECRETS.items():
        if not v:
            raise ValueError(f"Missing env variable for: {k}")

    return ZERODHA_SECRETS

# -------------------------------------------------------------------------
# MONGO HELPER
# -------------------------------------------------------------------------

def save_token_to_mongo(access_token: str):
    """
    Upserts token into zerodhatokens collection.
    """
    try:
        client = MongoClient(MONGO_URI)
        db = client[MONGO_DB_NAME] if MONGO_DB_NAME else client.get_default_database()
        coll = db[MONGO_COLLECTION_NAME]

        now = datetime.utcnow()
        expires_at = now + timedelta(hours=24)

        result = coll.update_one(
            {"updatedBy": TOKEN_UPDATED_BY},
            {
                "$set": {
                    "accessToken": access_token,
                    "updatedAt": now,
                    "expiresAt": expires_at,
                    "isActive": True,
                    "updatedBy": TOKEN_UPDATED_BY,
                },
                "$setOnInsert": {"createdAt": now},
            },
            upsert=True,
        )

        logging.info(
            f"Mongo update: matched={result.matched_count}, modified={result.modified_count}"
        )

    finally:
        client.close()

# -------------------------------------------------------------------------
# ZERODHA CLIENT (LOGIN)
# -------------------------------------------------------------------------

class ZerodhaClient:
    _user_map = APP_CONFIG.get("user_credentials_map", {})

    def __init__(self, user_id="XW7136"):
        self.user_id = user_id
        self.config = load_config()
        self.kite = None
        self.access_token = None

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def login(self):
        logging.info("Starting Zerodha login…")
        self.kite = KiteConnect(api_key=self.config["api_key"])
        login_url = self.kite.login_url()

        chrome_options = Options()
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option("useAutomationExtension", False)

        # Headless mode for GitHub Actions
        if os.getenv("GITHUB_ACTIONS") == "true":
            chrome_options.binary_location = "/usr/bin/chromium-browser"
            chrome_options.add_argument("--headless=new")
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-dev-shm-usage")

        driver_path = ChromeDriverManager().install()
        driver = webdriver.Chrome(service=Service(driver_path), options=chrome_options)

        try:
            driver.get(login_url)
            wait = WebDriverWait(driver, 20)

            # User ID
            user_box = wait.until(
                EC.element_to_be_clickable((By.XPATH, '//input[@id="userid"]'))
            )
            user_box.send_keys(self.config["usr"])

            # Password
            pwd_box = wait.until(
                EC.element_to_be_clickable((By.XPATH, '//input[@id="password"]'))
            )
            pwd_box.send_keys(self.config["pwd"])

            driver.find_element(By.XPATH, '//button[@type="submit"]').click()

            import time
            time.sleep(2)

            # TOTP
            totp_box = wait.until(
                EC.element_to_be_clickable((By.XPATH, '//input[@id="totp"]'))
            )
            otp = pyotp.TOTP(self.config["authenticator"]).now()
            totp_box.send_keys(otp)

            # Submit OTP
            time.sleep(1)
            driver.find_element(By.XPATH, '//button[@type="submit"]').click()

            wait.until(EC.url_contains("request_token"))
            rt = driver.current_url.split("request_token=")[1].split("&")[0]

            data = self.kite.generate_session(rt, api_secret=self.config["api_secret"])
            self.access_token = data["access_token"]

            return self.kite, self.access_token

        finally:
            driver.quit()

# -------------------------------------------------------------------------
# MAIN
# -------------------------------------------------------------------------

def main():
    client = ZerodhaClient()
    _, access_token = client.login()

    save_token_to_mongo(access_token)

    print(access_token)

if __name__ == "__main__":
    main()

