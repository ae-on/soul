import sqlite3
import os
from cryptography.fernet import Fernet
from dotenv import load_dotenv

load_dotenv()
SECRET_KEY = os.getenv("SECRET_KEY")
cipher = Fernet(SECRET_KEY.encode())  # ключ должен быть 32-байтовый base64

def get_db_connection():
    conn = sqlite3.connect('clients.db')
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db_connection() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS clients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                full_name TEXT,
                phone_number TEXT,
                utm_source TEXT,
                registration_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                consent_given BOOLEAN DEFAULT 0
            )
        ''')

def encrypt_data(data):
    if data is None:
        return None
    return cipher.encrypt(data.encode()).decode()

def decrypt_data(encrypted_data):
    if encrypted_data is None:
        return None
    return cipher.decrypt(encrypted_data.encode()).decode()

def save_user(telegram_id, utm_source, full_name=None, phone=None):
    with get_db_connection() as conn:
        # Проверяем, есть ли уже пользователь
        cur = conn.execute('SELECT id FROM clients WHERE telegram_id = ?', (telegram_id,))
        if cur.fetchone() is None:
            # Вставляем нового, шифруем имя и телефон если переданы
            encrypted_name = encrypt_data(full_name) if full_name else None
            encrypted_phone = encrypt_data(phone) if phone else None
            conn.execute(
                'INSERT INTO clients (telegram_id, utm_source, full_name, phone_number) VALUES (?, ?, ?, ?)',
                (telegram_id, utm_source, encrypted_name, encrypted_phone)
            )
        else:
            # Можно обновить utm_source, если он изменился (например, при повторном переходе)
            conn.execute(
                'UPDATE clients SET utm_source = ? WHERE telegram_id = ?',
                (utm_source, telegram_id)
            )
