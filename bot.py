import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
from crawl4ai import AsyncWebCrawler
import json
from twocaptcha import TwoCaptcha
import os
from datetime import datetime, time
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from cryptography.fernet import Fernet
import pickle
import base64
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import (
    TimeoutException, 
    NoSuchElementException, 
    ElementNotInteractableException,
    StaleElementReferenceException
)
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
import logging
import time as time_module
from pathlib import Path
from dotenv import load_dotenv
from config import TELEGRAM_TOKEN, CAPTCHA_API_KEY, ERP_URL
from keep_alive import keep_alive

# Load environment variables
load_dotenv()

# Get environment variables with fallbacks
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CAPTCHA_API_KEY = os.getenv('CAPTCHA_API_KEY')
ERP_URL = os.getenv('ERP_URL', "https://isquareit.akronsystems.com/pLogin.aspx")

if not TELEGRAM_TOKEN or not CAPTCHA_API_KEY:
    raise ValueError("Missing required environment variables. Please check your .env file.")

# Create necessary directories
def setup_directories():
    """Create necessary directories for storing data"""
    # Create data directory if it doesn't exist
    data_dir = Path("data")
    data_dir.mkdir(exist_ok=True)
    
    # Create logs directory if it doesn't exist
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    
    return data_dir, logs_dir

# Set up logging with proper directories
data_dir, logs_dir = setup_directories()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(logs_dir / 'bot.log'),
        logging.StreamHandler()  # Keep console output too
    ]
)

# Set up user-specific logging
user_logger = logging.getLogger('user_activities')
user_logger.setLevel(logging.INFO)
user_handler = logging.FileHandler(logs_dir / 'user_activities.log')
user_handler.setFormatter(logging.Formatter('%(asctime)s - User %(message)s'))
user_logger.addHandler(user_handler)

logger = logging.getLogger(__name__)

# States for conversation
USERNAME, PASSWORD = range(2)

# Store user data
user_data = {}

class ERPBot:
    def __init__(self, telegram_token, captcha_api_key, erp_url):
        """Initialize the bot with configuration"""
        self.telegram_token = telegram_token
        self.captcha_api_key = captcha_api_key
        self.erp_url = erp_url
        self.application = None
        self.captcha_solution = None
        self.last_captcha_time = None
        self.driver = None
        self.is_browser_ready = False
        
        # Initialize captcha queue and event
        self.captcha_queue = asyncio.Queue()
        self.captcha_ready = asyncio.Event()
        self.captcha_task = None
        
        # Initialize attendance cache
        self.attendance_cache = {}
        self.cache_timeout = 300  # 5 minutes
        
        # Initialize encryption
        self.key = self.load_or_create_key()
        self.cipher_suite = Fernet(self.key)
        
        # Load existing user data if available
        self.load_user_data()
        
        # Setup Chrome options for visible mode
        self.chrome_options = Options()
        self.chrome_options.add_argument('--headless')  # Run in headless mode
        self.chrome_options.add_argument('--disable-gpu')
        self.chrome_options.add_argument('--start-maximized')
        self.chrome_options.add_argument('--disable-dev-shm-usage')
        self.chrome_options.add_argument('--no-sandbox')
        self.chrome_options.add_argument('--window-size=1920,1080')

    def load_or_create_key(self):
        """Load existing key or create a new one"""
        key_path = Path("data/encryption_key.key")
        try:
            with open(key_path, 'rb') as f:
                return f.read()
        except FileNotFoundError:
            # Generate new key if none exists
            key = Fernet.generate_key()
            with open(key_path, 'wb') as f:
                f.write(key)
            return key
            
    def encrypt_data(self, data):
        """Encrypt sensitive data"""
        return self.cipher_suite.encrypt(data.encode()).decode()
        
    def decrypt_data(self, encrypted_data):
        """Decrypt sensitive data"""
        return self.cipher_suite.decrypt(encrypted_data.encode()).decode()
        
    def save_user_data(self):
        """Save encrypted user data to file"""
        encrypted_data = {}
        for user_id, data in user_data.items():
            encrypted_data[user_id] = {
                'username': self.encrypt_data(data['username']),
                'password': self.encrypt_data(data['password']),
            }
        
        with open(Path("data/user_data.pkl"), 'wb') as f:
            pickle.dump(encrypted_data, f)
            
    def load_user_data(self):
        """Load and decrypt user data from file"""
        global user_data
        try:
            with open(Path("data/user_data.pkl"), 'rb') as f:
                encrypted_data = pickle.load(f)
                
            for user_id, data in encrypted_data.items():
                user_data[user_id] = {
                    'username': self.decrypt_data(data['username']),
                    'password': self.decrypt_data(data['password']),
                }
        except FileNotFoundError:
            user_data = {}
        except Exception as e:
            print(f"Error loading user data: {e}")
            # If there's an error loading the data, start fresh
            user_data = {}
            # Remove corrupted data file
            if os.path.exists(Path("data/user_data.pkl")):
                os.remove(Path("data/user_data.pkl"))

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start the conversation and ask for username"""
        await update.message.reply_text(
            "Welcome! Please enter your ERP username:"
        )
        return USERNAME

    async def get_username(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Store username and ask for password"""
        user_id = update.effective_user.id
        username = update.message.text
        
        user_data[user_id] = {'username': username}
        await update.message.reply_text("Please enter your ERP password:")
        return PASSWORD

    async def get_password(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Store password and complete setup"""
        user_id = update.effective_user.id
        user_data[user_id]['password'] = update.message.text
        
        # Save user data
        self.save_user_data()
        
        await update.message.reply_text(
            "Setup complete! You can now use /attendance to check your attendance."
        )
        return ConversationHandler.END

    async def attendance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle the /attendance command"""
        user_id = update.effective_user.id
        
        if user_id not in user_data:
            await update.message.reply_text(
                "Please set up your credentials first using /start"
            )
            return
            
        await update.message.reply_text("Fetching your attendance... Please wait.")
        user_logger.info(f"{user_id} - Requested attendance")
        
        try:
            # Check cache first
            cached_data = self.get_cached_attendance(user_id)
            if cached_data:
                user_logger.info(f"{user_id} - Using cached attendance data")
                # Send cached data incrementally
                for attendance_type, subjects in cached_data.items():
                    await self.send_incremental_attendance(update, attendance_type, subjects)
                return
            
            print(f"Attempting login with username: {user_data[user_id]['username']}")
            all_attendance_data = await self.check_attendance(user_id)
            
            if all_attendance_data and len(all_attendance_data) > 0:
                # Cache the new data
                self.cache_attendance(user_id, all_attendance_data)
                
                # Send data incrementally by type
                for attendance_type, subjects in all_attendance_data.items():
                    await self.send_incremental_attendance(update, attendance_type, subjects)
            else:
                await update.message.reply_text(
                    "Sorry, I couldn't fetch your attendance data. Please try again later."
                )
                user_logger.error(f"{user_id} - No attendance data retrieved")
        except Exception as e:
            logger.error(f"Error in attendance command: {str(e)}")
            user_logger.error(f"{user_id} - Error fetching attendance: {str(e)}")
            await update.message.reply_text(
                "Sorry, there was an error fetching your attendance. Please try again later."
            )

    async def captcha_worker(self):
        """Background worker for solving captchas"""
        logger.info("Starting captcha worker")
        while True:
            try:
                # Get the next captcha solving request
                await self.captcha_queue.get()
                
                # Solve the captcha
                logger.info("Solving captcha in background...")
                solver = TwoCaptcha(self.captcha_api_key)
                
                result = solver.recaptcha(
                    sitekey="6Le73cMbAAAAANUPFMh89e5vPsfwqyiwAh8x4ylp",
                    url=self.erp_url,
                    version='v2'
                )
                
                # Update the solution
                self.captcha_solution = result['code']
                self.last_captcha_time = datetime.now()
                logger.info("Captcha solved successfully in background")
                
                # Signal that a new captcha is ready
                self.captcha_ready.set()
                
                # Clear the queue of any pending requests while we have a fresh solution
                while not self.captcha_queue.empty():
                    try:
                        self.captcha_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                        
            except Exception as e:
                logger.error(f"Error in captcha worker: {str(e)}")
                # Reset the event in case of error
                self.captcha_ready.clear()
            finally:
                # Mark the task as done
                self.captcha_queue.task_done()

    async def solve_captcha(self):
        """Request a captcha solution asynchronously"""
        try:
            # Clear previous ready state
            self.captcha_ready.clear()
            
            # Queue a captcha solving request
            await self.captcha_queue.put(True)
            
            # Wait for the solution with a timeout
            try:
                await asyncio.wait_for(self.captcha_ready.wait(), timeout=30)
                return True
            except asyncio.TimeoutError:
                logger.error("Captcha solving timed out")
                return False
                
        except Exception as e:
            logger.error(f"Error requesting captcha solution: {str(e)}")
            return False

    async def refresh_captcha(self):
        """Refresh captcha solution if it's older than 110 seconds"""
        if (not self.last_captcha_time or 
            (datetime.now() - self.last_captcha_time).total_seconds() > 110):
            # Queue a new captcha solution request
            await self.solve_captcha()

    async def initialize_browser(self):
        """Initialize browser and load login page"""
        try:
            if self.driver:
                try:
                    await self.save_cookies()  # Save cookies before quitting
                    self.driver.quit()
                except:
                    pass
            
            logger.info("Initializing browser...")
            service = Service(ChromeDriverManager().install())
            self.driver = webdriver.Chrome(service=service, options=self.chrome_options)
            
            # Navigate to login page
            logger.info("Pre-loading login page...")
            self.driver.get(self.erp_url)
            
            # Try to load cookies
            cookies_loaded = await self.load_cookies()
            if cookies_loaded:
                # Refresh page to apply cookies
                self.driver.refresh()
            
            # Wait for login form to be ready
            await self._wait_for_element(self.driver, By.ID, "txtUSERNAME")
            await self._wait_for_element(self.driver, By.ID, "txtPASSWORD")
            
            # Pre-solve captcha
            await self.solve_captcha()
            
            self.is_browser_ready = True
            logger.info("Browser initialized and ready")
            return True
        except Exception as e:
            logger.error(f"Error initializing browser: {str(e)}")
            self.is_browser_ready = False
            if self.driver:
                try:
                    self.driver.quit()
                except:
                    pass
            self.driver = None
            return False

    async def refresh_browser_session(self):
        """Refresh browser session if needed"""
        try:
            if not self.driver or not self.is_browser_ready:
                return await self.initialize_browser()
            
            # Check if browser is still responsive
            try:
                self.driver.current_url
                # Refresh page if we're not on login page
                if self.erp_url not in self.driver.current_url:
                    await self.save_cookies()  # Save cookies before navigating
                    self.driver.get(self.erp_url)
                    await self.load_cookies()  # Load cookies after navigation
                    await self._wait_for_element(self.driver, By.ID, "txtUSERNAME")
                return True
            except:
                return await self.initialize_browser()
        except:
            return await self.initialize_browser()

    async def check_attendance(self, user_id):
        """Check attendance using Selenium with improved error handling"""
        try:
            # Ensure browser is ready
            if not await self.refresh_browser_session():
                raise Exception("Browser initialization failed")

            # Ensure we have a fresh captcha solution
            await self.refresh_captcha()
            if not self.captcha_solution:
                raise Exception("No valid captcha solution available")
            
            # Fill credentials and submit form with captcha in one go
            self.driver.execute_script(
                """
                document.getElementById('txtUSERNAME').value = arguments[0];
                document.getElementById('txtPASSWORD').value = arguments[1];
                document.getElementById('g-recaptcha-response').innerHTML = arguments[2];
                document.getElementById('btnSUBMIT').click();
                """,
                user_data[user_id]["username"],
                user_data[user_id]["password"],
                self.captcha_solution
            )
            logger.info("Login submitted with pre-solved captcha")
            
            # Brief wait for page load
            await asyncio.sleep(1)  # Reduced from 2s to 1s for optimization

            # Dictionary to store all attendance data
            all_attendance_data = {}
            
            # Function to extract data from a table
            async def extract_table_data(table_id, attendance_type):
                try:
                    # Wait for table to be present and visible
                    table = WebDriverWait(self.driver, 10).until(
                        EC.presence_of_element_located((By.ID, table_id))
                    )
                    
                    # Wait for table to be visible
                    WebDriverWait(self.driver, 10).until(
                        EC.visibility_of_element_located((By.ID, table_id))
                    )
                    
                    # Scroll to table and reduced wait
                    self.driver.execute_script("arguments[0].scrollIntoView(true);", table)
                    await asyncio.sleep(0.5)  # Back to 0.5s wait after scroll
                    
                    # Wait for table data
                    WebDriverWait(self.driver, 10).until(
                        lambda d: len(d.find_element(By.ID, table_id).find_elements(By.TAG_NAME, "tr")) > 1 and
                                len(d.find_element(By.ID, table_id).find_elements(By.TAG_NAME, "td")) > 0
                    )
                    
                    # Optimized retry mechanism
                    max_retries = 2
                    for attempt in range(max_retries):
                        attendance_data = []
                        rows = table.find_elements(By.TAG_NAME, "tr")
                        
                        # Skip header row
                        for row in rows[1:]:
                            cells = row.find_elements(By.TAG_NAME, "td")
                            if len(cells) >= 6:
                                subject = cells[1].text.strip()
                                # Only add if we have actual subject text
                                if subject:
                                    attendance_data.append({
                                        "subject": subject,
                                        "total_lectures": cells[2].text.strip(),
                                        "present": cells[3].text.strip(),
                                        "absent": cells[4].text.strip(),
                                        "percentage": cells[5].text.strip()
                                    })
                        
                        # If we got data, return it
                        if attendance_data:
                            return attendance_data
                        
                        # If no data, wait and retry
                        await asyncio.sleep(0.5)
                    
                    logger.error(f"Failed to get data for {attendance_type} table after {max_retries} attempts")
                    return []
                    
                except Exception as e:
                    logger.error(f"Error finding {attendance_type} table: {str(e)}")
                    return []

            # Wait for the attendance section
            try:
                attendance_section = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.CLASS_NAME, "attendanceW"))
                )
                # Scroll to attendance section
                self.driver.execute_script("arguments[0].scrollIntoView(true);", attendance_section)
                await asyncio.sleep(0.5)  # Back to 0.5s wait after scroll

                # Get Theory attendance
                logger.info("Extracting Theory attendance")
                theory_data = await extract_table_data("ctl00_ContentPlaceHolder1_ctl03_grdTHERORY", "Theory")
                if theory_data:
                    all_attendance_data["Theory"] = theory_data

                # Click Practical radio button and get Practical attendance
                logger.info("Extracting Practical attendance")
                try:
                    practical_radio = WebDriverWait(self.driver, 5).until(
                        EC.element_to_be_clickable((By.XPATH, "//input[@type='radio' and following-sibling::text()='Practical']"))
                    )
                    self.driver.execute_script("arguments[0].click();", practical_radio)
                    await asyncio.sleep(0.2)  # Wait after click
                    practical_data = await extract_table_data("ctl00_ContentPlaceHolder1_ctl03_grdpract", "Practical")
                    if practical_data:
                        all_attendance_data["Practical"] = practical_data
                except Exception as e:
                    logger.error(f"Error getting practical attendance: {str(e)}")

                # Click Tutorial radio button and get Tutorial attendance
                logger.info("Extracting Tutorial attendance")
                try:
                    tutorial_radio = WebDriverWait(self.driver, 5).until(
                        EC.element_to_be_clickable((By.XPATH, "//input[@type='radio' and following-sibling::text()='Tutorial']"))
                    )
                    self.driver.execute_script("arguments[0].click();", tutorial_radio)
                    await asyncio.sleep(0.2)  # Wait after click
                    tutorial_data = await extract_table_data("ctl00_ContentPlaceHolder1_ctl03_grdtut", "Tutorial")
                    if tutorial_data:
                        all_attendance_data["Tutorial"] = tutorial_data
                except Exception as e:
                    logger.error(f"Error getting tutorial attendance: {str(e)}")

            except Exception as e:
                logger.error("Could not find attendance section")
                raise Exception("Failed to load attendance page")

            # Verify we have some valid data
            if not any(all_attendance_data.values()):
                raise Exception("No attendance data could be retrieved")

            # Return to login page for next request
            self.driver.get(self.erp_url)
            await self._wait_for_element(self.driver, By.ID, "txtUSERNAME")
            
            return all_attendance_data
            
        except Exception as e:
            logger.error(f"Error during attendance check: {str(e)}")
            self.is_browser_ready = False
            raise

    async def _wait_for_element(self, driver, by, value, timeout=5):
        """Helper method to wait for and return an element"""
        return WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((by, value))
        )

    def extract_site_key(self, html):
        """Extract reCAPTCHA site key from login page"""
        import re
        # The specific site key for iSquareIT ERP
        site_key = "6LfvNwUTAAAAANwD8GB3a0kzYBVPnzj7qGD8_D-Z"
        return site_key

    def extract_aspnet_field(self, html, field_name):
        """Extract ASP.NET form field value"""
        import re
        # Look for both id and name attributes since ASP.NET can use either
        patterns = [
            f'id="{field_name}" value="([^"]+)"',
            f'name="{field_name}" value="([^"]+)"',
            f'id="{field_name}".*?value="([^"]+)"',
            f'name="{field_name}".*?value="([^"]+)"'
        ]
        
        for pattern in patterns:
            match = re.search(pattern, html, re.DOTALL)
            if match:
                return match.group(1)
                
        print(f"Warning: Could not find {field_name} in form")
        return ""

    async def reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Reset user credentials"""
        user_id = update.effective_user.id
        
        # Clear existing credentials for this user
        if user_id in user_data:
            del user_data[user_id]
            self.save_user_data()
        
        await update.message.reply_text(
            "Your credentials have been reset. Please use /start to enter new credentials."
        )
        return ConversationHandler.END

    async def run(self):
        """Run the bot"""
        # Start the captcha worker
        self.captcha_task = asyncio.create_task(self.captcha_worker())
        
        # Initialize browser and pre-solve captcha when starting the server
        await self.initialize_browser()
        
        # Set up periodic captcha refresh and browser check (every 110 seconds)
        scheduler = AsyncIOScheduler()
        scheduler.add_job(self.refresh_browser_session, 'interval', seconds=110)
        scheduler.start()
        
        self.application = Application.builder().token(self.telegram_token).build()

        # Add conversation handler for initial setup
        conv_handler = ConversationHandler(
            entry_points=[
                CommandHandler('start', self.start),
                CommandHandler('reset', self.reset)
            ],
            states={
                USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.get_username)],
                PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.get_password)],
            },
            fallbacks=[CommandHandler('reset', self.reset)],
        )

        # Add handlers
        self.application.add_handler(conv_handler)
        self.application.add_handler(CommandHandler('attendance', self.attendance))
        self.application.add_handler(CommandHandler('reset', self.reset))
        
        # Start the bot
        await self.application.initialize()
        await self.application.start()
        await self.application.updater.start_polling()
        
        try:
            # Keep the bot running
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            # Cancel the captcha worker task
            if self.captcha_task:
                self.captcha_task.cancel()
                try:
                    await self.captcha_task
                except asyncio.CancelledError:
                    pass
            
            # Properly shut down the application
            if self.application.updater.running:
                await self.application.updater.stop()
            await self.application.stop()
            await self.application.shutdown()

    async def save_cookies(self):
        """Save browser cookies for session persistence"""
        if self.driver:
            try:
                cookies = self.driver.get_cookies()
                with open(Path("data/browser_cookies.pkl"), 'wb') as f:
                    pickle.dump(cookies, f)
                logger.info("Browser cookies saved successfully")
            except Exception as e:
                logger.error(f"Error saving cookies: {str(e)}")

    async def load_cookies(self):
        """Load saved browser cookies"""
        try:
            with open(Path("data/browser_cookies.pkl"), 'rb') as f:
                cookies = pickle.load(f)
                for cookie in cookies:
                    self.driver.add_cookie(cookie)
            logger.info("Browser cookies loaded successfully")
            return True
        except Exception as e:
            logger.error(f"Error loading cookies: {str(e)}")
            return False

    def get_cached_attendance(self, user_id):
        """Get cached attendance data if available and not expired"""
        current_time = time_module.time()
        if user_id in self.attendance_cache:
            cached_data, timestamp = self.attendance_cache[user_id]
            if current_time - timestamp < self.cache_timeout:
                logger.info(f"Returning cached attendance data for user {user_id}")
                return cached_data
        return None

    def cache_attendance(self, user_id, attendance_data):
        """Cache attendance data with timestamp"""
        current_time = time_module.time()
        self.attendance_cache[user_id] = (attendance_data, current_time)
        logger.info(f"Cached attendance data for user {user_id}")

    async def send_incremental_attendance(self, update: Update, data_type: str, subjects: list):
        """Send attendance data incrementally by type"""
        if subjects:
            message = f"📊 {data_type} Classes:\n\n"
            for subject in subjects:
                try:
                    percentage_str = subject['percentage'].replace('%', '').strip()
                    percentage = float(percentage_str) if percentage_str else 0
                    emoji = "🟢" if percentage >= 75 else "🔴"
                except (ValueError, TypeError):
                    emoji = "🔴"
                    
                message += f"{emoji} {subject['subject']}\n"
                message += f"├─ Present: {subject['present']}/{subject['total_lectures']}\n"
                message += f"├─ Absent: {subject['absent']}\n"
                message += f"└─ Attendance: {subject['percentage']}\n\n"
            
            await update.message.reply_text(message)
            user_logger.info(f"{update.effective_user.id} - Sent {data_type} attendance data")

if __name__ == "__main__":
    # Start the keep_alive server
    keep_alive()
    
    bot = ERPBot(TELEGRAM_TOKEN, CAPTCHA_API_KEY, ERP_URL)
    
    # Set up and run the event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(bot.run())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()

    # Test 2captcha balance
    solver = TwoCaptcha(CAPTCHA_API_KEY)
    balance = solver.balance()
    print(f"2captcha balance: {balance}")