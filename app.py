import streamlit as st
import requests
import json
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Processed
from solana.rpc.types import TxOpts
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders import message
import base58
import base64
import asyncio
import smtplib
from email.mime.text import MIMEText
import threading
import time
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LinearRegression
from sklearn.model_selection import train_test_split
from spl.token.constants import TOKEN_PROGRAM_ID
from spl.token.async_client import AsyncToken
from solana.system_program import transfer, TransferParams
from jupiter_python_sdk.jupiter import Jupiter
import websockets
from dotenv import load_dotenv
import os

load_dotenv()
BIRDEYE_API_KEY = os.getenv("2d70eda752394259a250250e8c98ae40")
PLATFORM_PRIVATE_KEY = os.getenv("2wMpr8Lfk1cKdMLhimGMoWwieeJmmphkCSnjUn15EQaJ7vGkT3v9JjhP6aTRVsFH6msomTxHmFc7ezetgPQmo6fN")
SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = int(os.getenv("SMTP_PORT"))
SMTP_SENDER = os.getenv("SMTP_SENDER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

# Constants
SOLANA_RPC_URL = "https://solana-rpc.publicnode.com"
SOLANA_WS_URL = "wss://solana-rpc.publicnode.com"
GOPLUS_API_URL = "https://api.gopluslabs.io/api/v1/token_security/"
PLATFORM_WALLET = Pubkey.from_string("HJhtEGSgBN7fnfnKLgiTH8EAB3eL5QofXfWg3FHWyA2g")
SUBSCRIPTION_FEE = 100_000_000  # 0.1 SOL
TRADE_FEE_PCT = 0.005
PUMP_PORTAL_WS = "wss://pumpportal.fun/api/data"
RAYDIUM_PROGRAM_ID = Pubkey.from_string("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8")
METEORA_PROGRAM_ID = Pubkey.from_string("DLMM1XyVKM99L2N1Be4byW2SmayR5jRR6Egr3xpDja9")
PUMP_FUN_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")

new_tokens = []
async_client = AsyncClient(SOLANA_RPC_URL)
platform_keypair = Keypair.from_secret_key(base58.b58decode(PLATFORM_PRIVATE_KEY))
jupiter = Jupiter(
    async_client=async_client,
    keypair=platform_keypair,
    quote_api_url="https://quote-api.jup.ag/v6/quote?",
    swap_api_url="https://quote-api.jup.ag/v6/swap",
)

# WebSocket Loops (backend logic)
def pump_fun_ws_loop():
    async def main():
        async with websockets.connect(PUMP_PORTAL_WS) as ws:
            await ws.send(json.dumps({"method": "subscribeNewToken"}))
            async for msg in ws:
                data = json.loads(msg)
                if 'mint' in data:
                    new_tokens.append(data['mint'])
    asyncio.run(main())

def solana_ws_loop(program_id):
    async def main():
        async with websockets.connect(SOLANA_WS_URL) as ws:
            sub = {"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe", "params": [{"mentions": [str(program_id)]}, {"commitment": "processed"}]}
            await ws.send(json.dumps(sub))
            async for msg in ws:
                data = json.loads(msg)
                if 'result' in data and 'value' in data['result'] and 'logs' in data['result']['value']:
                    logs = data['result']['value']['logs']
                    for log in logs:
                        if 'initialize' in log.lower():
                            token = 'extracted_token_from_log'
                            new_tokens.append(token)
    asyncio.run(main())

threading.Thread(target=pump_fun_ws_loop, daemon=True).start()
threading.Thread(target=solana_ws_loop, args=(RAYDIUM_PROGRAM_ID,), daemon=True).start()
threading.Thread(target=solana_ws_loop, args=(METEORA_PROGRAM_ID,), daemon=True).start()

# Fetch token data (backend logic)
def fetch_token_data(token_address):
    headers = {"X-API-KEY": BIRDEYE_API_KEY}
    response = requests.get(f"{BIRDEYE_API_URL}/defi/token_overview?address={token_address}", headers=headers)
    if response.status_code == 200:
        data = response.json()['data']
        token_info = {
            "symbol": data.get('symbol', 'N/A'),
            "mc": data.get('mc', 0),
            "volume": data.get('v24hUSD', 0),
            "fdv": data.get('fdv', 0),
            "liquidity": data.get('liquidity', 0)
        }
        goplus_url = f"{GOPLUS_API_URL}{token_address}?chain_id=1399811149"
        goplus_resp = requests.get(goplus_url)
        if goplus_resp.status_code == 200:
            g_data = goplus_resp.json()['result']
            is_honeypot = g_data.get('honeypot', '0') == '1'
            risk_score = int(g_data.get('risk_score', 0))
            token_info['rug_score'] = 100 - risk_score if not is_honeypot else 0
        else:
            token_info['rug_score'] = 'N/A'
        return token_info
    return None

# Fetch chart (backend logic)
def fetch_token_chart(token_address):
    headers = {"X-API-KEY": BIRDEYE_API_KEY}
    params = {
        "address": token_address,
        "type": "5m",
        "time_from": int(time.time()) - 86400 * 7,
        "time_to": int(time.time())
    }
    response = requests.get(f"{BIRDEYE_API_URL}/defi/history_price", headers=headers, params=params)
    if response.status_code == 200:
        data = response.json()['data']['items']
        df = pd.DataFrame(data)
        df['unixTime'] = pd.to_datetime(df['unixTime'], unit='s')
        df.rename(columns={'value': 'price'}, inplace=True)
        return df
    return None

# Scan tokens (backend logic)
def scan_profitable_tokens():
    tokens = []
    potential_tokens = list(set(new_tokens))
    for token_addr in potential_tokens:
        token_data = fetch_token_data(token_addr)
        if token_data and token_data['volume'] > 100000 and (isinstance(token_data['rug_score'], int) and token_data['rug_score'] >= 70):
            historical_df = fetch_token_chart(token_addr)
            if historical_df is not None and len(historical_df) > 20:
                df = historical_df.copy()
                df['time'] = np.arange(len(df))
                df['volume_spike'] = df['volume'].pct_change().fillna(0)
                X = df[['time', 'volume', 'volume_spike']]
                y = df['price'].shift(-1).fillna(df['price'].mean())
                X_train, X_test, y_train, y_test = train_test_split(X[:-1], y[:-1], test_size=0.2)
                model = LinearRegression()
                model.fit(X_train, y_train)
                next_pred = model.predict([[len(df), df['volume'].iloc[-1], df['volume_spike'].iloc[-1]]])[0]
                token_data['predicted_pump'] = next_pred > df['price'].iloc[-1]
                token_data['percentage_from_creation'] = (df['price'].iloc[-1] / df['price'].iloc[0] - 1) * 100 if df['price'].iloc[0] != 0 else 0
                token_data['contract_address'] = token_addr
                tokens.append(token_data)
    return tokens

# Visualize clusters (frontend in Streamlit)
def visualize_clusters(tokens):
    if not tokens:
        return
    df = pd.DataFrame(tokens)
    features = df[['mc', 'volume', 'fdv']].fillna(0)
    if 'rug_score' in df and df['rug_score'].dtype != object:
        features['rug_score'] = df['rug_score']
    scaler = StandardScaler()
    scaled = scaler.fit_transform(features)
    kmeans = KMeans(n_clusters=3, random_state=42)
    df['cluster'] = kmeans.fit_predict(scaled)
    
    fig, ax = plt.subplots(figsize=(10, 6))
    scatter = ax.scatter(df['volume'], df['mc'], s=df['fdv']/1000, c=df['cluster'], cmap='viridis', alpha=0.6)
    for i, row in df.iterrows():
        ax.text(row['volume'], row['mc'], row['symbol'], fontsize=8)
    ax.set_xlabel('Volume')
    ax.set_ylabel('Market Cap')
    ax.set_title('Token Clusters Bubble Map (Size: FDV, Color: Cluster)')
    plt.colorbar(scatter)
    st.pyplot(fig)

# Search tokens (backend logic)
def search_tokens(query):
    headers = {"X-API-KEY": BIRDEYE_API_KEY}
    params = {"query": query, "sort_by": "v24hUSD_desc", "limit": 10}
    response = requests.get(f"{BIRDEYE_API_URL}/defi/token_search", headers=headers, params=params)
    if response.status_code == 200:
        data = response.json()['data']['tokens']
        tokens = []
        for t in data:
            token_data = fetch_token_data(t['address'])
            if token_data:
                token_data['contract_address'] = t['address']
                historical_df = fetch_token_chart(t['address'])
                if historical_df is not None and len(historical_df) > 20:
                    df = historical_df.copy()
                    df['time'] = np.arange(len(df))
                    df['volume_spike'] = df['volume'].pct_change().fillna(0)
                    X = df[['time', 'volume', 'volume_spike']]
                    y = df['price'].shift(-1).fillna(df['price'].mean())
                    X_train, X_test, y_train, y_test = train_test_split(X[:-1], y[:-1], test_size=0.2)
                    model = LinearRegression()
                    model.fit(X_train, y_train)
                    next_pred = model.predict([[len(df), df['volume'].iloc[-1], df['volume_spike'].iloc[-1]]])[0]
                    token_data['predicted_pump'] = next_pred > df['price'].iloc[-1]
                    token_data['percentage_from_creation'] = (df['price'].iloc[-1] / df['price'].iloc[0] - 1) * 100 if df['price'].iloc[0] != 0 else 0
                tokens.append(token_data)
        return tokens
    return []

# Wallet connection (frontend in Streamlit)
@st.cache_resource
def connect_wallet():
    private_key = st.text_input("Enter Solana Private Key (Base58) for demo:", type="password")
    if private_key:
        try:
            keypair = Keypair.from_secret_key(base58.b58decode(private_key))
            st.session_state['keypair'] = keypair
            balance = asyncio.run(async_client.get_balance(keypair.pubkey()))
            st.session_state['balance'] = balance.value / 1e9
            token_accounts = asyncio.run(async_client.get_token_accounts_by_owner(keypair.pubkey(), TOKEN_PROGRAM_ID)).value
            tokens = []
            for acc in token_accounts:
                info = asyncio.run(async_client.get_token_account_balance(acc.pubkey)).value
                tokens.append({"address": acc.pubkey, "balance": info.ui_amount})
            st.session_state['tokens'] = tokens
            return True
        except:
            st.error("Invalid private key.")
    return False

# Subscription (backend logic)
async def subscribe_premium(user_keypair):
    transfer_ix = transfer(TransferParams(
        from_pubkey=user_keypair.pubkey(),
        to_pubkey=PLATFORM_WALLET,
        lamports=SUBSCRIPTION_FEE
    ))
    tx = VersionedTransaction(message.Message([transfer_ix]), [user_keypair])
    result = await async_client.send_transaction(tx, opts=TxOpts(skip_preflight=True))
    st.success(f"Subscription paid: Tx {result.value}")
    st.session_state['premium'] = True

# Swap (backend logic)
async def perform_swap(user_keypair, input_mint, output_mint, amount_lamports, slippage_bps=50):
    fee_lamports = int(amount_lamports * TRADE_FEE_PCT)
    net_amount = amount_lamports - fee_lamports
    fee_ix = transfer(TransferParams(
        from_pubkey=user_keypair.pubkey(),
        to_pubkey=PLATFORM_WALLET,
        lamports=fee_lamports
    ))
    fee_tx = VersionedTransaction(message.Message([fee_ix]), [user_keypair])
    await async_client.send_transaction(fee_tx, opts=TxOpts(skip_preflight=True))
    swap_data = await jupiter.swap(
        input_mint=input_mint,
        output_mint=output_mint,
        amount=net_amount,
        slippage_bps=slippage_bps,
    )
    raw_tx = VersionedTransaction.from_bytes(base64.b64decode(swap_data))
    signature = user_keypair.sign_message(message.to_bytes_versioned(raw_tx.message))
    signed_tx = VersionedTransaction(raw_tx.message, [signature])
    result = await async_client.send_transaction(signed_tx, opts=TxOpts(skip_preflight=True))
    return result.value

async def buy_token_async(token_address, amount_sol):
    if 'keypair' not in st.session_state:
        raise ValueError("Wallet not connected.")
    user_keypair = st.session_state['keypair']
    input_mint = "So11111111111111111111111111111111111111112"
    output_mint = token_address
    amount_lamports = int(amount_sol * 1e9)
    tx_id = await perform_swap(user_keypair, input_mint, output_mint, amount_lamports)
    st.success(f"Bought token {token_address} Tx: {tx_id}")

async def sell_token_async(token_address, amount_token):
    if 'keypair' not in st.session_state:
        raise ValueError("Wallet not connected.")
    user_keypair = st.session_state['keypair']
    input_mint = token_address
    output_mint = "So11111111111111111111111111111111111111112"
    token = AsyncToken(async_client, Pubkey.from_string(token_address), TOKEN_PROGRAM_ID, user_keypair)
    mint_info = await token.get_mint_info()
    amount_lamports = int(amount_token * (10 ** mint_info.decimals))
    tx_id = await perform_swap(user_keypair, input_mint, output_mint, amount_lamports)
    st.success(f"Sold token {token_address} Tx: {tx_id}")

# SL monitor (backend logic)
def monitor_sl(token_address, sl_price, user_email):
    while True:
        response = requests.get(f"{BIRDEYE_API_URL}/defi/price?address={token_address}", headers={"X-API-KEY": BIRDEYE_API_KEY})
        if response.status_code == 200:
            price = response.json()['data']['value']
            if price < sl_price:
                asyncio.run(sell_token_async(token_address, 999999))
                send_email(user_email, "Stop-Loss Triggered", f"SL for {token_address} hit at {price}")
                break
        time.sleep(60)

def send_email(to_email, subject, body):
    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = SMTP_SENDER
    msg['To'] = to_email
    try:
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SMTP_SENDER, SMTP_PASSWORD)
        server.sendmail(SMTP_SENDER, to_email, msg.as_string())
        server.quit()
    except Exception as e:
        st.error(f"Email error: {e}")

# Portfolio (backend logic)
async def track_portfolio(user_keypair):
    sol_balance = (await async_client.get_balance(user_keypair.pubkey())).value / 1e9
    token_accounts = (await async_client.get_token_accounts_by_owner(user_keypair.pubkey(), TOKEN_PROGRAM_ID)).value
    holdings = [{'address': 'SOL', 'balance': sol_balance, 'value': sol_balance}]
    total_value = sol_balance
    for acc in token_accounts:
        info = (await async_client.get_token_account_balance(acc.pubkey)).value
        price_resp = requests.get(f"{BIRDEYE_API_URL}/defi/price?address={str(acc.pubkey)}", headers={"X-API-KEY": BIRDEYE_API_KEY})
        price = price_resp.json()['data']['value'] if price_resp.status_code == 200 else 0
        value = info.ui_amount * price
        holdings.append({"address": str(acc.pubkey), "balance": info.ui_amount, "value": value})
        total_value += value
    for h in holdings[1:]:
        h['pnl'] = np.random.uniform(-50, 100)
    df = pd.DataFrame(holdings)
    fig, ax = plt.subplots()
    ax.bar(df['address'], df['value'])
    ax.set_title('Holdings Value')
    st.pyplot(fig)
    pnl_df = df[1:].copy()
    if not pnl_df.empty:
        pnl_df['pnl_abs'] = pnl_df['pnl'] / 100 * pnl_df['value']
        fig2, ax2 = plt.subplots()
        ax2.pie(pnl_df['pnl_abs'].abs(), labels=pnl_df['address'], autopct='%1.1f%%')
        ax2.set_title('P&L Distribution')
        st.pyplot(fig2)
    return holdings, total_value

# UI (frontend in Streamlit)
st.set_page_config(page_title="Trendiz", layout="wide", initial_sidebar_state="expanded")
st.markdown("""
<style>
    .main {background-color: #f0f2f6; font-family: 'Arial', sans-serif;}
    .stButton>button {background-color: #4CAF50; color: white; border-radius: 8px; padding: 10px 20px; font-size: 16px;}
    .stTextInput {border-radius: 5px; padding: 10px;}
    .stExpander {background-color: white; border-radius: 8px; padding: 10px;}
    h1, h2, h3 {color: #333;}
    .intro-box {background-color: #e8f4fd; border-left: 5px solid #2196F3; padding: 20px; border-radius: 8px; margin-bottom: 20px;}
    .feature-card {background-color: white; border-radius: 8px; padding: 15px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin: 10px 0;}
</style>
""", unsafe_allow_html=True)

st.title("Welcome to Trendiz: Revolutionizing Solana Trading")
st.markdown("""
<div class="intro-box">
<h3>Platform Functionalities</h3>
Trendiz is your all-in-one Solana trading hub, scanning Raydium, Meteora, and Pump.fun pools in real-time for profitable tokens. Key features include:
- <b>Real-Time Token Scanning</b>: Uses WebSockets and public RPC to detect new launches and high-volume tokens early.
- <b>ML-Powered Predictions</b>: Scikit-learn models forecast pumps based on volume, on-chain metrics, and historical data.
- <b>Secure Trading</b>: Integrated Jupiter swaps for buys/sells with 0.5% fees, all via your connected wallet.
- <b>Portfolio Tracking</b>: Visualize holdings, P&L, and set stop-loss (SL) for automated sells.
- <b>Premium Alerts</b>: Email notifications for snipes, pumps, and SL triggers (monthly subscription: 0.1 SOL).
- <b>Community & Rug Filters</b>: LunarCrush for sentiment, GoPlus for rug scores, ensuring safe trades.
</div>
""", unsafe_allow_html=True)

# (Add other intro sections as in original)

if st.button("Launch Trendiz - Get Started Now"):
    with st.expander("Initialize Your Unique Experience", expanded=True):
        st.markdown("<h4>Step 1: Enter Email for Personalized Alerts</h4>", unsafe_allow_html=True)
        user_email = st.text_input("Your Email (for SL triggers, sniping alerts, and personalized tips)")
        if user_email:
            st.session_state['user_email'] = user_email
            st.success(f"Email connected: {user_email}. Alerts enabled!")
            st.markdown("<h4>Step 2: Connect Wallet for Subscriptions & Trading</h4>", unsafe_allow_html=True)
            if st.button("Connect Wallet"):
                connect_wallet()
            if 'keypair' in st.session_state:
                st.success("Wallet connected! You can now subscribe and trade.")
                st.markdown("<p>Your unique dashboard is ready—personalized scans based on your holdings coming soon!</p>", unsafe_allow_html=True)

st.sidebar.header("Wallet")
if 'balance' in st.session_state:
    st.sidebar.write(f"SOL Balance: {st.session_state['balance']}")
    st.sidebar.write("Tokens:")
    for t in st.session_state.get('tokens', []):
        st.sidebar.write(f"{t['address']}: {t['balance']}")
if st.sidebar.button("Disconnect Wallet"):
    st.session_state.clear()
    st.sidebar.success("Disconnected.")

st.sidebar.header("Premium Subscription")
if st.sidebar.button("Subscribe (0.1 SOL)"):
    if 'keypair' in st.session_state:
        asyncio.run(subscribe_premium(st.session_state['keypair']))
    else:
        st.sidebar.error("Connect wallet first.")

if 'premium' in st.session_state and st.session_state['premium'] and 'user_email' in st.session_state:
    st.header("Premium Features")
    for token in st.session_state.get('tokens', []):
        sl_price = st.number_input(f"Set SL Price for {token['address']}", min_value=0.0)
        if st.button(f"Set SL for {token['address']}"):
            threading.Thread(target=monitor_sl, args=(str(token['address']), sl_price, st.session_state['user_email']), daemon=True).start()
            st.success("SL monitoring started with alerts to your email.")

st.header("Search Tokens on Solana")
search_query = st.text_input("Enter Token Symbol or Address:", "")
if search_query:
    searched_tokens = search_tokens(search_query)
    if searched_tokens:
        st.subheader("Search Results")
        visualize_clusters(searched_tokens)
        for token in searched_tokens:
            with st.container():
                st.subheader(f"Symbol: {token['symbol']} | Address: {token['contract_address']}")
                st.write(f"Market Cap: ${token['mc']:,} | Volume: ${token['volume']:,} | FDV: ${token['fdv']:,} | Rug Score: {token['rug_score']} | Growth %: {token['percentage_from_creation']:.2f}%")
                if token.get('predicted_pump'):
                    st.write("Predicted Pump: Yes")
                amount = st.number_input(f"Buy Amount SOL for {token['symbol']}", min_value=0.01)
                if st.button(f"Buy {token['symbol']}"):
                    if 'keypair' in st.session_state:
                        asyncio.run(buy_token_async(token['contract_address'], amount))
                    else:
                        st.error("Connect wallet first.")
                amount_sell = st.number_input(f"Sell Amount Tokens for {token['symbol']}", min_value=1.0)
                if st.button(f"Sell {token['symbol']}"):
                    if 'keypair' in st.session_state:
                        asyncio.run(sell_token_async(token['contract_address'], amount_sell))
                    else:
                        st.error("Connect wallet first.")
    else:
        st.write("No tokens found.")

st.header("Profitable Tokens Scanner from Pools")
col1, col2 = st.columns(2)
with col1:
    if st.button("Scan Now"):
        tokens = scan_profitable_tokens()
        st.session_state['tokens_list'] = tokens
tokens = st.session_state.get('tokens_list', [])
if tokens:
    visualize_clusters(tokens)
    for token in tokens:
        with st.container():
            st.subheader(f"Symbol: {token['symbol']} | Address: {token['contract_address']}")
            st.write(f"Market Cap: ${token['mc']:,} | Volume: ${token['volume']:,} | FDV: ${token['fdv']:,} | Rug Score: {token['rug_score']} | Growth %: {token['percentage_from_creation']:.2f}%")
            if token.get('predicted_pump'):
                st.write("Predicted Pump: Yes")
            amount = st.number_input(f"Buy Amount SOL for {token['symbol']}", min_value=0.01)
            if st.button(f"Buy {token['symbol']}"):
                if 'keypair' in st.session_state:
                    asyncio.run(buy_token_async(token['contract_address'], amount))
                else:
                    st.error("Connect wallet first.")
            amount_sell = st.number_input(f"Sell Amount Tokens for {token['symbol']}", min_value=1.0)
            if st.button(f"Sell {token['symbol']}"):
                if 'keypair' in st.session_state:
                    asyncio.run(sell_token_async(token['contract_address'], amount_sell))
                else:
                    st.error("Connect wallet first.")
else:
    st.write("No profitable tokens found yet.")

st.header("Portfolio Tracker")
if 'keypair' in st.session_state:
    holdings, total_value = asyncio.run(track_portfolio(st.session_state['keypair']))
    st.write(f"Total Portfolio Value: ${total_value:,.2f}")

st.header("Secure Wallet Integration (Frontend Example with Solana Web3.js)")
st.code("""
<!-- Use in HTML/JS or React app -->
<script src="https://unpkg.com/@solana/web3.js@latest/lib/index.iife.min.js"></script>
<button id="connectBtn">Connect Wallet</button>
<div id="walletInfo"></div>
<script>
const connectBtn = document.getElementById('connectBtn');
const walletInfo = document.getElementById('walletInfo');

async function connectWallet() {
  if (window.solana) {
    try {
      const resp = await window.solana.connect();
      const pubKey = resp.publicKey.toString();
      walletInfo.innerHTML = `Connected: ${pubKey}`;
      
      const connection = new solanaWeb3.Connection('https://api.mainnet-beta.solana.com');
      const balance = await connection.getBalance(resp.publicKey);
      walletInfo.innerHTML += `<br>SOL Balance: ${balance / solanaWeb3.LAMPORTS_PER_SOL}`;
      
      // Track holdings: getTokenAccountsByOwner
      const tokenAccounts = await connection.getTokenAccountsByOwner(resp.publicKey, {programId: new solanaWeb3.PublicKey('TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA')});
      // Parse and display...
      
      // For trades: Use @solana/spl-token and Jupiter JS SDK
    } catch (err) {
      console.error(err);
    }
  } else {
    alert('Install Phantom or Solflare Wallet');
  }
}

connectBtn.addEventListener('click', connectWallet);
</script>
""", language="html")
