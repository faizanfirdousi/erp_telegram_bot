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
import logging

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
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
        
        # Initialize encryption
        self.key = self.load_or_create_key()
        self.cipher_suite = Fernet(self.key)
        
        # Load existing user data if available
        self.load_user_data()
        
        # Setup Chrome options for visible mode
        self.chrome_options = Options()
        # self.chrome_options.add_argument('--headless')  # Commented out to make browser visible
        self.chrome_options.add_argument('--start-maximized')  # Start with maximized window
        self.chrome_options.add_argument('--disable-dev-shm-usage')
        self.chrome_options.add_argument('--no-sandbox')
        self.chrome_options.add_argument('--window-size=1920,1080')

    def load_or_create_key(self):
        """Load existing key or create a new one"""
        try:
            with open('encryption_key.key', 'rb') as f:
                return f.read()
        except FileNotFoundError:
            # Generate new key if none exists
            key = Fernet.generate_key()
            with open('encryption_key.key', 'wb') as f:
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
        
        with open('user_data.pkl', 'wb') as f:
            pickle.dump(encrypted_data, f)
            
    def load_user_data(self):
        """Load and decrypt user data from file"""
        global user_data
        try:
            with open('user_data.pkl', 'rb') as f:
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
            if os.path.exists('user_data.pkl'):
                os.remove('user_data.pkl')

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
        
        try:
            print(f"Attempting login with username: {user_data[user_id]['username']}")
            screenshot_data = await self.check_attendance(user_id)
            
            if screenshot_data and len(screenshot_data) > 0:
                # Save the screenshot temporarily
                temp_file = f"temp_attendance_{user_id}.png"
                with open(temp_file, "wb") as f:
                    f.write(screenshot_data)
                
                # Send the file
                try:
                    with open(temp_file, "rb") as f:
                        await update.message.reply_document(
                            document=f,
                            filename=f"attendance_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png",
                            caption="Here's your attendance update!"
                        )
                finally:
                    # Clean up temp file
                    if os.path.exists(temp_file):
                        os.remove(temp_file)
            else:
                await update.message.reply_text(
                    "Sorry, I couldn't fetch your attendance. The screenshot was empty."
                )
        except Exception as e:
            await update.message.reply_text(
                f"Error fetching attendance: {str(e)}"
            )

    async def check_attendance(self, user_id):
        """Check attendance using Selenium with improved error handling"""
        driver = None
        try:
            logger.info("Starting attendance check process")
            driver = webdriver.Chrome(options=self.chrome_options)
            
            # Navigate to login page
            logger.info("Navigating to login page")
            driver.get(self.erp_url)
            
            # Wait for and fill username
            username_field = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.ID, "txtUSERNAME"))
            )
            username_field.clear()
            username_field.send_keys(user_data[user_id]["username"])
            logger.info("Username entered successfully")
            
            # Wait for and fill password
            password_field = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.ID, "txtPASSWORD"))
            )
            password_field.clear()
            password_field.send_keys(user_data[user_id]["password"])
            logger.info("Password entered successfully")
            
            # Handle captcha
            try:
                logger.info("Starting captcha solving process")
                solver = TwoCaptcha(self.captcha_api_key)
                
                # Solve captcha
                result = solver.recaptcha(
                    sitekey="6Le73cMbAAAAANUPFMh89e5vPsfwqyiwAh8x4ylp",
                    url=self.erp_url,
                    version='v2'
                )
                
                logger.info("Captcha solved successfully")
                
                # Simply set the response in the textarea
                driver.execute_script(
                    "document.getElementById('g-recaptcha-response').innerHTML = arguments[0];",
                    result['code']
                )
                
                # Wait a moment
                await asyncio.sleep(2)
                
                # Click login button immediately after setting captcha
                login_button = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.ID, "btnSUBMIT"))
                )
                driver.execute_script("arguments[0].click();", login_button)
                logger.info("Login button clicked")
                
            except Exception as e:
                logger.error(f"Captcha solving failed: {str(e)}")
                raise
            
            # Wait for attendance table with a longer timeout
            logger.info("Waiting for attendance table")
            try:
                attendance_table = WebDriverWait(driver, 30).until(
                    EC.presence_of_element_located((By.ID, "ctl00_ContentPlaceHolder1_ctl03_grdTHERORY"))
                )
                logger.info("Attendance table found")
            except Exception as e:
                logger.error("Could not find attendance table")
                raise
            
            # After finding attendance table, handle screenshot properly
            logger.info("Preparing to take screenshot")
            
            # Scroll to top first
            driver.execute_script("window.scrollTo(0, 0);")
            await asyncio.sleep(1)
            
            # Find the attendance div container
            attendance_div = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CLASS_NAME, "attendanceW"))
            )
            
            # Scroll the attendance div into view
            driver.execute_script("arguments[0].scrollIntoView(true);", attendance_div)
            await asyncio.sleep(1)
            
            # Scroll up slightly to show the full table (accounting for any fixed headers)
            driver.execute_script("window.scrollBy(0, -100);")
            await asyncio.sleep(1)
            
            # Get the table dimensions
            table = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.ID, "ctl00_ContentPlaceHolder1_ctl03_grdTHERORY"))
            )
            location = table.location
            size = table.size
            
            # Take full page screenshot
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_path = os.path.abspath(f"attendance_{user_id}_{timestamp}.png")
            logger.info(f"Taking screenshot: {screenshot_path}")
            
            # Set window size to ensure all content is visible
            total_height = driver.execute_script("return document.body.scrollHeight")
            driver.set_window_size(1920, total_height)
            await asyncio.sleep(1)
            
            # Take the screenshot
            driver.save_screenshot(screenshot_path)
            logger.info("Screenshot saved successfully")
            
            # Verify the screenshot exists and has content
            if not os.path.exists(screenshot_path):
                raise Exception("Screenshot file was not created")
            
            if os.path.getsize(screenshot_path) == 0:
                raise Exception("Screenshot file is empty")
            
            # Read the screenshot
            with open(screenshot_path, "rb") as f:
                screenshot_data = f.read()
            
            logger.info(f"Screenshot size: {len(screenshot_data)} bytes")
            
            # Clean up
            os.remove(screenshot_path)
            logger.info("Screenshot cleanup completed")
            
            return screenshot_data
            
        except Exception as e:
            logger.error(f"Error during attendance check: {str(e)}")
            if driver:
                try:
                    error_path = os.path.abspath(f"error_{user_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png")
                    driver.save_screenshot(error_path)
                    logger.info(f"Error screenshot saved as {error_path}")
                except Exception as screenshot_error:
                    logger.error(f"Failed to take error screenshot: {str(screenshot_error)}")
            raise
            
        finally:
            if driver:
                driver.quit()
                logger.info("Browser session closed")

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
        self.application = Application.builder().token(self.telegram_token).build()

        # Add conversation handler for initial setup
        conv_handler = ConversationHandler(
            entry_points=[
                CommandHandler('start', self.start),
                CommandHandler('reset', self.reset)  # Add reset as an entry point
            ],
            states={
                USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.get_username)],
                PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.get_password)],
            },
            fallbacks=[CommandHandler('reset', self.reset)],  # Add reset as a fallback
        )

        # Add handlers
        self.application.add_handler(conv_handler)
        self.application.add_handler(CommandHandler('attendance', self.attendance))
        self.application.add_handler(CommandHandler('reset', self.reset))  # Add standalone reset handler
        
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
            # Properly shut down the application
            if self.application.updater.running:
                await self.application.updater.stop()
            await self.application.stop()
            await self.application.shutdown()

if __name__ == "__main__":
    # Update with your actual token
    TELEGRAM_TOKEN = "7876123272:AAFmca-po2bmQ1Z0d6BIfDsAptddQIREw18"
    CAPTCHA_API_KEY = "dfe25dfb57892e1f51ba087c3a92fcba"
    ERP_URL = "https://isquareit.akronsystems.com/pLogin.aspx"
    
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
solver = TwoCaptcha('dfe25dfb57892e1f51ba087c3a92fcba')
balance = solver.balance()
print(f"2captcha balance: {balance}") 