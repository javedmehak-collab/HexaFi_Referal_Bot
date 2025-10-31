import os
import re
from decimal import Decimal
from dotenv import load_dotenv
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from tinydb import TinyDB, Query
from web3 import Web3
from web3.middleware import geth_poa_middleware

# --- Configuration (Loads from .env file)
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
BSC_RPC = os.getenv("BSC_RPC")
HXF_TOKEN_ADDRESS = Web3.to_checksum_address(os.getenv("HXF_TOKEN_ADDRESS"))
DISTRIBUTOR_PRIVATE_KEY = os.getenv("DISTRIBUTOR_PRIVATE_KEY")
DISTRIBUTOR_ADDRESS = Web3.to_checksum_address(os.getenv("DISTRIBUTOR_ADDRESS"))
REWARD_PER_REFERRAL = Decimal(os.getenv("REWARD_PER_REFERRAL", "1"))
CLAIM_THRESHOLD = int(os.getenv("CLAIM_THRESHOLD", "5"))
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL")  # e.g. @HexaFiOfficial

# --- Init
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
db = TinyDB('hxf_referrals.json')
U = Query()

# --- Web3 Init
w3 = Web3(Web3.HTTPProvider(BSC_RPC))
# Inject middleware for Proof-of-Authority chains like BSC
w3.middleware_onion.inject(geth_poa_middleware, layer=0)

ERC20_ABI = [
    {"constant":True,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"},
    {"constant":True,"inputs":[{"name":"owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"},
    {"constant":False,"inputs":[{"name":"to","type":"address"},{"name":"amount","type":"uint256"}],"name":"transfer","outputs":[{"name":"","type":"bool"}],"type":"function"},
]
token = w3.eth.contract(address=HXF_TOKEN_ADDRESS, abi=ERC20_ABI)
TOKEN_DECIMALS = token.functions.decimals().call()
TOKEN_FACTOR = Decimal(10) ** TOKEN_DECIMALS

# --- Helpers

def get_user(user_id: int):
    r = db.search(U.user_id == user_id)
    return r[0] if r else None

def save_user(user_id: int, username: str):
    existing = get_user(user_id)
    if existing:
        return existing
    rec = {
        "user_id": user_id,
        "username": username,
        "referrer_id": None,
        "referrals": 0,
        "joined_channel": False,
        "wallet": None,
        "claimed": 0,  # total HXF claimed
        "eligible_refs": 0 # referrals that met conditions
    }
    db.insert(rec)
    return rec

def set_referrer(new_user_id: int, referrer_id: int):
    if new_user_id == referrer_id:
        return False  # no self-referral
    ref_user = get_user(referrer_id)
    if not ref_user:
        return False
    # set only once
    db.update({"referrer_id": referrer_id}, U.user_id == new_user_id)
    # increment raw referrals; final eligibility requires channel join
    db.update({"referrals": ref_user["referrals"] + 1}, U.user_id == referrer_id)
    return True

def is_valid_bep20(addr: str):
    try:
        return Web3.is_address(addr) and addr.startswith("0x")
    except:
        return False

def get_member_status(chat_username: str, user_id: int):
    # This requires the bot to be an Admin in the channel
    try:
        member = bot.get_chat_member(chat_username, user_id)
        return member.status  # "creator","administrator","member","restricted","left","kicked"
    except Exception:
        # Fails if channel is private, bot is not admin, or username is wrong
        return None

def mark_joined_if_member(user_id: int):
    if not REQUIRED_CHANNEL:
        return False
    status = get_member_status(REQUIRED_CHANNEL, user_id)
    if status in ("creator","administrator","member"):
        # Only update the database field if the user actually wasn't marked before
        user = get_user(user_id)
        if not user.get("joined_channel"):
             db.update({"joined_channel": True}, U.user_id == user_id)

             # If this user has a referrer, update the referrer's eligible count
             referrer_id = user.get("referrer_id")
             if referrer_id:
                 # Recalculate and update the referrer's eligible count
                 eligible = calc_eligible_refs(referrer_id)
                 db.update({"eligible_refs": eligible}, U.user_id == referrer_id)

             return True
        return True # Was already marked as joined
    return False

def calc_eligible_refs(user_id: int):
    """
    Count referrals whose accounts have actually joined the channel.
    """
    # Query for users where referrer_id matches and they have joined the channel
    count = len(db.search((U.referrer_id == user_id) & (U.joined_channel == True)))
    return count

def claimable_amount(user_id: int):
    u = get_user(user_id)
    if not u:
        return Decimal(0)
    
    # Check current total eligible referrals
    eligible_now = calc_eligible_refs(user_id)
    
    # Check the amount of eligible referrals already claimed/accounted for
    eligible_accounted = u.get("eligible_refs", 0)
    
    # Only allow claim if total eligible is >= threshold at least once
    if eligible_now < CLAIM_THRESHOLD and eligible_accounted == 0:
        return Decimal(0)
    
    # Calculate new eligible refs since the last claim/update
    new_eligible = max(0, eligible_now - eligible_accounted)

    # User can claim on the delta
    return Decimal(new_eligible) * REWARD_PER_REFERRAL

def send_hxf(to_addr: str, amount_token: Decimal):
    amount_wei = int(amount_token * TOKEN_FACTOR)
    
    # Ensure address is checksummed
    to_checksum_addr = Web3.to_checksum_address(to_addr)

    # Build and sign transaction
    nonce = w3.eth.get_transaction_count(DISTRIBUTOR_ADDRESS)
    tx = token.functions.transfer(to_checksum_addr, amount_wei).build_transaction({
        "from": DISTRIBUTOR_ADDRESS,
        "nonce": nonce,
        "gasPrice": w3.eth.gas_price,
    })
    
    # Gas estimate
    gas = w3.eth.estimate_gas(tx)
    tx["gas"] = gas
    
    signed = w3.eth.account.sign_transaction(tx, private_key=DISTRIBUTOR_PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
    
    # Wait for transaction confirmation
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    
    if receipt.status != 1:
        raise RuntimeError(f"Transfer failed on-chain. Receipt status: {receipt.status}")
        
    return tx_hash.hex()

# --- Commands

@bot.message_handler(commands=["start"])
def start_cmd(message):
    user_id = message.from_user.id
    username = message.from_user.username or str(user_id)
    
    # Ensure record exists
    user = get_user(user_id)
    if not user:
        user = save_user(user_id, username)

    # Process referral link if available and referrer not set
    args = message.text.split()
    if len(args) > 1 and user.get("referrer_id") is None:
        try:
            ref_id = int(args[1])
            if set_referrer(user_id, ref_id):
                try:
                    # Notify the referrer
                    bot.send_message(ref_id, f"🎉 New referral joined: <b>@{username}</b>")
                except:
                    pass
        except:
            pass

    # Verify channel membership (and update referrer's eligible count if a new join)
    if REQUIRED_CHANNEL:
        mark_joined_if_member(user_id)
    
    link = f"https://t.me/{bot.get_me().username}?start={user_id}"
    kb = InlineKeyboardMarkup()
    if REQUIRED_CHANNEL:
        # Create a channel join button using the username (e.g., t.me/HexaFiOfficial)
        kb.add(InlineKeyboardButton("🔗 Join Required Channel", url=f"https://t.me/{REQUIRED_CHANNEL.replace('@','')}"))
        
    required_text = (
        f"• You must join **{REQUIRED_CHANNEL}** to make your referrals eligible "
        f"and for you to be able to claim."
    ) if REQUIRED_CHANNEL else ""
    
    response_text = (
        f"👋 Welcome <b>@{username}</b>!\n\n"
        f"Your personal referral link:\n<code>{link}</code>\n\n"
        f"• Earn **{REWARD_PER_REFERRAL} HXF** per <i>eligible</i> referral\n"
        f"• Minimum **{CLAIM_THRESHOLD}** eligible referrals required to claim\n"
        f"{required_text}"
    )

    bot.reply_to(message, response_text, reply_markup=kb)

@bot.message_handler(commands=["stats"])
def stats_cmd(message):
    user_id = message.from_user.id
    u = get_user(user_id)
    
    if not u:
        bot.reply_to(message, "Use /start first.")
        return
        
    # Re-check channel membership just in case the user joined after /start
    if REQUIRED_CHANNEL:
        mark_joined_if_member(user_id)
        
    raw_refs = u.get("referrals", 0)
    eligible = calc_eligible_refs(user_id) # Calculates based on current DB state
    claimed = Decimal(u.get("claimed", 0))
    claimable = claimable_amount(user_id) # Calculates based on the delta
    
    wallet_status = u.get('wallet')
    
    bot.reply_to(message,
        f"📊 **Referral Statistics**\n\n"
        f"👥 Total Referrals: {raw_refs}\n"
        f"✅ Eligible Referrals: {eligible}\n"
        f"💰 Claimable HXF: **{claimable}**\n"
        f"🧾 Total Claimed: {claimed}\n"
        f"🔗 Wallet Status: {'Set' if wallet_status else 'Not Set (use /wallet)'}"
    )

@bot.message_handler(commands=["wallet"])
def wallet_cmd(message):
    """
    /wallet -> show
    /wallet 0xabc... -> set
    """
    user_id = message.from_user.id
    u = get_user(user_id)
    if not u:
        bot.reply_to(message, "Use /start first.")
        return

    parts = message.text.strip().split()
    if len(parts) == 1:
        bot.reply_to(message, f"Your registered wallet: <code>{u.get('wallet') or 'not set'}</code>\nSend /wallet 0xYourBEP20Address to set.")
        return

    addr = parts[1].strip()
    if not is_valid_bep20(addr):
        bot.reply_to(message, "❌ Invalid BEP-20 address. It should start with 0x and be a valid format.")
        return

    db.update({"wallet": Web3.to_checksum_address(addr)}, U.user_id == user_id)
    bot.reply_to(message, f"✅ Wallet set to:\n<code>{addr}</code>")

@bot.message_handler(commands=["claim"])
def claim_cmd(message):
    user_id = message.from_user.id
    u = get_user(user_id)
    
    if not u:
        bot.reply_to(message, "Use /start first.")
        return
        
    # Final check for channel eligibility before claim
    if REQUIRED_CHANNEL and not u.get("joined_channel"):
        if not mark_joined_if_member(user_id):
            bot.reply_to(message, f"❌ You must join {REQUIRED_CHANNEL} to be eligible to claim.")
            return

    wallet = u.get("wallet")
    if not wallet:
        bot.reply_to(message, "❌ Set your wallet first using /wallet 0xYourBEP20Address")
        return

    amount = claimable_amount(user_id)
    
    if amount <= 0:
        eligible_now = calc_eligible_refs(user_id)
        if eligible_now < CLAIM_THRESHOLD:
            bot.reply_to(message, f"❌ Not enough eligible referrals to claim. You need at least **{CLAIM_THRESHOLD}** (currently: {eligible_now}).")
        else:
            bot.reply_to(message, f"❌ You have no new claimable rewards at this time (Claimable: 0 HXF).")
        return

    try:
        # Inform the user that the claim process has started
        m = bot.reply_to(message, f"⏳ Processing claim for **{amount} HXF** to <code>{wallet}</code>. This may take a minute...")
        
        tx_hash = send_hxf(wallet, amount)
        
        # --- Update Database State AFTER Successful Transaction ---
        
        # Current total eligible count (which is now all accounted for)
        eligible_now = calc_eligible_refs(user_id)
        
        # The amount already claimed in the DB
        claimed_so_far = Decimal(u.get("claimed", 0))

        # Update the user record
        db.update({
            "eligible_refs": eligible_now, # Mark all current eligible refs as accounted for
            "claimed": claimed_so_far + amount # Add the claimed amount
        }, U.user_id == user_id)

        # Update the message to confirm success
        bot.edit_message_text(
            chat_id=m.chat.id, 
            message_id=m.message_id, 
            text=(
                f"✅ **Claim Successful!** 🎉\n\n"
                f"Sent **{amount} HXF** to <code>{wallet}</code>\n"
                f"🔗 Tx: <a href='https://bscscan.com/tx/{tx_hash}'>View Transaction on BscScan</a>"
            ),
            parse_mode="HTML"
        )
        
    except RuntimeError as e:
        bot.reply_to(message, f"❌ **Claim failed on-chain:** {e}. Please ensure the distributor wallet has enough BNB for gas and HXF tokens.")
    except Exception as e:
        # Catch other errors like network issues or W3 init failure
        bot.reply_to(message, f"❌ **An unexpected error occurred:** {e}")

@bot.message_handler(commands=["leaderboard"])
def leaderboard_cmd(message):
    users = db.all()
    rows = []
    
    # Calculate eligible referrals for every user
    for u in users:
        eligible_count = calc_eligible_refs(u["user_id"])
        # Only include users with at least 1 eligible referral
        if eligible_count > 0:
            rows.append((u.get("username") or u["user_id"], eligible_count))
            
    rows.sort(key=lambda x: x[1], reverse=True)
    top = rows[:10]
    
    text = "🏆 **Top Referrers**\n\n"
    if not top:
        text += "No eligible referral data yet."
    else:
        for i,(name,score) in enumerate(top, start=1):
            # Using str(name) in case username is None
            text += f"{i}. @{str(name)} — **{score}** eligible\n"
            
    bot.reply_to(message, text, parse_mode="HTML")

# --- Admin Commands (Requires your numerical User ID)

# 🚀 Your numerical User ID is now configured here: 
ADMIN_IDS = set({7990561188}) 

def is_admin(uid:int)->bool:
    return uid in ADMIN_IDS

@bot.message_handler(commands=["setreward"])
def setreward_cmd(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        bot.reply_to(message, "Usage: /setreward 1.5 (sets reward to 1.5 HXF)")
        return
    global REWARD_PER_REFERRAL
    try:
        REWARD_PER_REFERRAL = Decimal(parts[1])
        bot.reply_to(message, f"✅ **REWARD_PER_REFERRAL** set to **{REWARD_PER_REFERRAL} HXF**")
    except:
        bot.reply_to(message, "❌ Invalid number format.")

@bot.message_handler(commands=["setthreshold"])
def setthreshold_cmd(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        bot.reply_to(message, "Usage: /setthreshold 5 (sets minimum claims to 5 referrals)")
        return
    global CLAIM_THRESHOLD
    try:
        CLAIM_THRESHOLD = int(parts[1])
        bot.reply_to(message, f"✅ **CLAIM_THRESHOLD** set to **{CLAIM_THRESHOLD}** eligible referrals")
    except:
        bot.reply_to(message, "❌ Invalid integer format.")

# --- Start Polling ---
print("HexaFi Referral Bot is running…")
bot.infinity_polling()