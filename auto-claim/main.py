import time
import requests
import datetime
from web3 import Web3
from eth_account import Account
from dotenv import load_dotenv
import os

load_dotenv("../.env")

# ================= КОНФИГУРАЦИЯ =================

# 1. Приватный ключ вашего кошелька (Владелец Proxy)
PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY")

# 2. Адрес Proxy Wallet (Gnosis Safe)
PROXY_ADDRESS = os.getenv("PM_ADDRESS")

# 3. RPC Polygon
# Рекомендую использовать Alchemy или Infura, если публичный RPC будет отваливаться
RPC_URL = "https://polygon-rpc.com"

# Интервал проверки в секундах (15 минут = 900 секунд)
CHECK_INTERVAL = 5 * 60

# ================= КОНСТАНТЫ И ABI =================

CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

CTF_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"}
        ],
        "name": "redeemPositions",
        "outputs": [],
        "payable": False,
        "stateMutability": "nonpayable",
        "type": "function"
    }
]

SAFE_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "value", "type": "uint256"},
            {"internalType": "bytes", "name": "data", "type": "bytes"},
            {"internalType": "enum Enum.Operation", "name": "operation", "type": "uint8"},
            {"internalType": "uint256", "name": "safeTxGas", "type": "uint256"},
            {"internalType": "uint256", "name": "baseGas", "type": "uint256"},
            {"internalType": "uint256", "name": "gasPrice", "type": "uint256"},
            {"internalType": "address", "name": "gasToken", "type": "address"},
            {"internalType": "address", "name": "refundReceiver", "type": "address"},
            {"internalType": "bytes", "name": "signatures", "type": "bytes"}
        ],
        "name": "execTransaction",
        "outputs": [{"internalType": "bool", "name": "success", "type": "bool"}],
        "stateMutability": "payable",
        "type": "function"
    }
]

def log(message):
    """Выводит сообщение с текущим временем."""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {message}")

def get_raw_tx_bytes(signed_tx):
    """Безопасно извлекает rawTransaction для разных версий Web3.py"""
    if hasattr(signed_tx, 'raw_transaction'):
        return signed_tx.raw_transaction
    if hasattr(signed_tx, 'rawTransaction'):
        return signed_tx.rawTransaction
    if isinstance(signed_tx, dict) and 'rawTransaction' in signed_tx:
        return signed_tx['rawTransaction']
    return signed_tx[0] if isinstance(signed_tx, (tuple, list)) else signed_tx

def get_redeemable_markets(proxy_address):
    log("🔍 Проверка доступных клеймов через API...")
    url = "https://data-api.polymarket.com/positions"
    params = {"user": proxy_address, "redeemable": "true", "limit": 50}

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        conditions = set()
        for item in data:
            if float(item.get('size', 0)) > 0:
                conditions.add(item.get('conditionId'))
        return list(conditions)
    except Exception as e:
        log(f"⚠️ Ошибка API Polymarket (не страшно, попробуем позже): {e}")
        return []

def redeem_via_proxy(w3, account, condition_id):
    proxy = w3.eth.contract(address=PROXY_ADDRESS, abi=SAFE_ABI)
    ctf = w3.eth.contract(address=CTF_ADDRESS, abi=CTF_ABI)

    log(f"⚙️ Подготовка клейма для ID: {condition_id}")

    try:
        cond_id_bytes = bytes.fromhex(condition_id.replace("0x", ""))

        # 1. Формируем payload для CTF
        ctf_tx_dummy = ctf.functions.redeemPositions(
            USDC_ADDRESS,
            b'\x00' * 32,
            cond_id_bytes,
            [1, 2]
        ).build_transaction({
            'chainId': 137,
            'gas': 0, 'gasPrice': 0,
            'from': "0x0000000000000000000000000000000000000000"
        })
        ctf_data = ctf_tx_dummy['data']

        # 2. Подпись владельца
        owner_int = int(account.address, 16)
        signature = (
            owner_int.to_bytes(32, 'big') +
            (0).to_bytes(32, 'big') +
            (1).to_bytes(1, 'big')
        )

        # 3. Транзакция Proxy
        tx_call = proxy.functions.execTransaction(
            CTF_ADDRESS, 0, ctf_data, 0, 0, 0, 0,
            "0x0000000000000000000000000000000000000000",
            "0x0000000000000000000000000000000000000000",
            signature
        )

        # 4. Билд и отправка
        tx = tx_call.build_transaction({
            'from': account.address,
            'chainId': 137,
            'nonce': w3.eth.get_transaction_count(account.address),
            'gasPrice': w3.eth.gas_price
        })

        try:
            est_gas = w3.eth.estimate_gas(tx)
            tx['gas'] = int(est_gas * 1.3)
        except:
            tx['gas'] = 500000

        signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        raw_tx = get_raw_tx_bytes(signed_tx)
        tx_hash = w3.eth.send_raw_transaction(raw_tx)

        log(f"🚀 Транзакция отправлена! Hash: https://polygonscan.com/tx/{w3.to_hex(tx_hash)}")

        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt.status == 1:
            log("✅ Успешно склеймлено!")
        else:
            log("❌ Транзакция откатилась (revert).")

    except Exception as e:
        log(f"❌ Ошибка в процессе клейма: {e}")

def run_cycle():
    """Один полный цикл проверки."""
    # Инициализируем соединение заново каждый цикл для надежности
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    if not w3.is_connected():
        log("⚠️ Нет подключения к RPC. Пропуск цикла.")
        return

    try:
        account = Account.from_key(PRIVATE_KEY)
    except:
        log("⚠️ Неверный приватный ключ.")
        return

    conditions = get_redeemable_markets(PROXY_ADDRESS)

    if not conditions:
        log("Позиций для клейма не найдено.")
    else:
        log(f"🔥 Найдено рынков: {len(conditions)}")
        for cond in conditions:
            redeem_via_proxy(w3, account, cond)
            time.sleep(3) # Пауза между клеймами, чтобы nonce успел обновиться

def main():
    log("🤖 Бот запущен.")
    log(f"🕒 Интервал проверки: {int(CHECK_INTERVAL / 60)} минут.")
    log(f"👤 Proxy Address: {PROXY_ADDRESS}")

    while True:
        try:
            run_cycle()
        except Exception as e:
            log(f"💥 Критическая ошибка цикла: {e}")

        log(f"💤 Сон {int(CHECK_INTERVAL / 60)} минут...")
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n🛑 Скрипт остановлен пользователем.")